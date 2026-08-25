"""主动回复投递状态机。

负责一次回复投递的完整状态机：发送前门卫、装饰钩子调用与代次复核
（前/后/发送中）、事件发送与 context 兜底发送、发送结果分类、
UNKNOWN 语义（不自动重试、不触发 after-send 钩子、仍消耗冷却与日配额
并推进观察窗口）、主动状态记录（冷却、日配额、观察窗口、历史条目）。

对外暴露三个入口：
- ``deliver_reply``：投递一次回复（发送前门卫 + 状态机 + 结果分类）
- ``send_reply``：发送一条文本回复（钩子装饰与代次复核 + 事件/context 发送）
- ``apply_proactive_state`` / ``persist_proactive_state``：pipeline 记账入口

宿主交互经注入回调执行：钩子调用与 context 发送运行时查找
（测试替换 ``main.call_event_hook`` / ``plugin.context.send_message``
后仍指向最新实现）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .models import (
    PLUGIN_ID,
    STALE_REPLY_MESSAGE,
    STALE_TASK_MESSAGE,
    AttemptLedger,
    LocalGateCallback,
    SendOutcome,
    SendStatus,
    SessionState,
    Settings,
    now_ts,
)
from .outbound import OutboundGateway

# 注入回调的类型别名。这五个全按位置调用，故用 Callable；models.py 的三个
# Protocol 有关键字形参（limit / enabled+provider_id / force），Callable 表达不了。
#
# - SaveStorageCallback：生产注入 ``_save_storage``（锁串行 + 快照 + to_thread
#   原子写），返回即已落盘。落盘失败只影响持久化，不影响已发生的投递，故调用点兜异常。
# - RuntimeCallback：宿主私有符号适配层获取器。用 getter 而非传值，
#   使测试替换 ``_AGENT_RUNTIME`` 后仍指向最新实现。
SaveStorageCallback = Callable[[], Awaitable[None]]
CallHookCallback = Callable[[Any, Any], Awaitable[None]]
ContextSendCallback = Callable[[str, Any], Awaitable[Any]]
RuntimeCallback = Callable[[], Any]


class DeliveryRunner:
    """一次主动回复的投递状态机：门卫、钩子、发送与状态记录。"""

    def __init__(
        self,
        *,
        settings: Settings,
        gate: Any,
        local_gate: LocalGateCallback,
        last_events: dict[str, Any],
        call_hook: CallHookCallback,
        context_send: ContextSendCallback,
        save_storage: SaveStorageCallback,
        runtime: RuntimeCallback,
        is_stopping: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self._gate = gate
        self._local_gate = local_gate
        self._last_events = last_events
        self._call_hook = call_hook
        self._context_send = context_send
        self._save_storage = save_storage
        self._runtime = runtime
        self._is_stopping = is_stopping or (lambda: False)

    @staticmethod
    def _clear_result(last_event: Any) -> None:
        """事件结果回收：各出口统一形状（防泄漏，宿主异常不阻断投递）。"""
        try:
            last_event.clear_result()
        except Exception:
            pass

    async def deliver_reply(
        self,
        umo: str,
        state: SessionState,
        reply: str,
        direct_send_count: int,
        *,
        ledger: AttemptLedger,
        expected_generation: int | None,
        force: bool,
        trigger: str,
    ) -> str:
        """发送前门卫与发送状态机；返回结果消息。记账由 pipeline 的 ledger 收口。"""
        ledger_id = ledger.ledger_id
        if self._is_stopping():
            logger.info(
                "[%s] suppress reply while plugin is stopping ledger_id=%s session=%s",
                PLUGIN_ID,
                ledger_id,
                umo,
            )
            return "插件未启用。"
        gate = "" if self._gate.is_current(umo, expected_generation) else STALE_TASK_MESSAGE
        if not gate:
            gate = self._local_gate(state, force=force)
        if gate:
            logger.debug(
                "[%s] skip before send ledger_id=%s session=%s trigger=%s reason=%s",
                PLUGIN_ID,
                ledger_id,
                umo,
                trigger,
                gate,
            )
            if direct_send_count:
                return f"工具主动回复已完成；{gate}"
            return gate

        if reply:
            sent = await self.send_reply(
                umo,
                reply,
                ledger=ledger,
                expected_generation=expected_generation,
            )
            if not sent.delivered:
                if sent.status is SendStatus.UNKNOWN:
                    # 可能已经提交：不自动重试；消耗冷却与日配额并推进观察窗口
                    # （视为已尝试），防止巡检或新消息立刻对同一事件重复处理。
                    # 注意：即使工具已直发也必须记录——否则观察窗口不推进，
                    # 同一事件会被再次处理并可能再次直发。
                    return "主动发送状态未知，未自动重试。"
                if not self._gate.is_current(umo, expected_generation):
                    return STALE_REPLY_MESSAGE
                if sent.status is SendStatus.SUPPRESSED:
                    return STALE_REPLY_MESSAGE
                return "主动发送失败。"
        else:
            # 仅有工具直发。这里不再合成 SendOutcome：原先合成的 DELIVERED 之后
            # 无人读取（下文只用 reply / direct_send_count），却让「确定未提交也算
            # 已投递」这个错觉留在源码里。真正的把关在 OutboundGateway：确定未提交
            # 会退还 direct_send_count，于是 session_pipeline 的 `not reply and not
            # direct_send_count` 会先行短路，走不到这一支。能到这里说明至少有一条
            # 直发是 DELIVERED/UNKNOWN——UNKNOWN 可能已达，扣配额是正确的兜底。
            pass

        if self.settings.log_reply_content and reply:
            preview = reply if len(reply) <= 80 else reply[:80] + "…"
            logger.debug(
                "[%s] proactive reply sent ledger_id=%s session=%s chars=%d "
                "direct_tools=%d text=%s",
                PLUGIN_ID,
                ledger_id,
                umo,
                len(reply),
                direct_send_count,
                preview,
            )
        else:
            logger.debug(
                "[%s] proactive reply sent ledger_id=%s session=%s chars=%d direct_tools=%d",
                PLUGIN_ID,
                ledger_id,
                umo,
                len(reply),
                direct_send_count,
            )

        return "已通过工具主动回复。" if direct_send_count and not reply else "已主动回复。"

    async def send_reply(
        self,
        umo: str,
        reply: str,
        *,
        ledger: AttemptLedger | None = None,
        expected_generation: int | None = None,
    ) -> SendOutcome:
        """Send one proactive reply without retrying an unknown submission.

        本方法只做「复核点 1/4 + 选路」，两条投递路径各自成方法：
        事件仍在手边走 ``_send_via_event``（复核点 2/4、3/4，可触发装饰与发送后钩子），
        否则走 ``_send_via_context``（复核点 4/4，经宿主 context 兜底发送）。
        拆分不改语义：四个复核点的相对位置、UNKNOWN 归类方向、``_clear_result``
        的唯一收敛点均保持原样。
        """
        # 复核点 1/4（真实窗口）：expected_generation 是生成前 advance 拿到的 token，
        # 到此已隔整轮 LLM 生成（多个 await），代次极可能已被新消息推进。此处尚未
        # set_result，无需 _clear_result。
        ledger = ledger or AttemptLedger()
        if self._is_stopping():
            logger.info(
                "[%s] suppress reply while plugin is stopping ledger_id=%s session=%s",
                PLUGIN_ID,
                ledger.ledger_id,
                umo,
            )
            return SendOutcome(SendStatus.SUPPRESSED, "plugin is stopping")
        if not self._gate.is_current(umo, expected_generation):
            logger.info(
                "[%s] suppress stale reply before hooks ledger_id=%s session=%s",
                PLUGIN_ID,
                ledger.ledger_id,
                umo,
            )
            return SendOutcome(SendStatus.SUPPRESSED, "generation changed before hooks")

        last_event = self._last_events.get(umo)
        if last_event:
            return await self._send_via_event(
                umo,
                reply,
                last_event,
                ledger=ledger,
                expected_generation=expected_generation,
            )
        return await self._send_via_context(
            umo,
            reply,
            ledger=ledger,
            expected_generation=expected_generation,
        )

    async def _send_via_event(
        self,
        umo: str,
        reply: str,
        last_event: Any,
        *,
        ledger: AttemptLedger | None,
        expected_generation: int | None,
    ) -> SendOutcome:
        """事件路径投递：装饰钩子 → 代次复核 → 事件 send → 发送后钩子。

        仅由 ``send_reply`` 在 ``last_event`` 为真时调用，进入时复核点 1/4 已通过。
        本方法是唯一会 ``set_result`` 的路径，故所有出口都必须经 ``_clear_result``
        回收（防结果泄漏到宿主后续流程）。
        """
        ledger = ledger or AttemptLedger()
        ledger_id = ledger.ledger_id
        send_started = False
        try:
            last_event.set_result(
                self._runtime()
                .new_event_result()
                .message(reply)
                .set_result_content_type(self._runtime().result_llm_type)
            )
            await self._call_hook(last_event, self._runtime().event_type.OnDecoratingResultEvent)
            # 复核点 2/4（真实窗口）：装饰钩子是 await，期间其他任务可运行、新消息
            # 可推进代次。四处中只有此处与复核点 1 存在真实竞态窗口。
            if not self._gate.is_current(umo, expected_generation):
                self._clear_result(last_event)
                logger.info(
                    "[%s] suppress stale reply after decorating hook ledger_id=%s session=%s",
                    PLUGIN_ID,
                    ledger_id,
                    umo,
                )
                return SendOutcome(SendStatus.SUPPRESSED, "generation changed after decorating")
            if self._is_stopping():
                self._clear_result(last_event)
                logger.info(
                    "[%s] suppress reply after decorating hook lifecycle stop "
                    "ledger_id=%s session=%s",
                    PLUGIN_ID,
                    ledger_id,
                    umo,
                )
                return SendOutcome(SendStatus.SUPPRESSED, "plugin is stopping")
            result = last_event.get_result()
            if result is None or not result.chain:
                self._clear_result(last_event)
                return SendOutcome(
                    SendStatus.FAILED_BEFORE_SUBMIT, "decorating hook produced no result"
                )
            # 复核点 3/4（结构防线）：与复核点 2 之间零 await（get_result 同步），
            # 当前代码下代次不可能在此变化，覆盖靠 test_delivery_blindspots 的
            # _FlipGate(true_times=2) 数调用次数翻转。保留理由是结构性：
            # test_storage_and_umo 锁「钩子后、send 前必须有复核」（ 拆分后
            # 该断言指向本方法），此处紧贴 outbound.send；上方一旦插入任何 await，
            # 这道防线立即变实。
            if not self._gate.is_current(umo, expected_generation):
                self._clear_result(last_event)
                logger.info(
                    "[%s] suppress stale reply before event send ledger_id=%s session=%s",
                    PLUGIN_ID,
                    ledger_id,
                    umo,
                )
                return SendOutcome(SendStatus.SUPPRESSED, "generation changed before send")
            logger.debug(
                "[%s] event send begin ledger_id=%s session=%s chars=%d chain_items=%d",
                PLUGIN_ID,
                ledger_id,
                umo,
                len(reply),
                len(getattr(result, "chain", []) or []),
            )
            outbound = OutboundGateway(last_event.send, ledger=ledger)
            # 悲观默认：send 调用一旦开始，消息就可能已提交。gateway 内部虽把
            # adapter 异常转成 UNKNOWN，但其 except 块自身仍可能抛（异常对象的
            # ``__str__`` 坏掉时 ``str(exc)`` 二次抛），此时异常逃出 gateway 而
            # adapter 早已调用过——下方 except 必须仍归 UNKNOWN，归
            # FAILED_BEFORE_SUBMIT 会不消耗冷却而重发。send 正常返回后再用
            # submitted 精确化（gateway 明确说未提交时才降为提交前失败）。
            send_started = True
            send_result = await outbound.send(result)
            send_started = send_result.submitted
            if not send_result.submitted:
                self._clear_result(last_event)
                return send_result.outcome
            logger.debug(
                "[%s] event send completed ledger_id=%s session=%s chars=%d;"
                " platform adapter completion is not a delivery receipt",
                PLUGIN_ID,
                ledger_id,
                umo,
                len(reply),
            )
            if send_result.outcome.status is SendStatus.DELIVERED:
                # UNKNOWN 可能已经提交也可能没有，不触发 after-send hook，
                # 避免副作用基于未确认的发送结果。
                try:
                    await self._call_hook(
                        last_event, self._runtime().event_type.OnAfterMessageSentEvent
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] after-send hook failed ledger_id=%s session=%s error=%s",
                        PLUGIN_ID,
                        ledger_id,
                        umo,
                        exc,
                    )
            self._clear_result(last_event)
            return send_result.outcome
        except asyncio.CancelledError:
            self._clear_result(last_event)
            raise
        except Exception as exc:
            logger.warning(
                "[%s] event send reply failed ledger_id=%s session=%s error=%s",
                PLUGIN_ID,
                ledger_id,
                umo,
                exc,
                exc_info=True,
            )
            self._clear_result(last_event)
            if send_started:
                return SendOutcome(SendStatus.UNKNOWN, str(exc))
            return SendOutcome(SendStatus.FAILED_BEFORE_SUBMIT, str(exc))

    async def _send_via_context(
        self,
        umo: str,
        reply: str,
        *,
        ledger: AttemptLedger | None,
        expected_generation: int | None,
    ) -> SendOutcome:
        """context 兜底投递：事件已不在手边时经宿主 ``Context.send_message`` 发送。

        仅由 ``send_reply`` 在 ``last_event`` 为假时调用。本路径不 ``set_result``、
        不触发装饰与发送后钩子，故无 ``_clear_result`` 义务。
        """
        ledger = ledger or AttemptLedger()
        ledger_id = ledger.ledger_id
        # 复核点 4/4（结构防线）：与复核点 1 之间没有真实挂起点——
        # ``await self._send_via_context(...)`` 只是进入协程，不向事件循环让出，
        # 故 的拆分没有新开竞态窗口。性质同复核点 3：
        # 为日后此路径插入异步查询预留拦截位。此路径未 set_result，无需 _clear_result。
        if not self._gate.is_current(umo, expected_generation):
            logger.info(
                "[%s] suppress stale reply before context send ledger_id=%s session=%s",
                PLUGIN_ID,
                ledger_id,
                umo,
            )
            return SendOutcome(SendStatus.SUPPRESSED, "generation changed before context send")
        # send_started 取自 ``OutboundResult.submitted``（DELIVERED/UNKNOWN 为真），
        # 与事件路径上方那处同源：是否已提交由 gateway 的分类结果决定，不靠此处
        # 枚举失败场景。下方 ``except`` 必须条件式归类，两个方向的代价不对称——
        # 提交前记 UNKNOWN 会经 record_proactive_state(confirmed=False) 白吃冷却
        # 与日配额；已提交记 FAILED_BEFORE_SUBMIT 会不消耗冷却而重发，制造重复
        # 消息。三条测试各钉一侧：提交前失败、提交后失败、异常逃出 gateway。
        # OutboundGateway 会把 adapter 调用期间的 CancelledError 归类为 UNKNOWN，
        # 让 deliver_reply 继续按不重试语义记录状态；本处单独 raise 只保留给
        # gateway 之外的取消点。
        send_started = False
        try:
            outbound = OutboundGateway(
                lambda message: self._context_send(umo, message),
                # Context.send_message 正常完成返回 None（True 也代表送达），
                # False 已被单独区分为 FAILED_BEFORE_SUBMIT；未抛异常即视为已
                # 提交，记 DELIVERED 才能写入 assistant 历史供后续决策参考。
                none_status=SendStatus.DELIVERED,
                ledger=ledger,
            )
            chain = MessageChain().message(reply)
            # 悲观默认，理由见事件路径同处注释。
            send_started = True
            send_result = await outbound.send(chain)
            send_started = send_result.submitted
            if send_result.outcome.status is SendStatus.UNKNOWN:
                logger.warning(
                    "[%s] context send result unknown ledger_id=%s session=%s detail=%s",
                    PLUGIN_ID,
                    ledger_id,
                    umo,
                    send_result.outcome.detail,
                )
            elif send_result.outcome.status is SendStatus.FAILED_BEFORE_SUBMIT:
                # False = no reachable platform target; the message was not
                # submitted, so it must not consume cooldown/quota.
                logger.warning(
                    "[%s] context send rejected (no reachable platform) "
                    "ledger_id=%s session=%s detail=%s",
                    PLUGIN_ID,
                    ledger_id,
                    umo,
                    send_result.outcome.detail,
                )
            return send_result.outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[%s] send reply failed ledger_id=%s session=%s error=%s",
                PLUGIN_ID,
                ledger_id,
                umo,
                exc,
            )
            if send_started:
                return SendOutcome(SendStatus.UNKNOWN, str(exc))
            return SendOutcome(SendStatus.FAILED_BEFORE_SUBMIT, str(exc))

    def apply_proactive_state(
        self,
        umo: str,
        state: SessionState,
        reply: str,
        direct_send_count: int = 0,
        *,
        expected_generation: int | None = None,
        observed_active_at: float | None = None,
        confirmed: bool = True,
    ) -> None:
        """Apply one outbound fact to memory without starting persistence."""
        at = now_ts()
        text = reply.strip() or f"[工具主动发送 x{direct_send_count}]"
        state.record_proactive_attempt(confirmed=confirmed, text=text, at=at)
        if not confirmed:
            # UNKNOWN may have been delivered: advance the observed window so a
            # later patrol does not regenerate a reply for the same event.
            if self._gate.is_current(umo, expected_generation):
                state.last_proactive_observed_at = (
                    state.last_active_at if observed_active_at is None else observed_active_at
                )
            logger.info(
                "[%s] record unconfirmed proactive send session=%s (submission status unknown)",
                PLUGIN_ID,
                umo,
            )
        elif not self._gate.is_current(umo, expected_generation):
            logger.info(
                "[%s] record delivered stale generation without advancing observation session=%s",
                PLUGIN_ID,
                umo,
            )
        else:
            state.last_proactive_observed_at = (
                state.last_active_at if observed_active_at is None else observed_active_at
            )

    async def persist_proactive_state(self) -> bool:
        """Persist already-applied state without mutating it again."""
        try:
            await self._save_storage()
            return True
        except Exception as exc:
            logger.warning("[%s] proactive state save failed: %s", PLUGIN_ID, exc)
            return False

    async def record_proactive_state(
        self,
        umo: str,
        state: SessionState,
        reply: str,
        direct_send_count: int = 0,
        *,
        expected_generation: int | None = None,
        observed_active_at: float | None = None,
        confirmed: bool = True,
    ) -> bool:
        """Apply and persist one proactive send attempt for legacy callers."""
        self.apply_proactive_state(
            umo,
            state,
            reply,
            direct_send_count,
            expected_generation=expected_generation,
            observed_active_at=observed_active_at,
            confirmed=confirmed,
        )
        return await self.persist_proactive_state()
