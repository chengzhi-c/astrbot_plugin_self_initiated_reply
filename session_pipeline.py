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
    now_ts,
)
from .session_gate import SessionGate
from .utils import (
    collapse_whitespace,
    session_is_private,
    session_whitelisted,
)

_MAX_RECORD_SAVE_ATTEMPTS = 2
_RECORD_SAVE_RETRY_SEC = 0.5
# 真实退避间隔：sleep(0) 只让出一个事件循环 tick，磁盘瞬时故障（Windows 文件
# 占用、网络盘抖动）在零退避下两次必然背靠背失败，重试形同虚设。0.5s 足以跨过
# 瞬时占用，又不会把 record task 拖出 §5 的收敛边界。


def record_decision(
    last_decisions: dict[str, Any],
    umo: str,
    trigger: str,
    *,
    should_reply: bool,
    reason: str,
) -> None:
    last_decisions[umo] = {
        "at": round(now_ts(), 3),
        "trigger": trigger,
        "should_reply": should_reply,
        "reason": reason,
    }


async def decide_session_reply(
    decision: Any,
    gate: Any,
    last_decisions: dict[str, Any],
    umo: str,
    state: SessionState,
    *,
    trigger: str,
    force: bool,
    expected_generation: int | None,
) -> dict[str, Any] | str:
    result = await decision.decide(umo, state, trigger=trigger, force=force)
    if isinstance(result, str):
        record_decision(last_decisions, umo, trigger, should_reply=False, reason=result)
        return result
    if not gate.is_current(umo, expected_generation):
        return STALE_TASK_MESSAGE
    record_decision(
        last_decisions,
        umo,
        trigger,
        should_reply=bool(result.get("should_reply")),
        reason=str(result.get("reason") or ""),
    )
    logger.info(
        "[%s] decision session=%s trigger=%s should_reply=%s elapsed=%.2fs reason=%s",
        PLUGIN_ID,
        umo,
        trigger,
        result.get("should_reply"),
        float(result.get("elapsed_sec") or 0.0),
        collapse_whitespace(result.get("reason") or "-"),
    )
    if not result.get("should_reply"):
        # 契约（decide 侧）：should_reply=False 时已转成字符串返回，文案单源在
        # decision。走到这里说明该契约被破坏——静默放行会造成"判断不该回复
        # 却仍然生成并发送"。
        raise RuntimeError("decide() must convert should_reply=False into a string")
    return result


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
        track_critical_task: Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]],
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
        pre_guard = self.session_check_guard(
            umo, force=force, expected_generation=expected_generation
        )
        if pre_guard is not None:
            # 锁前预检：与持锁路径同一门卫，避免为注定早退的会话创建锁表条目。
            # 持锁后仍会复检（运行互斥的 TOCTOU），此处只做“不建锁”的快速返回。
            return pre_guard
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
        state = self._state_for(umo)
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
                silence_active_at=observed_active_at,
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
                force=force,
                trigger=trigger,
                silence_active_at=observed_active_at,
            )
        finally:
            try:
                try:
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
                except RuntimeError as exc:
                    # 任务注册被拒（停止中 / 降级 / 隔离任务超限）：协程已被
                    # _create_critical_task 关闭，账本停在 sealed、配额不记
                    # （test_attempt_ledger 锚定）。只留日志——若让它从 finally
                    # 传出，会改写主链已得出的结果或在途异常。
                    logger.error(
                        "[%s] proactive ledger finalizer registration rejected session=%s error=%s",
                        PLUGIN_ID,
                        umo,
                        exc,
                    )
                else:
                    try:
                        await asyncio.shield(finalizer)
                    except RuntimeError as exc:
                        # finalizer 内部（_finalize_ledger 挂 record task 时）注册被拒
                        # 的镜像出口：记账错误同样不外溢，理由同上。
                        logger.error(
                            "[%s] proactive ledger finalizer rejected session=%s error=%s",
                            PLUGIN_ID,
                            umo,
                            exc,
                        )
                    except asyncio.CancelledError:
                        await asyncio.shield(finalizer)
                        raise
            finally:
                # unmark 挂在最外层：注册被拒路径同样必经，否则会话永久卡在 running。
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
        logger.debug(
            "[%s] record proactive ledger_id=%s session=%s submissions=%s unknown=%s",
            PLUGIN_ID,
            ledger.ledger_id,
            umo,
            ledger.has_submission,
            ledger.has_unknown,
        )
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
                    logger.debug(
                        "[%s] record proactive completed ledger_id=%s session=%s",
                        PLUGIN_ID,
                        ledger.ledger_id,
                        umo,
                    )
                    return True
                logger.warning(
                    "[%s] record proactive persistence failed ledger_id=%s session=%s attempt=%d",
                    PLUGIN_ID,
                    ledger.ledger_id,
                    umo,
                    attempt_no + 1,
                )
                if attempt_no + 1 < _MAX_RECORD_SAVE_ATTEMPTS:
                    await asyncio.sleep(_RECORD_SAVE_RETRY_SEC)
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
                "[%s] proactive ledger finalizer failed ledger_id=%s session=%s error=%s",
                PLUGIN_ID,
                ledger.ledger_id,
                umo,
                exc,
            )
            return False

    def _create_critical_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = self._track_critical_task(coro)
        if task is None:
            coro.close()
            raise RuntimeError("critical task registration was rejected")
        return task

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
        # seal() 只把 open→sealed，recorded / record_failed 只能从 recording 经
        # mark_* 到达。这两支是重入终态（由 test_attempt_ledger 锚定）：二次进入
        # 直接返回既有结论，不再挂第二条 record task。
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
