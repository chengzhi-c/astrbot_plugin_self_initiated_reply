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
        # 确定未提交（sender 返 False）会退还 _direct_send_count，否则计数与
        # _direct_texts 失去同源、上层把「一次都没发出去」读成已直发。退还会移除
        # 「目标不可达时反复调 sender」的那个界，故另设失败上限把界留在本类内，
        # 不外借给宿主迭代上限（0.9.4 §5：有界等待不得依赖外部兜底）。
        self._direct_fail_count = 0
        self._direct_texts: list[str] = []

    @property
    def direct_send_count(self) -> int:
        return self._direct_send_count

    @property
    def direct_texts(self) -> tuple[str, ...]:
        return tuple(self._direct_texts)

    async def send(self, message: Any, *, kind: str = "reply") -> OutboundResult:
        """经宿主发送一条消息，把结果归一为「是否确定已提交」三态。

        ``kind="tool_direct"`` 走额外的预算与闸门检查（工具侧直发不可无限量，
        且必须仍处于当前代次）。预算在调用适配器**之前**扣减：调用已开始后抛异常
        的消息可能已经送达，重试会造成重复发送。

        失败时按「能否确定未提交」分类，这是不重试语义的依据：
        ``sender`` 不可调用或返回 ``False``（宿主明确表示无可达目标）→
        ``FAILED_BEFORE_SUBMIT``（确定未提交）；调用中抛异常 →
        ``UNKNOWN``（可能已达，禁止重试）；预算耗尽或闸门拒绝 → ``SUPPRESSED``。
        直发文本仅在 DELIVERED/UNKNOWN 时记账，供上层去重最终回复。
        """
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
            if self._direct_fail_count >= self._max_direct_sends:
                # 退还预算后此处是唯一的界：确定未提交不消耗 _direct_send_count，
                # 若不另计失败次数，不可达目标会被无限重试。
                return OutboundResult(
                    SendOutcome(SendStatus.SUPPRESSED, "direct send failure budget exhausted")
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
            if is_direct:
                # 退还调用前扣掉的预算：确定未提交，没有「可能已送达」的重复风险。
                # 不退还会让 direct_send_count 计入一条 direct_texts 里没有的消息，
                # 上层据此把「群里一个字没收到」当成已直发，扣配额并谎报成功。
                self._direct_send_count -= 1
                self._direct_fail_count += 1
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
