from __future__ import annotations

import asyncio
from types import SimpleNamespace

from .host_stubs import install_astrbot_stubs, load_package

PACKAGE_NAME = "selfreply_outbound_test_package"


def _load_gateway():
    install_astrbot_stubs()  # 本文件不依赖其他测试先跑：outbound 经 models 引 astrbot.api
    return load_package(PACKAGE_NAME, "outbound")


def _models():
    install_astrbot_stubs()
    return load_package(PACKAGE_NAME, "models")


def test_tool_direct_send_budget_is_consumed_before_adapter_call() -> None:
    outbound = _load_gateway()
    ledger = _models().AttemptLedger()
    sent: list[str] = []

    class Message:
        type = "tool_direct_result"

        def get_plain_text(self):
            return "工具消息"

    async def sender(_message):
        sent.append("event")
        return None

    async def context_sender(_message):
        sent.append("context")
        return True

    gateway = outbound.OutboundGateway(sender, max_direct_sends=2, ledger=ledger)
    first = asyncio.run(gateway.send(Message(), kind="tool_direct"))
    second = asyncio.run(
        gateway.send(Message(), kind="tool_direct", sender=context_sender, record_text=False)
    )
    third = asyncio.run(gateway.send(Message(), kind="tool_direct"))

    assert first.outcome.status.value == "delivered"
    assert second.outcome.status.value == "delivered"
    assert third.outcome.status.value == "suppressed"
    assert ledger.direct_send_count == 2
    assert sent == ["event", "context"]
    assert ledger.direct_texts == ("工具消息",)


def test_tool_direct_false_refunds_budget_and_keeps_count_in_sync() -> None:
    """sender 确定未提交时退还预算并校正内部计数，避免误走已直发分支扣除配额。"""
    outbound = _load_gateway()
    ledger = _models().AttemptLedger()
    calls: list[str] = []

    class Message:
        def get_plain_text(self) -> str:
            return "工具消息"

    async def sender(_message):
        calls.append("called")
        # 第一次失败（确定未提交），之后成功：失败不得吃掉直发预算
        return False if len(calls) == 1 else None

    gateway = outbound.OutboundGateway(sender, max_direct_sends=2, ledger=ledger)

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
    assert ledger.direct_send_count == 2
    assert ledger.direct_texts == ("工具消息", "工具消息")


def test_tool_direct_failures_are_bounded_after_refund() -> None:
    """退还预算不得换来无界重试：失败次数自身也要有上限。

    退还后 ``_direct_send_count`` 不再随失败增长，若不另计失败次数，不可达目标
    会被反复调用，界就外借给了宿主迭代上限（契约要求边界由插件自身持有）。
    """
    outbound = _load_gateway()
    ledger = _models().AttemptLedger()
    calls: list[str] = []

    async def sender(_message):
        calls.append("called")
        return False

    gateway = outbound.OutboundGateway(sender, max_direct_sends=2, ledger=ledger)
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
    assert ledger.direct_send_count == 0


def test_tool_direct_exception_is_unknown_and_still_consumes_budget() -> None:
    outbound = _load_gateway()
    ledger = _models().AttemptLedger()

    async def sender(_message):
        raise RuntimeError("adapter disconnected")

    gateway = outbound.OutboundGateway(sender, max_direct_sends=1, ledger=ledger)
    first = asyncio.run(
        gateway.send(SimpleNamespace(type="tool_direct_result"), kind="tool_direct")
    )
    second = asyncio.run(
        gateway.send(SimpleNamespace(type="tool_direct_result"), kind="tool_direct")
    )

    assert first.outcome.status.value == "unknown"
    assert second.outcome.status.value == "suppressed"
    assert ledger.direct_send_count == 1


def test_none_result_is_delivered_on_both_channels() -> None:
    """``None`` 返回值两条出口都归 DELIVERED。

    event.send 与 Context.send_message 正常完成都返回 None（True 同样代表送达）；
    记成未提交会让 assistant 历史少一条、下一次决策看不到自己说过什么。
    """
    outbound = _load_gateway()

    async def sender(_message):
        return None

    event_result = asyncio.run(outbound.OutboundGateway(sender).send("event"))
    context_result = asyncio.run(outbound.OutboundGateway(sender).send("context"))

    assert event_result.outcome.status is outbound.SendStatus.DELIVERED
    assert context_result.outcome.status is outbound.SendStatus.DELIVERED


def test_sender_false_is_failed_before_submit() -> None:
    """``False``（如 Context.send_message 未找到平台）是确定未提交，不得消耗配额。"""

    outbound = _load_gateway()

    async def sender(_message):
        return False

    gateway = outbound.OutboundGateway(sender)
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


def test_tool_direct_unknown_is_retained_in_the_attempt_ledger() -> None:
    """A tool send that enters the adapter retains UNKNOWN evidence for the pipeline."""
    outbound = _load_gateway()
    models = _models()

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


def test_adapter_exception_detail_is_redacted() -> None:
    """adapter 异常的 detail 必须经 ``redact_exc_text``，不得原样带出 URL 凭证。

    缺陷形态：``except Exception`` 用 ``safe_exc_text(exc)``，它只防 ``__str__``
    再抛，不脱敏。该 detail 会被 ``delivery._send_via_context`` 的 UNKNOWN 分支
    写进 WARNING 日志，而平台适配器异常串常形如
    ``... for url 'https://api.example.com/bot<token>/sendMessage?token=...'``。
    判断/生成/投递/webapi 各异常路径都已脱敏，产出 detail 的网关是漏掉的那一个。
    """
    outbound = _load_gateway()

    async def sender(_message):
        raise RuntimeError(
            "Client error '401' for url "
            "'https://api.example.com/bot123/sendMessage?token=SECRET_TOKEN"
            "&Signature=SECRET_SIGNATURE'"
        )

    gateway = outbound.OutboundGateway(sender)
    result = asyncio.run(gateway.send("message"))

    assert result.outcome.status is outbound.SendStatus.UNKNOWN
    detail = result.outcome.detail
    assert "SECRET_TOKEN" not in detail, f"detail 泄漏凭证：{detail}"
    assert "SECRET_SIGNATURE" not in detail, f"detail 泄漏凭证：{detail}"
    assert "api.example.com" in detail, "脱敏不应把整条 URL 抹掉，排障需要主机名"


def test_unstringable_adapter_exception_still_classifies_and_records() -> None:
    """异常文本提取失败时退化为类型名，保留投递分类和账本终态。"""
    outbound = _load_gateway()
    models = _models()

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
        f"detail={result.outcome.detail!r}，未经 safe_exc_text 退化，"
        "重新引入二次抛出后异常会逃出 gateway"
    )
    assert result.outcome.status is models.SendStatus.UNKNOWN, (
        f"adapter 已调用过，判成 {result.outcome.status!r} 会不消耗冷却而重发"
    )
    assert ledger.attempts[0].state is models.AttemptState.UNKNOWN
    assert ledger.has_submission is True, "异常逃出会让这次投递不记账"
