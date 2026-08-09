"""主动回复投递状态机（自 main.py 拆分，ticket 05）。

负责一次回复投递的完整状态机：发送前门卫、装饰钩子调用与代次复核
（前/后/发送中）、事件发送与 context 兜底发送、发送结果分类、
UNKNOWN 语义（不自动重试、不触发 after-send 钩子、仍消耗冷却与日配额
并推进观察窗口）、主动状态记录（冷却、日配额、观察窗口、历史条目）。

对外暴露三个入口（main.py 保留同名委托壳，测试替换实例方法仍生效）：
- ``deliver_reply``：投递一次回复（发送前门卫 + 状态机 + 结果分类 + 记录）
- ``send_reply``：发送一条文本回复（钩子装饰与代次复核 + 事件/context 发送）
- ``record_proactive_state``：记录一次主动发送尝试的状态

宿主交互经注入回调执行：钩子调用与 context 发送经 lambda 运行时查找
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
# - RuntimeCallback：宿主私有符号适配层获取器（ticket 13）。用 getter 而非传值，
#   使测试替换 ``_AGENT_RUNTIME`` 后仍指向最新实现。
SaveStorageCallback = Callable[[], Awaitable[None]]
CallHookCallback = Callable[[Any, Any], Awaitable[None]]
ContextSendCallback = Callable[[str, Any], Awaitable[Any]]
SendReplyCallback = Callable[[str, str, int | None], Awaitable[SendOutcome]]
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
        send_reply: SendReplyCallback,
        save_storage: SaveStorageCallback,
        runtime: RuntimeCallback,
    ) -> None:
        self.settings = settings
        self._gate = gate
        self._local_gate = local_gate
        self._last_events = last_events
        self._call_hook = call_hook
        self._context_send = context_send
        self._send_reply = send_reply
        self._save_storage = save_storage
        self._runtime = runtime

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
        expected_generation: int | None,
        observed_active_at: float | None,
        force: bool,
        trigger: str,
    ) -> str:
        """发送前门卫与发送状态机；返回结果消息。"""
        gate = "" if self._gate.is_current(umo, expected_generation) else STALE_TASK_MESSAGE
        if not gate:
            gate = self._local_gate(state, force=force)
        if gate:
            logger.debug(
                "[%s] skip before send session=%s trigger=%s reason=%s",
                PLUGIN_ID,
                umo,
                trigger,
                gate,
            )
            if direct_send_count:
                await self._record_direct_sends(
                    umo,
                    state,
                    direct_send_count,
                    expected_generation=expected_generation,
                    observed_active_at=observed_active_at,
                )
                return f"工具主动回复已完成；{gate}"
            return gate

        if reply:
            sent = await self._send_reply(umo, reply, expected_generation)
            if not sent.delivered:
                if sent.status is SendStatus.UNKNOWN:
                    # 可能已经提交：不自动重试；消耗冷却与日配额并推进观察窗口
                    # （视为已尝试），防止巡检或新消息立刻对同一事件重复处理。
                    # 注意：即使工具已直发也必须记录——否则观察窗口不推进，
                    # 同一事件会被再次处理并可能再次直发。
                    await self._record_direct_sends(
                        umo,
                        state,
                        direct_send_count,
                        expected_generation=expected_generation,
                        observed_active_at=observed_active_at,
                        confirmed=False,
                    )
                    return "主动发送状态未知，未自动重试。"
                if direct_send_count:
                    await self._record_direct_sends(
                        umo,
                        state,
                        direct_send_count,
                        expected_generation=expected_generation,
                        observed_active_at=observed_active_at,
                    )
                if not self._gate.is_current(umo, expected_generation):
                    return STALE_REPLY_MESSAGE
                if sent.status is SendStatus.SUPPRESSED:
                    return STALE_REPLY_MESSAGE
                return "主动发送失败。"
        else:
            sent = SendOutcome(SendStatus.DELIVERED, "仅有工具直发")

        if self.settings.log_reply_content and reply:
            preview = reply if len(reply) <= 80 else reply[:80] + "…"
            logger.debug(
                "[%s] proactive reply sent session=%s chars=%d direct_tools=%d text=%s",
                PLUGIN_ID,
                umo,
                len(reply),
                direct_send_count,
                preview,
            )
        else:
            logger.debug(
                "[%s] proactive reply sent session=%s chars=%d direct_tools=%d",
                PLUGIN_ID,
                umo,
                len(reply),
                direct_send_count,
            )

        await self.record_proactive_state(
            umo,
            state,
            reply,
            direct_send_count,
            expected_generation=expected_generation,
            observed_active_at=observed_active_at,
        )
        return "已通过工具主动回复。" if direct_send_count and not reply else "已主动回复。"

    async def send_reply(
        self, umo: str, reply: str, *, expected_generation: int | None = None
    ) -> SendOutcome:
        """Send one proactive reply without retrying an unknown submission."""
        if not self._gate.is_current(umo, expected_generation):
            logger.info("[%s] suppress stale reply before hooks session=%s", PLUGIN_ID, umo)
            return SendOutcome(SendStatus.SUPPRESSED, "generation changed before hooks")

        last_event = self._last_events.get(umo)
        if last_event:
            send_started = False
            try:
                last_event.set_result(
                    self._runtime()
                    .new_event_result()
                    .message(reply)
                    .set_result_content_type(self._runtime().result_llm_type)
                )
                await self._call_hook(
                    last_event, self._runtime().event_type.OnDecoratingResultEvent
                )
                if not self._gate.is_current(umo, expected_generation):
                    self._clear_result(last_event)
                    logger.info(
                        "[%s] suppress stale reply after decorating hook session=%s",
                        PLUGIN_ID,
                        umo,
                    )
                    return SendOutcome(SendStatus.SUPPRESSED, "generation changed after decorating")
                result = last_event.get_result()
                if result is None or not result.chain:
                    self._clear_result(last_event)
                    return SendOutcome(
                        SendStatus.FAILED_BEFORE_SUBMIT, "decorating hook produced no result"
                    )
                if not self._gate.is_current(umo, expected_generation):
                    self._clear_result(last_event)
                    logger.info(
                        "[%s] suppress stale reply before event send session=%s", PLUGIN_ID, umo
                    )
                    return SendOutcome(SendStatus.SUPPRESSED, "generation changed before send")
                logger.debug(
                    "[%s] event send begin session=%s chars=%d chain_items=%d",
                    PLUGIN_ID,
                    umo,
                    len(reply),
                    len(getattr(result, "chain", []) or []),
                )
                outbound = OutboundGateway(last_event.send)
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
                    "[%s] event send completed session=%s chars=%d;"
                    " platform adapter completion is not a delivery receipt",
                    PLUGIN_ID,
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
                            "[%s] after-send hook failed session=%s error=%s",
                            PLUGIN_ID,
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
                    "[%s] event send reply failed session=%s error=%s",
                    PLUGIN_ID,
                    umo,
                    exc,
                    exc_info=True,
                )
                self._clear_result(last_event)
                if send_started:
                    return SendOutcome(SendStatus.UNKNOWN, str(exc))
                return SendOutcome(SendStatus.FAILED_BEFORE_SUBMIT, str(exc))

        if not self._gate.is_current(umo, expected_generation):
            logger.info("[%s] suppress stale reply before context send session=%s", PLUGIN_ID, umo)
            return SendOutcome(SendStatus.SUPPRESSED, "generation changed before context send")
        # send_started 取自 ``OutboundResult.submitted``（DELIVERED/UNKNOWN 为真），
        # 与事件路径上方那处同源：是否已提交由 gateway 的分类结果决定，不靠此处
        # 枚举失败场景。下方 ``except`` 必须条件式归类，两个方向的代价不对称——
        # 提交前记 UNKNOWN 会经 record_proactive_state(confirmed=False) 白吃冷却
        # 与日配额；已提交记 FAILED_BEFORE_SUBMIT 会不消耗冷却而重发，制造重复
        # 消息。三条测试各钉一侧：提交前失败、提交后失败、异常逃出 gateway。
        # 注意 ``CancelledError`` 不经此分类（下方单独 raise）：取消发生在 adapter
        # 调用期间时消息可能已提交，而调用方 deliver_reply 走不到状态记录，冷却
        # 与观察窗口都不推进。属既有语义缺口，非本处引入。
        send_started = False
        try:
            outbound = OutboundGateway(
                lambda message: self._context_send(umo, message),
                # Context.send_message 正常完成返回 None（True 也代表送达），
                # False 已被单独区分为 FAILED_BEFORE_SUBMIT；未抛异常即视为已
                # 提交，记 DELIVERED 才能写入 assistant 历史供后续决策参考。
                none_status=SendStatus.DELIVERED,
            )
            chain = MessageChain().message(reply)
            # 悲观默认，理由见事件路径同处注释。
            send_started = True
            send_result = await outbound.send(chain)
            send_started = send_result.submitted
            if send_result.outcome.status is SendStatus.UNKNOWN:
                logger.warning(
                    "[%s] context send result unknown session=%s detail=%s",
                    PLUGIN_ID,
                    umo,
                    send_result.outcome.detail,
                )
            elif send_result.outcome.status is SendStatus.FAILED_BEFORE_SUBMIT:
                # False = no reachable platform target; the message was not
                # submitted, so it must not consume cooldown/quota.
                logger.warning(
                    "[%s] context send rejected (no reachable platform) session=%s detail=%s",
                    PLUGIN_ID,
                    umo,
                    send_result.outcome.detail,
                )
            return send_result.outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[%s] send reply failed session=%s error=%s", PLUGIN_ID, umo, exc)
            if send_started:
                return SendOutcome(SendStatus.UNKNOWN, str(exc))
            return SendOutcome(SendStatus.FAILED_BEFORE_SUBMIT, str(exc))

    async def _record_direct_sends(
        self,
        umo: str,
        state: SessionState,
        direct_send_count: int,
        *,
        expected_generation: int | None,
        observed_active_at: float | None,
        confirmed: bool = True,
    ) -> bool:
        """工具直发的状态记录（无文本回复路径共用；0.9.0 低垂果实合并重复调用）。"""
        return await self.record_proactive_state(
            umo,
            state,
            "",
            direct_send_count,
            expected_generation=expected_generation,
            observed_active_at=observed_active_at,
            confirmed=confirmed,
        )

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
        """Persist the outcome of one proactive send attempt.

        ``confirmed=False`` models an UNKNOWN submission that may have reached
        the platform: it consumes the cooldown and the daily quota so later
        triggers do not immediately retry the same conversation, and it also
        advances the observed window (the attempt is treated as done, matching
        the no-retry policy). It does not write an assistant history entry.
        """
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
        # 逐次落盘（见 SaveStorageCallback）：写盘经 to_thread + 原子写，
        # try/except 兜回调自身抛错，失败仅影响持久化不影响已发送事实。
        try:
            await self._save_storage()
            return True
        except Exception as exc:
            logger.warning("[%s] proactive state save failed: %s", PLUGIN_ID, exc)
            return False
