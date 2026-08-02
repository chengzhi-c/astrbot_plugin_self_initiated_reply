from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "selfreply_outbound_test_package"


def _load_gateway():
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package
    return importlib.import_module(f"{PACKAGE_NAME}.outbound")


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


def test_tool_direct_exception_is_unknown_and_still_consumes_budget() -> None:
    outbound = _load_gateway()

    async def sender(_message):
        raise RuntimeError("adapter disconnected")

    gateway = outbound.OutboundGateway(sender, max_direct_sends=1)
    first = asyncio.run(gateway.send(SimpleNamespace(type="tool_direct_result"), kind="tool_direct"))
    second = asyncio.run(gateway.send(SimpleNamespace(type="tool_direct_result"), kind="tool_direct"))

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


def test_missing_sender_fails_before_submit() -> None:
    outbound = _load_gateway()

    result = asyncio.run(outbound.OutboundGateway(None).send("message"))

    assert result.outcome.status is outbound.SendStatus.FAILED_BEFORE_SUBMIT
    assert result.submitted is False
