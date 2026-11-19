"""外发网关：一次发送尝试的结果分类与直发预算。

拥有：把宿主 sender 的多种返回形态归一为三态（DELIVERED /
FAILED_BEFORE_SUBMIT / UNKNOWN，加闸门与预算的 SUPPRESSED）、工具直发的
预算扣减与文本记账。

预算在调用适配器**之前**扣（``send`` 内），因为提交之后抛异常仍可能已送达，
那种情况必须计入而非退还。三态只描述「这一次尝试的结果」，是否重试、是否
消耗冷却与配额由 ``delivery`` 决定。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .models import AttemptLedger, SendOutcome, SendStatus, SuppressCode
from .utils import redact_exc_text


@dataclass(frozen=True)
class OutboundResult:
    """The classified send result plus the adapter's raw return value."""

    outcome: SendOutcome
    raw_result: Any = None

    @property
    def submitted(self) -> bool:
        return self.outcome.status in {SendStatus.DELIVERED, SendStatus.UNKNOWN}


class OutboundGateway:
    """Classify one outbound channel and bound tool-originated direct sends."""

    def __init__(
        self,
        sender: Callable[[Any], Any],
        *,
        max_direct_sends: int = 0,
        allow_direct: Callable[[], bool] | None = None,
        ledger: AttemptLedger | None = None,
    ) -> None:
        self._sender = sender
        self._max_direct_sends = max(0, int(max_direct_sends))
        self._allow_direct = allow_direct
        self._ledger = ledger or AttemptLedger()
        self._direct_send_count = 0
        self._direct_fail_count = 0

    @staticmethod
    def _message_text(message: Any) -> str:
        try:
            return str(message.get_plain_text() or "").strip()
        except Exception:
            return ""

    async def send(
        self,
        message: Any,
        *,
        kind: str = "reply",
        sender: Callable[[Any], Any] | None = None,
        record_text: bool = True,
    ) -> OutboundResult:
        """Classify one outbound call and retain its evidence in the ledger."""
        is_direct = kind == "tool_direct"
        send_message = self._sender if sender is None else sender
        if not self._ledger.accepts_attempts:
            # 账本已封时收到迟到的工具直发是可预期时序（被隔离的运行保留了
            # tracker），降级为闸门拒绝；抛异常会让该次直发的记账整条丢失。
            outcome = SendOutcome(
                SendStatus.SUPPRESSED,
                "ledger already sealed",
                SuppressCode.GATE_REJECTED,
            )
            return OutboundResult(outcome)
        attempt = self._ledger.reserve(
            "tool_direct" if is_direct else "final_reply",
            self._message_text(message) if is_direct and record_text else "",
        )
        if is_direct:
            if self._direct_send_count >= self._max_direct_sends:
                outcome = SendOutcome(
                    SendStatus.SUPPRESSED,
                    "direct send budget exhausted",
                    SuppressCode.BUDGET,
                )
                self._ledger.finish_before_submit(attempt, outcome.status)
                return OutboundResult(outcome)
            if self._direct_fail_count >= self._max_direct_sends:
                outcome = SendOutcome(
                    SendStatus.SUPPRESSED,
                    "direct send failure budget exhausted",
                    SuppressCode.BUDGET,
                )
                self._ledger.finish_before_submit(attempt, outcome.status)
                return OutboundResult(outcome)
            gate_ok = self._allow_direct is None or bool(self._allow_direct())
            if not gate_ok:
                outcome = SendOutcome(
                    SendStatus.SUPPRESSED,
                    "direct send gate rejected",
                    SuppressCode.GATE_REJECTED,
                )
                self._ledger.finish_before_submit(attempt, outcome.status)
                return OutboundResult(outcome)
            self._direct_send_count += 1

        self._ledger.mark_in_flight(attempt)
        raw_result: Any = None
        try:
            raw_result = send_message(message)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
        except asyncio.CancelledError:
            # 与下方 except Exception 同样分类为 UNKNOWN：提交状态未知，不得
            # 自动重试。**故意不重抛取消**——调用方分两类：
            #   - final_reply：``delivery._send_via_context`` 依赖它正常返回，
            #     以便走完 UNKNOWN 记账（消耗冷却与配额，防止重复直发）；
            #   - tool_direct：``generation`` 的 tracked_send 在 agent run 内
            #     调用，吞掉取消后该 run 只能靠 ``_graceful_stop`` 的第二次
            #     cancel() + 宽限窗口 + 隔离登记兜底收敛。
            # 两条路径都不希望在这里把取消变成异常上抛，故统一只分类不重抛。
            outcome = SendOutcome(SendStatus.UNKNOWN, "sender cancelled after start")
        except Exception as exc:
            # adapter 异常串常带带凭证的请求 URL（provider 与平台适配器都有），
            # 与其余异常路径同一脱敏口径；redact_exc_text 一并挡住 __str__ 再抛。
            outcome = SendOutcome(SendStatus.UNKNOWN, redact_exc_text(exc))
        else:
            if raw_result is False:
                outcome = SendOutcome(
                    SendStatus.FAILED_BEFORE_SUBMIT,
                    "sender returned False (definitely not submitted)",
                )
            else:
                # ``None`` 与真值同归 DELIVERED：event.send / context.send_message
                # 正常返回 None。未抛异常即已提交，记 DELIVERED 才会写进
                # assistant 历史供后续决策参考。
                outcome = SendOutcome(SendStatus.DELIVERED, "sender completed")

        if is_direct and outcome.status is SendStatus.FAILED_BEFORE_SUBMIT:
            self._direct_send_count -= 1
            self._direct_fail_count += 1
        self._ledger.resolve(attempt, outcome.status)
        return OutboundResult(outcome, raw_result)
