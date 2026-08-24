"""会话检查主链：闸门 → 裁决 → 生成 → 投递。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, cast

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .decision import DecisionMaker
from .delivery import DeliveryRunner
from .generation import GenerationRunner
from .models import (
    PLUGIN_ID,
    STALE_TASK_MESSAGE,
    AttemptLedger,
    AttemptState,
    SessionState,
    Settings,
)
from .plugin_state import decide_session_reply
from .session_gate import SessionGate
from .utils import (
    collapse_whitespace,
    session_is_private,
    session_whitelisted,
    whitelist_storage_key,
)

_MAX_RECORD_SAVE_ATTEMPTS = 2


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
        track_critical_task: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]] | None = None,
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
        self._track_critical_task = track_critical_task

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
        ledger = AttemptLedger()
        effective_reply = ""
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
                ledger=ledger,
                force=force,
            )
            if pipeline_reply.ledger is not None and pipeline_reply.ledger is not ledger:
                raise RuntimeError("generation returned a different attempt ledger")
            effective_reply = pipeline_reply.text.strip()
            direct_send_count = ledger.direct_send_count
            if effective_reply and ledger.direct_texts:
                normalized_reply = collapse_whitespace(effective_reply)
                if any(
                    normalized_reply == collapse_whitespace(text) for text in ledger.direct_texts
                ):
                    logger.info(
                        "[%s] suppress duplicate final text after tool direct send session=%s",
                        PLUGIN_ID,
                        umo,
                    )
                    effective_reply = ""
            if not effective_reply and not direct_send_count:
                return "管线未生成内容。"

            return await self._delivery.deliver_reply(
                umo,
                state,
                effective_reply,
                direct_send_count,
                ledger=ledger,
                expected_generation=expected_generation,
                observed_active_at=observed_active_at,
                force=force,
                trigger=trigger,
            )
        finally:
            finalizer = self._create_critical_task(
                self._finalize_ledger(
                    umo,
                    state,
                    ledger,
                    effective_reply,
                    expected_generation=expected_generation,
                    observed_active_at=observed_active_at,
                )
            )
            try:
                await asyncio.shield(finalizer)
            except asyncio.CancelledError:
                await asyncio.shield(finalizer)
                raise
            finally:
                self._gate.unmark_running(umo)

    async def _record_ledger(
        self,
        umo: str,
        state: SessionState,
        ledger: AttemptLedger,
        reply: str,
        *,
        expected_generation: int | None,
        observed_active_at: float | None,
    ) -> bool:
        """Apply one ledger outcome and retry only persistence, never state mutation."""
        try:
            if not ledger.has_submission:
                ledger.mark_recorded()
                return True

            final_states = [
                attempt.state for attempt in ledger.attempts if attempt.kind == "final_reply"
            ]
            final_state = final_states[-1] if final_states else None
            if final_state is AttemptState.DELIVERED:
                confirmed = True
                recorded_reply = reply
            elif ledger.has_unknown:
                confirmed = False
                recorded_reply = ""
            else:
                confirmed = True
                recorded_reply = ""

            self._delivery.apply_proactive_state(
                umo,
                state,
                recorded_reply,
                ledger.direct_send_count,
                expected_generation=expected_generation,
                observed_active_at=observed_active_at,
                confirmed=confirmed,
            )
            for attempt_no in range(_MAX_RECORD_SAVE_ATTEMPTS):
                if await self._delivery.persist_proactive_state():
                    ledger.mark_recorded()
                    return True
                if attempt_no + 1 < _MAX_RECORD_SAVE_ATTEMPTS:
                    await asyncio.sleep(0)
            ledger.mark_record_failed("state persistence retries exhausted")
            return False
        except asyncio.CancelledError:
            if ledger.phase == "recording":
                ledger.mark_record_failed("state persistence task cancelled")
            raise
        except Exception as exc:
            if ledger.phase == "recording":
                ledger.mark_record_failed(str(exc))
            logger.error(
                "[%s] proactive ledger finalizer failed session=%s error=%s",
                PLUGIN_ID,
                umo,
                exc,
            )
            return False

    def _create_critical_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        if self._track_critical_task is not None:
            task = self._track_critical_task(coro)
            if task is None:
                coro.close()
                raise RuntimeError("critical task registration was rejected")
            return task
        return asyncio.create_task(coro)

    async def _finalize_ledger(
        self,
        umo: str,
        state: SessionState,
        ledger: AttemptLedger,
        reply: str,
        *,
        expected_generation: int | None,
        observed_active_at: float | None,
    ) -> bool:
        """Seal one run and await its single record task, including cancellation."""
        ledger.seal()
        if ledger.phase == "recorded":
            return True
        if ledger.phase == "record_failed":
            return False
        task = cast(asyncio.Task[Any] | None, ledger.record_task)
        if task is None:
            task = self._create_critical_task(
                self._record_ledger(
                    umo,
                    state,
                    ledger,
                    reply,
                    expected_generation=expected_generation,
                    observed_active_at=observed_active_at,
                )
            )
            if not ledger.start_recording(task):
                task.cancel()
                return ledger.phase == "recorded"
        try:
            await asyncio.shield(cast(asyncio.Future[Any], task))
            return ledger.phase == "recorded"
        except asyncio.CancelledError:
            await asyncio.shield(cast(asyncio.Future[Any], task))
            raise

    def session_check_guard(
        self, umo: str, *, force: bool, expected_generation: int | None
    ) -> str | None:
        """会话级前置门卫：全部通过返回 None，否则返回跳过原因。"""
        if self._is_stopping() or (not force and not self._is_enabled()):
            return "插件未启用。"
        if not force and not session_whitelisted(umo, self._settings.whitelist):
            return "会话不在主动回复白名单。"
        if not force and session_is_private(umo) and not self._settings.enabled_private_sessions:
            return "未启用私聊主动回复。"
        if not self._gate.is_current(umo, expected_generation):
            return STALE_TASK_MESSAGE
        if not force and not self._last_events.get(umo):
            return "没有可用的最近消息事件。"
        if self._gate.is_running(umo):
            return "已有判断任务在运行。"
        return None
