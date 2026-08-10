from __future__ import annotations

import asyncio
from types import SimpleNamespace

from .host_stubs import load_package

PACKAGE_NAME = "selfreply_outbound_test_package"


def _load_gateway():
    return load_package(PACKAGE_NAME, "outbound")


def test_tool_direct_send_budget_is_consumed_before_adapter_call() -> None:
    outbound = _load_gateway()
    sent: list[str] = []

    class Message:
        type = "tool_direct_result"

        def get_plain_text(self):
            return "工具消息"

    async def sender(_message):
        sent.append("sent")
        return None

    gateway = outbound.OutboundGateway(sender, max_direct_sends=2)
    first = asyncio.run(gateway.send(Message(), kind="tool_direct"))
    second = asyncio.run(gateway.send(Message(), kind="tool_direct"))
    third = asyncio.run(gateway.send(Message(), kind="tool_direct"))

    assert first.outcome.status.value == "delivered"
    assert second.outcome.status.value == "delivered"
    assert third.outcome.status.value == "suppressed"
    assert gateway.direct_send_count == 2
    assert len(sent) == 2
    assert gateway.direct_texts == ("工具消息", "工具消息")


def test_tool_direct_false_refunds_budget_and_keeps_count_in_sync() -> None:
    """红线：sender 返 ``False``（确定未提交）必须退还直发预算。

    不退还时 ``direct_send_count == 1`` 而 ``direct_texts == ()``，两者失去同源；
    上层 ``main.py`` 的 ``not reply and not direct_send_count`` 因计数非零而不短路，
    ``delivery.py`` 走"仅有工具直发"分支，于是**扣掉当日配额、推进冷却与观察窗口，
    并回报"已通过工具主动回复。"，而群里一个字都没收到**。

    变异锚定：删掉 ``outbound.py`` 里 ``self._direct_send_count -= 1`` 这一行，
    本用例第一条断言即红（计数变 1）。断言的是精确值而非区间——区间断言在这里
    恰好两边都成立，正是此前 clamp 假绿的同一类陷阱。
    """
    outbound = _load_gateway()
    calls: list[str] = []

    async def sender(_message):
        calls.append("called")
        return False

    gateway = outbound.OutboundGateway(sender, max_direct_sends=2)
    result = asyncio.run(
        gateway.send(SimpleNamespace(type="tool_direct_result"), kind="tool_direct")
    )

    assert result.outcome.status.value == "failed_before_submit"
    assert gateway.direct_send_count == 0
    assert gateway.direct_texts == ()
    assert len(calls) == 1


def test_tool_direct_failures_are_bounded_after_refund() -> None:
    """退还预算不得换来无界重试：失败次数自身也要有上限。

    退还后 ``_direct_send_count`` 不再随失败增长，若不另计失败次数，不可达目标
    会被反复调用，界就外借给了宿主迭代上限（0.9.4 §5 明确禁止这种依赖）。

    变异锚定：删掉 ``_direct_fail_count >= self._max_direct_sends`` 那个早退，
    ``len(calls)`` 会从 2 变成 4，本用例红。
    """
    outbound = _load_gateway()
    calls: list[str] = []

    async def sender(_message):
        calls.append("called")
        return False

    gateway = outbound.OutboundGateway(sender, max_direct_sends=2)
    statuses = [
        asyncio.run(
            gateway.send(SimpleNamespace(type="tool_direct_result"), kind="tool_direct")
        ).outcome.status.value
        for _ in range(4)
    ]

    assert statuses == [
        "failed_before_submit",
        "failed_before_submit",
        "suppressed",
        "suppressed",
    ]
    assert len(calls) == 2
    assert gateway.direct_send_count == 0


def test_tool_direct_exception_is_unknown_and_still_consumes_budget() -> None:
    outbound = _load_gateway()

    async def sender(_message):
        raise RuntimeError("adapter disconnected")

    gateway = outbound.OutboundGateway(sender, max_direct_sends=1)
    first = asyncio.run(
        gateway.send(SimpleNamespace(type="tool_direct_result"), kind="tool_direct")
    )
    second = asyncio.run(
        gateway.send(SimpleNamespace(type="tool_direct_result"), kind="tool_direct")
    )

    assert first.outcome.status.value == "unknown"
    assert second.outcome.status.value == "suppressed"
    assert gateway.direct_send_count == 1


def test_context_none_result_is_unknown_while_event_none_is_delivered() -> None:
    outbound = _load_gateway()

    async def sender(_message):
        return None

    event_gateway = outbound.OutboundGateway(sender)
    context_gateway = outbound.OutboundGateway(
        sender,
        none_status=outbound.SendStatus.UNKNOWN,
    )

    event_result = asyncio.run(event_gateway.send("event"))
    context_result = asyncio.run(context_gateway.send("context"))

    assert event_result.outcome.status is outbound.SendStatus.DELIVERED
    assert context_result.outcome.status is outbound.SendStatus.UNKNOWN


def test_sender_false_is_failed_before_submit() -> None:
    """``False``（如 Context.send_message 未找到平台）是确定未提交，不得消耗配额。"""

    outbound = _load_gateway()

    async def sender(_message):
        return False

    gateway = outbound.OutboundGateway(
        sender,
        none_status=outbound.SendStatus.UNKNOWN,
    )
    result = asyncio.run(gateway.send("message"))

    assert result.outcome.status is outbound.SendStatus.FAILED_BEFORE_SUBMIT
    assert result.submitted is False


def test_sender_exception_is_unknown() -> None:
    """send 抛异常可能已提交到适配器：UNKNOWN，不可重试，计入 submitted。"""

    outbound = _load_gateway()

    async def sender(_message):
        raise RuntimeError("adapter disconnected")

    gateway = outbound.OutboundGateway(sender)
    result = asyncio.run(gateway.send("message"))

    assert result.outcome.status is outbound.SendStatus.UNKNOWN
    assert result.submitted is True


def test_missing_sender_fails_before_submit() -> None:
    outbound = _load_gateway()

    result = asyncio.run(outbound.OutboundGateway(None).send("message"))

    assert result.outcome.status is outbound.SendStatus.FAILED_BEFORE_SUBMIT
    assert result.submitted is False
