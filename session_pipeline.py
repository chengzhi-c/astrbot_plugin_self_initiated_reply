"""会话检查主链：闸门 → 裁决 → 生成 → 投递。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .decision import DecisionMaker
from .delivery import DeliveryRunner
from .generation import GenerationRunner
from .models import PLUGIN_ID, STALE_TASK_MESSAGE, SessionState, Settings
from .plugin_state import decide_session_reply
from .session_gate import SessionGate
from .utils import collapse_whitespace, session_whitelisted, whitelist_storage_key


class SessionPipeline:
    """单会话主动回复检查编排（持锁路径与未持锁入口）。"""

    def __init__(
        self,
        *,
        state_for: Callable[[str], SessionState],
        generation: GenerationRunner,
        delivery: DeliveryRunner,
        is_stopping: Callable[[], bool],
        is_enabled: Callable[[], bool],
        settings: Settings,
        gate: SessionGate,
        decision: DecisionMaker,
        last_events: dict[str, AstrMessageEvent],
        last_decisions: dict[str, Any],
    ) -> None:
        self._state_for = state_for
        self._generation = generation
        self._delivery = delivery
        self._is_stopping = is_stopping
        self._is_enabled = is_enabled
        self._settings = settings
        self._gate = gate
        self._decision = decision
        self._last_events = last_events
        self._last_decisions = last_decisions

    async def check_session(
        self,
        umo: str,
        *,
        trigger: str,
        force: bool,
        expected_generation: int | None = None,
    ) -> str:
        lock = self._gate.lock_for(umo)
        if lock.locked():
            return "已有判断任务在运行。"
        async with lock:
            return await self.check_session_locked(
                umo,
                trigger=trigger,
                force=force,
                expected_generation=expected_generation,
            )

    async def check_session_locked(
        self,
        umo: str,
        *,
        trigger: str,
        force: bool,
        expected_generation: int | None = None,
    ) -> str:
        """单会话检查主链；调用方已持有该会话检查锁。"""
        guard = self.session_check_guard(umo, force=force, expected_generation=expected_generation)
        if guard is not None:
            return guard
        if expected_generation is None:
            baseline = self._gate.current(umo)
            if baseline:
                expected_generation = baseline
        state = self._state_for(whitelist_storage_key(umo))
        observed_active_at = state.last_active_at

        state.refresh_day()
        gate = self._decision.local_gate(state, force=force)
        if gate:
            logger.debug(
                "[%s] skip session=%s trigger=%s reason=%s",
                PLUGIN_ID,
                umo,
                trigger,
                gate,
            )
            return gate

        self._gate.mark_running(umo)
        try:
            decision = await decide_session_reply(
                self._decision,
                self._gate,
                self._last_decisions,
                umo,
                state,
                trigger=trigger,
                force=force,
                expected_generation=expected_generation,
            )
            if isinstance(decision, str):
                return decision

            pipeline_reply = await self._generation.generate(
                umo,
                state,
                expected_generation=expected_generation,
                force=force,
            )
            reply = pipeline_reply.text.strip()
            direct_send_count = pipeline_reply.direct_send_count
            if reply and pipeline_reply.direct_texts:
                normalized_reply = collapse_whitespace(reply)
                if any(
                    normalized_reply == collapse_whitespace(text)
                    for text in pipeline_reply.direct_texts
                ):
                    logger.info(
                        "[%s] suppress duplicate final text after tool direct send session=%s",
                        PLUGIN_ID,
                        umo,
                    )
                    reply = ""
            if not reply and not direct_send_count:
                return "管线未生成内容。"

            return await self._delivery.deliver_reply(
                umo,
                state,
                reply,
                direct_send_count,
                expected_generation=expected_generation,
                observed_active_at=observed_active_at,
                force=force,
                trigger=trigger,
            )
        finally:
            self._gate.unmark_running(umo)

    def session_check_guard(
        self, umo: str, *, force: bool, expected_generation: int | None
    ) -> str | None:
        """会话级前置门卫：全部通过返回 None，否则返回跳过原因。"""
        if self._is_stopping() or (not force and not self._is_enabled()):
            return "插件未启用。"
        if not force and not session_whitelisted(umo, self._settings.whitelist):
            return "会话不在主动回复白名单。"
        if not self._gate.is_current(umo, expected_generation):
            return STALE_TASK_MESSAGE
        if not force and not self._last_events.get(umo):
            return "没有可用的最近消息事件。"
        if self._gate.is_running(umo):
            return "已有判断任务在运行。"
        return None
