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
from .utils import safe_exc_text


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
        sender: Callable[[Any], Any] | None,
        *,
        max_direct_sends: int = 0,
        allow_direct: Callable[[], bool] | None = None,
        none_status: SendStatus = SendStatus.DELIVERED,
        ledger: AttemptLedger | None = None,
    ) -> None:
        if none_status not in {
            SendStatus.DELIVERED,
            SendStatus.UNKNOWN,
            SendStatus.FAILED_BEFORE_SUBMIT,
        }:
            raise ValueError("none_status must describe an adapter completion")
        self._sender = sender
        self._max_direct_sends = max(0, int(max_direct_sends))
        self._allow_direct = allow_direct
        self._none_status = none_status
        self._ledger = ledger or AttemptLedger()
        self._direct_send_count = 0
        self._direct_fail_count = 0

    @property
    def ledger(self) -> AttemptLedger:
        return self._ledger

    @staticmethod
    def _message_text(message: Any) -> str:
        try:
            return str(message.get_plain_text() or "").strip()
        except Exception:
            return ""

    async def send(self, message: Any, *, kind: str = "reply") -> OutboundResult:
        """Classify one outbound call and retain its evidence in the ledger."""
        is_direct = kind == "tool_direct"
        if not self._ledger.accepts_attempts:
            # 账本已封（生成已结束/被隔离、或在记 RECORDED）：迟到的工具直发是
            # 可预期的时序，不是编程错误。`reserve` 的 RuntimeError 语义留给
            # 真正的调用点错误，这里降级为闸门拒绝，被隔离的运行保留了 tracker
            # （generation._cleanup_generation_state 的刻意取舍），其内的工具直发
            # 必然走到这里；抛异常会让该次直发的记账整条丢失，与"继续受预算与
            # 代次约束"的意图相反。
            outcome = SendOutcome(
                SendStatus.SUPPRESSED,
                "ledger already sealed",
                SuppressCode.GATE_REJECTED,
            )
            return OutboundResult(outcome)
        attempt = self._ledger.reserve(
            "tool_direct" if is_direct else "final_reply",
            self._message_text(message) if is_direct else "",
        )
        if not callable(self._sender):
            outcome = SendOutcome(SendStatus.FAILED_BEFORE_SUBMIT, "outbound sender unavailable")
            self._ledger.finish_before_submit(attempt, outcome.status)
            return OutboundResult(outcome)
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
            if self._allow_direct is not None and not self._allow_direct():
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
            raw_result = self._sender(message)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
        except asyncio.CancelledError:
            outcome = SendOutcome(SendStatus.UNKNOWN, "sender cancelled after start")
        except Exception as exc:
            outcome = SendOutcome(SendStatus.UNKNOWN, safe_exc_text(exc))
        else:
            if raw_result is False:
                outcome = SendOutcome(
                    SendStatus.FAILED_BEFORE_SUBMIT,
                    "sender returned False (definitely not submitted)",
                )
            elif raw_result is None:
                outcome = SendOutcome(self._none_status, "sender completed")
            else:
                outcome = SendOutcome(SendStatus.DELIVERED, "sender completed")

        if is_direct and outcome.status is SendStatus.FAILED_BEFORE_SUBMIT:
            self._direct_send_count -= 1
            self._direct_fail_count += 1
        self._ledger.resolve(attempt, outcome.status)
        return OutboundResult(outcome, raw_result)
