"""公开宿主 API 的判断调用、识图直调、Provider 选择与历史读取。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .host_stubs import load_package

PACKAGE_NAME = "selfreply_adapters_test_package"


def _load_adapters():
    return load_package(PACKAGE_NAME, "adapters")


@pytest.fixture(scope="module")
def bridge():
    from .host_stubs import install_astrbot_stubs

    install_astrbot_stubs()
    return _load_adapters().AstrBotBridge


async def test_direct_llm_body_type_error_is_not_retried(bridge) -> None:
    from types import SimpleNamespace

    calls = 0

    class Provider:
        async def text_chat(self, **kwargs):
            nonlocal calls
            calls += 1
            raise TypeError("body boom")

    ctx = SimpleNamespace(get_provider_by_id=lambda pid: Provider())
    with pytest.raises(TypeError, match="body boom"):
        await bridge(ctx).llm_generate_direct(provider_id="p1", prompt="hi", image_urls=["u1"])
    assert calls == 1


async def test_llm_generate_rejects_unsupported_images(bridge) -> None:
    from types import SimpleNamespace

    async def llm_generate(chat_provider_id, prompt):
        return "ok"

    ctx = SimpleNamespace(llm_generate=llm_generate)
    with pytest.raises(TypeError, match="image_urls"):
        await bridge(ctx).llm_generate(provider_id="p1", prompt="hi", image_urls=["u1"])


async def test_llm_generate_full_kwargs(bridge) -> None:
    from types import SimpleNamespace

    captured: dict[str, Any] = {}

    async def llm_generate(**kwargs):
        captured.update(kwargs)
        return "ok"

    ctx = SimpleNamespace(llm_generate=llm_generate)
    result = await bridge(ctx).llm_generate(
        provider_id="p1",
        prompt="hi",
        system_prompt="sys",
        temperature=0.3,
        max_tokens=50,
        image_urls=["u1"],
    )
    assert result == "ok"
    assert captured["chat_provider_id"] == "p1"
    assert captured["prompt"] == "hi"
    assert captured["system_prompt"] == "sys"
    assert captured["temperature"] == 0.3
    assert captured["max_tokens"] == 50
    assert captured["image_urls"] == ["u1"]


async def test_llm_generate_direct_requires_provider_id(bridge) -> None:
    from types import SimpleNamespace

    with pytest.raises(RuntimeError, match="未指定图片解析 Provider"):
        await bridge(SimpleNamespace()).llm_generate_direct(provider_id="  ", prompt="hi")


async def test_llm_generate_direct_requires_image_urls(bridge) -> None:
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="至少需要一个 image_url"):
        await bridge(SimpleNamespace()).llm_generate_direct(provider_id="p1", prompt="hi")


async def test_llm_generate_direct_via_context(bridge) -> None:
    from types import SimpleNamespace

    captured: dict[str, Any] = {}

    class Provider:
        async def text_chat(self, **kwargs):
            captured.update(kwargs)
            return "direct"

    def get_provider_by_id(pid):
        return Provider() if pid == "p1" else None

    ctx = SimpleNamespace(get_provider_by_id=get_provider_by_id)
    result = await bridge(ctx).llm_generate_direct(
        provider_id="p1",
        prompt="hi",
        image_urls=["u1"],
        temperature=0.7,
        max_tokens=128,
    )
    assert result == "direct"
    assert captured["image_urls"] == ["u1"]
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 128


async def test_llm_generate_direct_rejects_bad_provider(bridge) -> None:
    from types import SimpleNamespace

    def get_provider_by_id(pid):
        return None

    ctx = SimpleNamespace(get_provider_by_id=get_provider_by_id)
    with pytest.raises(RuntimeError, match="Provider 不可用"):
        await bridge(ctx).llm_generate_direct(provider_id="p1", prompt="hi", image_urls=["u1"])


async def test_llm_generate_direct_rejects_images_unsupported(bridge) -> None:
    from types import SimpleNamespace

    class Provider:
        async def text_chat(self, *, prompt, contexts, system_prompt=""):
            return "no-images"

    ctx = SimpleNamespace(get_provider_by_id=lambda pid: Provider())
    with pytest.raises(TypeError, match="image_urls"):
        await bridge(ctx).llm_generate_direct(provider_id="p1", prompt="hi", image_urls=["u1"])


async def test_resolve_provider_id_preferred_wins(bridge) -> None:
    from types import SimpleNamespace

    assert await bridge(SimpleNamespace()).resolve_provider_id("umo1", "p9") == "p9"


async def test_resolve_provider_id_via_current_chat(bridge) -> None:
    from types import SimpleNamespace

    async def get_current_chat_provider_id(umo):
        return "p-curr"

    ctx = SimpleNamespace(get_current_chat_provider_id=get_current_chat_provider_id)
    assert await bridge(ctx).resolve_provider_id("umo1", "") == "p-curr"


async def test_resolve_provider_id_returns_empty_when_current_provider_is_missing(bridge) -> None:
    from types import SimpleNamespace

    ProviderNotFoundError = type(
        "ProviderNotFoundError",
        (Exception,),
        {"__module__": "astrbot.core.exceptions"},
    )

    async def get_current_chat_provider_id(umo: str) -> str:
        raise ProviderNotFoundError("missing current provider")

    ctx = SimpleNamespace(get_current_chat_provider_id=get_current_chat_provider_id)
    assert await bridge(ctx).resolve_provider_id("umo1", "") == ""


async def test_resolve_provider_id_propagates_current_provider_errors(bridge) -> None:
    from types import SimpleNamespace

    async def get_current_chat_provider_id(umo: str) -> str:
        raise OSError("provider storage unavailable")

    ctx = SimpleNamespace(get_current_chat_provider_id=get_current_chat_provider_id)

    with pytest.raises(OSError, match="provider storage unavailable"):
        await bridge(ctx).resolve_provider_id("umo1", "")


async def test_read_history_no_manager(bridge) -> None:
    from types import SimpleNamespace

    assert await bridge(SimpleNamespace()).read_astrbot_history("umo1", limit=5) == []


async def test_read_history_empty_cid(bridge) -> None:
    from types import SimpleNamespace

    class Manager:
        async def get_curr_conversation_id(self, umo):
            return ""

    ctx = SimpleNamespace(conversation_manager=Manager())
    assert await bridge(ctx).read_astrbot_history("umo1", limit=5) == []


async def test_read_history_filters_and_maps(bridge) -> None:
    from types import SimpleNamespace

    # before limit 放最前：limit=5 时被窗口切掉（截断语义），limit=6 时完整遍历
    history = json.dumps(
        [
            {"role": "user", "content": "before limit"},
            {"role": "system", "content": "skip me"},
            "not-a-dict",
            {"role": "user", "content": ""},
            {"role": "user", "content": "hello", "sender_id": "u1"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}], "name": "Bot"},
        ]
    )
    Conversation = type("Conversation", (), {"history": history})

    class Manager:
        async def get_curr_conversation_id(self, umo):
            return "cid1"

        async def get_conversation(self, umo, cid):
            return Conversation()

    ctx = SimpleNamespace(conversation_manager=Manager())
    # limit=6 全量遍历：system（无效 role）、not-a-dict、空 content 三个过滤
    # 分支全部真实执行，before limit 也进窗口
    records = await bridge(ctx).read_astrbot_history("umo1", limit=6)
    assert len(records) == 3
    assert records[0].role == "user"
    assert records[1].role == "user"
    assert records[1].name == "用户"
    assert records[2].role == "assistant"
    assert records[2].name == "Bot"
    assert records[2].at == 0.0
    # limit=5 收缩窗口：before limit 被截断，无效项过滤后只剩末尾有效记录
    records2 = await bridge(ctx).read_astrbot_history("umo1", limit=5)
    assert len(records2) == 2


@pytest.mark.parametrize(
    "history",
    [
        pytest.param("{broken json", id="parse_error"),
        pytest.param({"not": "list"}, id="non_list_history"),
    ],
)
async def test_read_history_malformed_history_returns_empty(bridge, history) -> None:
    """坏 JSON 与非 list history 都退化为空历史，不让读取失败扩散。"""
    from types import SimpleNamespace

    class Conversation:
        pass

    Conversation.history = history

    class Manager:
        async def get_curr_conversation_id(self, umo):
            return "cid1"

        async def get_conversation(self, umo, cid):
            return Conversation()

    ctx = SimpleNamespace(conversation_manager=Manager())
    assert await bridge(ctx).read_astrbot_history("umo1", limit=5) == []


async def test_read_history_non_positive_limit_returns_nothing(bridge) -> None:
    """零条请求返回空，避免 -0 切片意外读取全量历史。"""
    from types import SimpleNamespace

    history = json.dumps(
        [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "user", "content": "three"},
        ]
    )
    Conversation = type("Conversation", (), {"history": history})

    class Manager:
        async def get_curr_conversation_id(self, umo):
            return "cid1"

        async def get_conversation(self, umo, cid):
            return Conversation()

    ctx = SimpleNamespace(conversation_manager=Manager())
    assert await bridge(ctx).read_astrbot_history("umo1", limit=0) == []
    assert await bridge(ctx).read_astrbot_history("umo1", limit=-1) == []
    assert len(await bridge(ctx).read_astrbot_history("umo1", limit=2)) == 2
