from __future__ import annotations

import asyncio
from types import SimpleNamespace

from .host_stubs import install_astrbot_stubs, load_package

PACKAGE_NAME = "selfreply_outbound_test_package"


def _load_gateway():
    install_astrbot_stubs()  # 本文件不依赖其他测试先跑：outbound 经 models 引 astrbot.api
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
    assert gateway.ledger.direct_send_count == 2
    assert len(sent) == 2
    assert gateway.ledger.direct_texts == ("工具消息", "工具消息")


def test_tool_direct_false_refunds_budget_and_keeps_count_in_sync() -> None:
    """红线：sender 返 ``False``（确定未提交）必须退还直发预算。

    不退还时内部计数 ``_direct_send_count`` 停在 1、预算被一次失败白白吃掉：
    上层 ``main.py`` 的 ``not reply and not direct_send_count`` 因计数非零而不短路，
    ``delivery.py`` 走"仅有工具直发"分支，于是**扣掉当日配额、推进冷却与观察窗口，
    并回报"已通过工具主动回复。"，而群里一个字都没收到**。

    变异锚定：删掉 ``outbound.py`` 里 ``self._direct_send_count -= 1`` 这一行，
    本用例红——断言的是**内部计数**与「一次失败后仍能用满 ``max_direct_sends`` 次」
    这一行为，两者都随退还与否变化。

    注意不能只断言 ``ledger.direct_send_count``：它是按 attempt 终态派生的视图
    （只数 DELIVERED/UNKNOWN），失败的 attempt 本就不入账，删掉退还那行它也恒为 0
    （此前的假绿正是踩了这个不同源）。故这里同时钉内部计数与端到端预算行为。
    """
    outbound = _load_gateway()
    calls: list[str] = []

    class Message:
        def get_plain_text(self) -> str:
            return "工具消息"

    async def sender(_message):
        calls.append("called")
        # 第一次失败（确定未提交），之后成功：失败不得吃掉直发预算
        return False if len(calls) == 1 else None

    gateway = outbound.OutboundGateway(sender, max_direct_sends=2)

    first = asyncio.run(gateway.send(Message(), kind="tool_direct"))
    assert first.outcome.status.value == "failed_before_submit"
    assert gateway._direct_send_count == 0, "失败后内部计数未退还（ledger 视图恒 0，盖不住）"
    assert gateway._direct_fail_count == 1

    # 一次失败之后仍能用满 max_direct_sends 次：预算被失败吃掉时第 2 次就已 SUPPRESSED
    rest = [
        asyncio.run(gateway.send(Message(), kind="tool_direct")).outcome.status.value
        for _ in range(3)
    ]

    assert rest == ["delivered", "delivered", "suppressed"], (
        "失败未退还预算：本该用满 2 次的直发预算被一次失败吃掉"
    )
    assert len(calls) == 3, "适配器调用次数应等于 1 次失败 + 2 次成功"
    assert gateway.ledger.direct_send_count == 2
    assert gateway.ledger.direct_texts == ("工具消息", "工具消息")


def test_tool_direct_failures_are_bounded_after_refund() -> None:
    """退还预算不得换来无界重试：失败次数自身也要有上限。

    退还后 ``_direct_send_count`` 不再随失败增长，若不另计失败次数，不可达目标
    会被反复调用，界就外借给了宿主迭代上限（契约 §5 明确禁止这种依赖）。

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
    assert gateway.ledger.direct_send_count == 0


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
    assert gateway.ledger.direct_send_count == 1


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


def test_tool_direct_unknown_is_retained_in_the_attempt_ledger() -> None:
    """A tool send that enters the adapter retains UNKNOWN evidence for the pipeline."""
    outbound = _load_gateway()
    models = load_package(PACKAGE_NAME, "models")

    async def sender(_message):
        raise RuntimeError("adapter disconnected")

    class Message:
        type = "tool_direct_result"

        def get_plain_text(self) -> str:
            return "tool result"

    ledger = models.AttemptLedger()
    gateway = outbound.OutboundGateway(sender, max_direct_sends=1, ledger=ledger)
    result = asyncio.run(gateway.send(Message(), kind="tool_direct"))

    assert result.outcome.status is models.SendStatus.UNKNOWN
    assert ledger.direct_send_count == 1
    assert ledger.direct_texts == ("tool result",)
    assert ledger.attempts[0].state is models.AttemptState.UNKNOWN


def test_unstringable_adapter_exception_still_classifies_and_records() -> None:
    """``__str__`` 坏掉的 adapter 异常不得让异常逃出 gateway，也不得丢记账。

    缺陷形态：``except`` 块里 ``SendOutcome(status, str(exc))`` 会二次抛出，异常
    逃出 ``OutboundGateway.send`` → ledger 停在 in-flight、调用方最后一步
    ``str(exc)`` 也可能再抛 → 该次投递不记账 → 消息若其实已提交则重复发送。
    修法是 ``safe_exc_text``（``__str__`` 抛时退化为类型名）。

    变异锚定：把 ``outbound.py`` 的 ``safe_exc_text(exc)`` 换回 ``str(exc)``，
    本用例抛 ``ValueError("__str__ is broken")`` 而非返回 ``OutboundResult``。
    """
    outbound = _load_gateway()
    models = load_package(PACKAGE_NAME, "models")

    class UnstringableError(RuntimeError):
        def __str__(self) -> str:
            raise ValueError("__str__ is broken")

    async def sender(_message):
        raise UnstringableError

    ledger = models.AttemptLedger()
    gateway = outbound.OutboundGateway(sender, ledger=ledger)
    result = asyncio.run(gateway.send("message"))

    # 路径锚定：detail 必须来自安全取文本的退化值，证明走的是 safe_exc_text
    assert result.outcome.detail == "UnstringableError", (
        f"detail={result.outcome.detail!r}，未经 safe_exc_text 退化——"
        "重新引入二次抛出后异常会逃出 gateway"
    )
    assert result.outcome.status is models.SendStatus.UNKNOWN, (
        f"adapter 已调用过，判成 {result.outcome.status!r} 会不消耗冷却而重发"
    )
    assert ledger.attempts[0].state is models.AttemptState.UNKNOWN
    assert ledger.has_submission is True, "异常逃出会让这次投递不记账"
