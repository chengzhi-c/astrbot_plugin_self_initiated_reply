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
    LedgerPhase,
    SessionState,
    Settings,
    now_ts,
)
from .session_gate import SessionGate
from .utils import (
    collapse_whitespace,
    redact_exc_text,
    session_is_private,
    session_whitelisted,
)

_MAX_RECORD_SAVE_ATTEMPTS = 2
# 真实退避间隔：sleep(0) 只让出一个事件循环 tick，磁盘瞬时故障在零退避下
# 两次必然背靠背失败。0.5s 足以跨过瞬时占用，又不拖出 §5 的收敛边界。
_RECORD_SAVE_RETRY_SEC = 0.5


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

    async def _decide_session_reply(
        self,
        umo: str,
        state: SessionState,
        *,
        trigger: str,
        force: bool,
        expected_generation: int | None,
    ) -> dict[str, Any] | str:
        """一次判断：通过返回 decision dict，早退返回跳过原因（并登记裁决）。"""
        result = await self._decision.decide(umo, state, trigger=trigger, force=force)
        if isinstance(result, str):
            # 与下方 dict 分支同级：§9 规定的「一次判断一行 INFO」必须覆盖两个
            # 出口。只打 dict 分支时，模型明确说「不接话」这条最高频排障路径
            # 在默认配置下完全不可见（result 串里已经带着理由）。
            logger.info(
                "[%s] decision session=%s trigger=%s should_reply=False reason=%s",
                PLUGIN_ID,
                umo,
                trigger,
                collapse_whitespace(result),
            )
            record_decision(self._last_decisions, umo, trigger, should_reply=False, reason=result)
            return result
        # 决策 INFO 先于代次复核：模型的结论是 §9 留给 INFO 的唯一排障入口，而
        # 代次在判断期间变化恰恰是最需要知道"模型到底说了什么"的一条路径——
        # 放到复核之后，这条路径上运营者既看不到日志也看不到裁决。
        logger.info(
            "[%s] decision session=%s trigger=%s should_reply=%s elapsed=%.2fs reason=%s",
            PLUGIN_ID,
            umo,
            trigger,
            result.get("should_reply"),
            float(result.get("elapsed_sec") or 0.0),
            collapse_whitespace(result.get("reason") or "-"),
        )
        if not self._gate.is_current(umo, expected_generation):
            # 登记丢弃理由而不是模型的结论：否则 /selfreply status 的「最近裁决」
            # 会显示一条本轮什么都没发生的「接话」（与 local_gate 分支同口径，§13）。
            record_decision(
                self._last_decisions,
                umo,
                trigger,
                should_reply=False,
                reason=STALE_TASK_MESSAGE,
            )
            return STALE_TASK_MESSAGE
        record_decision(
            self._last_decisions,
            umo,
            trigger,
            should_reply=bool(result.get("should_reply")),
            reason=str(result.get("reason") or ""),
        )
        if not result.get("should_reply"):
            # decide() 的契约：should_reply=False 时必须转成字符串返回。这里
            # 显式兜底，因为拿到 dict 意味着被注入的 decision 实现退化——那会让
            # 判断模型明确说"不接话"的会话继续走到生成与发送，且全程无日志。
            raise RuntimeError("decide() must convert should_reply=False into a string")
        return result

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
            return pre_guard
        lock = self._gate.lock_for(umo)
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
            # 必须无条件绑定基线：没有代次记录的会话 ``current()`` 返回 0，若把
            # 这个 0 当作"未取到"而保持 None，本轮就没有代次防护——判断期间新
            # 消息 advance 到 1 也不会让它失效，过期结论照发（契约 §5）。
            expected_generation = self._gate.current(umo)
        state = self._state_for(umo)
        observed_active_at = state.last_active_at

        state.refresh_day()
        gate = self._decision.local_gate(state, force=force)
        if gate:
            # 与判断路径同一套登记：网关拦截（冷却/日配额/静默不足/观察窗口）必须
            # 进 ``last_decisions``，否则 `/selfreply status` 的「最近裁决」会显示
            # 上一轮**过期**的"接话"——运营者读到的是"判断说该回"，实际被冷却拦着，
            # 反向误导。默认 cooldown 900s、日配额 5，这正是最常见的路径。
            #
            # 但日志取 DEBUG 而非 INFO：这与 ``scheduler.delayed_check`` 把同一句
            # result 降为 DEBUG 是同一条理由——闸门理由是高频成功路径（默认配置下
            # 每个巡检轮次一条），而 §9 只把「判断结果」那一条留给 INFO。运营者
            # 的可视答案在 ``/selfreply status``，不在日志流里。
            record_decision(self._last_decisions, umo, trigger, should_reply=False, reason=gate)
            logger.debug(
                "[%s] gate session=%s trigger=%s reason=%s",
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
            decision = await self._decide_session_reply(
                umo,
                state,
                trigger=trigger,
                force=force,
                expected_generation=expected_generation,
            )
            if isinstance(decision, str):
                return decision

            # 引用/@ 的目标与生成取数是同一时刻：紧贴在 generate 前冻结。判断阶段
            # 可能长达数十秒（识图 + 判断模型），在那里冻结会让引用指向生成从不曾
            # 依据的消息；而 build_context_text 读 state.recent 发生在第一个 await
            # 的参数求值里，两者之间没有让出事件循环的机会。
            target_event = self._last_events.get(umo)
            reply_text, reply_ledger = await self._generation.generate(
                umo,
                state,
                expected_generation=expected_generation,
                ledger=ledger,
                force=force,
                silence_active_at=observed_active_at,
            )
            if reply_ledger is not None and reply_ledger is not ledger:
                raise RuntimeError("generation returned a different attempt ledger")
            effective_reply = reply_text.strip()
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
                # 判断模型给出的引用决定（None = 未表态，由投递侧按模式兜底）
                quote=decision.get("quote"),
                target_event=target_event,
            )
        finally:
            try:
                finalizer = self._track_critical_task(
                    self._finalize_ledger(
                        umo,
                        state,
                        ledger,
                        effective_reply,
                        expected_generation=expected_generation,
                        observed_active_at=observed_active_at,
                    )
                )
                # 只补等一次：这里是会话 finalizer，不是必须落盘的回滚，无限等待会让
                # 关停卡死；回滚持久化路径才用 utils.await_rollback（那条必须跑完）。
                try:
                    await asyncio.shield(finalizer)
                except asyncio.CancelledError:
                    await asyncio.shield(finalizer)
                    raise
            finally:
                # unmark 挂在最外层：finalizer 抛错的路径同样必经，
                # 否则会话永久卡在 running。
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
    ) -> None:
        """Apply one ledger outcome and retry only persistence, never state mutation.

        结论只写进账本（``mark_recorded`` / ``mark_record_failed``），不返回
        bool：``ledger.phase`` 是结论的唯一真相源。
        """
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
                return

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

            if not recorded_reply.strip() and ledger.direct_texts:
                # 工具直发已把正文发出去了，记账却只留一个占位符：下一轮判断与
                # 生成会把「自己说过什么」读成「[工具主动发送 xN]」，去重基准也
                # 失效。契约 §3 要求生成超时/取消/失败三类出口都不得丢失这份文本。
                recorded_reply = "；".join(text for text in ledger.direct_texts if text.strip())

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
                    return
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
            return
        except asyncio.CancelledError:
            if ledger.phase == LedgerPhase.RECORDING:
                ledger.mark_record_failed("state persistence task cancelled")
            raise
        except Exception as exc:
            # 脱敏必须是唯一入口：本值既进 mark_record_failed 也进日志，
            # 而 provider SDK 的异常串标准形态就带签名 URL。
            detail = redact_exc_text(exc)
            if ledger.phase == LedgerPhase.RECORDING:
                ledger.mark_record_failed(detail)
            logger.error(
                "[%s] proactive ledger finalizer failed ledger_id=%s session=%s error=%s",
                PLUGIN_ID,
                ledger.ledger_id,
                umo,
                detail,
            )
            return

    async def _finalize_ledger(
        self,
        umo: str,
        state: SessionState,
        ledger: AttemptLedger,
        reply: str,
        *,
        expected_generation: int | None,
        observed_active_at: float | None,
    ) -> None:
        """Seal one run and await its single record task, including cancellation.

        不返回 bool：调用方只等它跑完，结论由 ``ledger.phase`` 单点表达。
        seal() 后这支是重入终态：二次进入直接返回既有结论，不再挂第二条
        record task（test_attempt_ledger 锚定）。
        """
        ledger.seal()
        if ledger.phase in {LedgerPhase.RECORDED, LedgerPhase.RECORD_FAILED}:
            return
        # 重入必须复用既有 record task：二次进入时 phase 还是 RECORDING（未终态），
        # 若在此另起一条就会出现两个记账任务同时 apply_proactive_state。
        task = cast(asyncio.Task[Any] | None, ledger.record_task)
        if task is None:
            task = self._track_critical_task(
                self._record_ledger(
                    umo,
                    state,
                    ledger,
                    reply,
                    expected_generation=expected_generation,
                    observed_active_at=observed_active_at,
                )
            )
            # 不需要检查 start_recording 的返回值：seal() 之后 phase 是 SEALED
            # 且 record_task 刚判过 None，这条同步路径上 start_recording 必然
            # 成功。判 False 只能是重入，而重入已被上面的 record_task 复用分支
            # 接住。多出的 cancel 分支永不执行，留着只会让人以为有第二条路径。
            ledger.start_recording(task)
        # 与上面同形：记录任务补等一次即可，不引入无界等待。
        try:
            await asyncio.shield(cast(asyncio.Future[Any], task))
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
