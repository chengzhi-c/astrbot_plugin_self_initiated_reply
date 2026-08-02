from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from .models import SendOutcome, SendStatus


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
    ) -> None:
        self._sender = sender
        self._max_direct_sends = max(0, int(max_direct_sends))
        self._allow_direct = allow_direct
        self._none_status = none_status
        self._direct_send_count = 0
        self._direct_texts: list[str] = []

    @property
    def direct_send_count(self) -> int:
        return self._direct_send_count

    @property
    def direct_texts(self) -> tuple[str, ...]:
        return tuple(self._direct_texts)

    async def send(self, message: Any, *, kind: str = "reply") -> OutboundResult:
        is_direct = kind == "tool_direct"
        if not callable(self._sender):
            return OutboundResult(
                SendOutcome(SendStatus.FAILED_BEFORE_SUBMIT, "outbound sender unavailable")
            )
        if is_direct:
            if self._direct_send_count >= self._max_direct_sends:
                return OutboundResult(
                    SendOutcome(SendStatus.SUPPRESSED, "direct send budget exhausted")
                )
            if self._allow_direct is not None and not self._allow_direct():
                return OutboundResult(
                    SendOutcome(SendStatus.SUPPRESSED, "direct send gate rejected")
                )
            # Consume the budget before calling the adapter. An exception after
            # submit is still potentially delivered and must not be retried.
            self._direct_send_count += 1

        try:
            raw_result = self._sender(message)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An exception after the call began may still have reached the
            # adapter; the outcome is UNKNOWN and must not be retried.
            return OutboundResult(SendOutcome(SendStatus.UNKNOWN, str(exc)))

        if raw_result is False:
            # ``False`` is a definite "not submitted" signal: ``Context.send_message``
            # returns False only when no reachable platform target exists. It must
            # not consume cooldown/quota as an UNKNOWN submission would.
            outcome = SendOutcome(
                SendStatus.FAILED_BEFORE_SUBMIT,
                "sender returned False (definitely not submitted)",
            )
        elif raw_result is None:
            outcome = SendOutcome(self._none_status, "sender completed")
        else:
            outcome = SendOutcome(SendStatus.DELIVERED, "sender completed")

        if is_direct and outcome.status in {SendStatus.DELIVERED, SendStatus.UNKNOWN}:
            try:
                text = str(message.get_plain_text() or "").strip()
            except Exception:
                text = ""
            if text:
                self._direct_texts.append(text)
        return OutboundResult(outcome, raw_result)
