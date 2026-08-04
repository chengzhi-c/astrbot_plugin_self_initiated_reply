"""AstrBotBridge 补盲测试（0.8.4 批次3：低覆盖模块）。

覆盖 adapters.py 的全部兼容分支：_supported_kwargs 异常回退、_call_compat
的 minimal 降级语义、_call_first_supported 的调用形态探测、
llm_generate / llm_generate_direct / resolve_provider_id /
read_astrbot_history 的宿主接口差异分支。
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "selfreply_adapters_test_package"


def _load_adapters():
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package
    return importlib.import_module(f"{PACKAGE_NAME}.adapters")


@pytest.fixture(scope="module")
def bridge():
    from .host_stubs import install_astrbot_stubs

    install_astrbot_stubs()
    return _load_adapters().AstrBotBridge


# ============================================================================
# _supported_kwargs
# ============================================================================


def test_supported_kwargs_filters_unknown(bridge) -> None:
    def target(a: int, b: int) -> None:  # pragma: no cover - 仅签名
        pass

    kwargs = {"x": 1, "y": 2}
    result = bridge._supported_kwargs(target, kwargs, {"x": ("x", "a"), "y": ("y", "b")})
    assert result == {"a": 1, "b": 2}


def test_supported_kwargs_returns_all_for_varkw(bridge) -> None:
    def target(**kwargs) -> None:  # pragma: no cover - 仅签名
        pass

    kwargs = {"a": 1, "b": 2}
    assert bridge._supported_kwargs(target, kwargs) == kwargs


def test_supported_kwargs_returns_all_when_signature_unavailable(bridge) -> None:
    class NoSignature:
        @property
        def __signature__(self):  # noqa: N802 - 模拟 inspect 失败
            raise TypeError("no signature")

    assert bridge._supported_kwargs(NoSignature(), {"a": 1}) == {"a": 1}


# ============================================================================
# _call_compat
# ============================================================================


async def test_call_compat_direct_call(bridge) -> None:
    calls: list[dict[str, Any]] = []

    async def target(**kwargs):
        calls.append(kwargs)
        return "ok"

    result = await bridge._call_compat(target, kwargs={"a": 1}, minimal_kwargs={"a": 0})
    assert result == "ok"
    assert calls == [{"a": 1}]


async def test_call_compat_binds_via_aliases_without_fallback(bridge) -> None:
    calls: list[dict[str, Any]] = []

    def target(a: int) -> str:
        calls.append(a)
        return f"a={a}"

    result = await bridge._call_compat(
        target, kwargs={"b": 2}, minimal_kwargs={"a": 1}, aliases={"b": ("b", "a")}
    )
    # kwargs 的 b 映射到 a（受支持），直接成功，不降级
    assert result == "a=2"
    assert calls == [2]


async def test_call_compat_minimal_rebind_succeeds(bridge) -> None:
    """kwargs 全部不被支持时，用 minimal_kwargs 重试成功。"""

    calls: list[dict[str, Any]] = []

    def target(a: int) -> str:
        calls.append(a)
        return f"a={a}"

    result = await bridge._call_compat(target, kwargs={"b": 2}, minimal_kwargs={"a": 1})
    assert result == "a=1"
    assert calls == [1]


async def test_call_compat_minimal_same_as_call_raises(bridge) -> None:
    """minimal == call_kwargs 时不再重试，直接上抛。"""
    calls: list[Any] = []

    def target(a: int) -> str:  # pragma: no cover - 不应被调用
        calls.append(a)
        return f"a={a}"

    with pytest.raises(TypeError, match="missing a required argument"):
        await bridge._call_compat(target, kwargs={"b": 2}, minimal_kwargs={"b": 2})
    # 若误实现为继续调用 func(**minimal)（{}），缺参 TypeError 消息与原始 bind
    # 错误相同，仅靠 match 无法区分；必须断言 target 未被调用
    assert calls == []


async def test_call_compat_minimal_also_type_error_raises_original(bridge) -> None:
    """minimal 重试也抛 TypeError（函数体内）时，上抛原始 bind 错误。"""
    calls: list[Any] = []

    def target(a: int) -> str:
        calls.append(a)
        raise TypeError("inner boom")

    with pytest.raises(TypeError, match="missing a required argument"):
        await bridge._call_compat(target, kwargs={"z": 1}, minimal_kwargs={"a": 1})
    # match 区分"原始 bind 错误"（missing 'z'）与函数体内 TypeError（inner boom）；
    # calls == [1] 证明 minimal 重试确实执行过（与上例零调用形成对照）
    assert calls == [1]


async def test_call_compat_signature_unavailable_direct_call(bridge) -> None:
    class NoSignature:
        def __call__(self, **kwargs):
            return "called"

        @property
        def __signature__(self):
            raise ValueError("no signature")

    result = await bridge._call_compat(NoSignature(), kwargs={"a": 1}, minimal_kwargs={"a": 0})
    assert result == "called"


# ============================================================================
# _method_call_options / _call_first_supported
# ============================================================================


def test_method_call_options_signature_unavailable(bridge) -> None:
    class NoSignature:
        @property
        def __signature__(self):
            raise TypeError("no signature")

    options = bridge._method_call_options(NoSignature(), "umo1")
    assert options == [((), {"umo": "umo1"}), (("umo1",), {}), ((), {})]


def test_method_call_options_varkw(bridge) -> None:
    def target(**kwargs) -> None:  # pragma: no cover - 仅签名
        pass

    assert bridge._method_call_options(target, "umo1") == [((), {"umo": "umo1"}), ((), {})]


def test_method_call_options_named_umo(bridge) -> None:
    def target(umo: str) -> None:  # pragma: no cover - 仅签名
        pass

    assert bridge._method_call_options(target, "umo1") == [((), {"umo": "umo1"}), ((), {})]


def test_method_call_options_positional(bridge) -> None:
    def target(uid: str) -> None:  # pragma: no cover - 仅签名
        pass

    assert bridge._method_call_options(target, "umo1") == [(("umo1",), {}), ((), {})]


def test_method_call_options_no_params(bridge) -> None:
    def target() -> None:  # pragma: no cover - 仅签名
        pass

    assert bridge._method_call_options(target, "umo1") == [((), {})]


async def test_call_first_supported_retries_on_type_error(bridge) -> None:
    calls: list[str] = []

    async def target(umo=None):
        calls.append(str(umo))
        if len(calls) == 1:
            raise TypeError("first form unsupported")
        return "resolved"

    result = await bridge._call_first_supported(target, "umo1", "probe")
    assert result == "resolved"
    assert calls == ["umo1", "None"]


async def test_call_first_supported_raises_business_error(bridge) -> None:
    def target(**kwargs):
        raise OSError("db down")

    with pytest.raises(OSError, match="db down"):
        await bridge._call_first_supported(target, "umo1", "probe")


async def test_call_first_supported_all_unsupported_returns_none(bridge) -> None:
    # 每个形态都能绑定成功，但函数体抛 TypeError 使该形态被判定为不支持，
    # 全部形态耗尽后返回 None（语义：找不到可执行形态）
    def target(required: int) -> str:
        raise TypeError("body boom")

    assert await bridge._call_first_supported(target, "umo1", "probe") is None


# ============================================================================
# llm_generate
# ============================================================================


async def test_llm_generate_requires_context_method(bridge) -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace()
    with pytest.raises(RuntimeError, match="llm_generate"):
        await bridge(ctx).llm_generate(provider_id="p1", prompt="hi")


async def test_llm_generate_rejects_unsupported_images(bridge) -> None:
    from types import SimpleNamespace

    async def llm_generate(chat_provider_id, prompt):
        return "ok"

    ctx = SimpleNamespace(llm_generate=llm_generate)
    with pytest.raises(RuntimeError, match="不支持图片输入"):
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


# ============================================================================
# llm_generate_direct
# ============================================================================


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

    async def get_provider_by_id(pid):
        return Provider() if pid == "p1" else None

    ctx = SimpleNamespace(get_provider_by_id=get_provider_by_id)
    result = await bridge(ctx).llm_generate_direct(provider_id="p1", prompt="hi", image_urls=["u1"])
    assert result == "direct"
    assert captured["image_urls"] == ["u1"]


async def test_llm_generate_direct_passes_temperature_and_max_tokens(bridge) -> None:
    from types import SimpleNamespace

    captured: dict[str, Any] = {}

    class Provider:
        async def text_chat(self, **kwargs):
            captured.update(kwargs)
            return "direct"

    async def get_provider_by_id(pid):
        return Provider() if pid == "p1" else None

    ctx = SimpleNamespace(get_provider_by_id=get_provider_by_id)
    result = await bridge(ctx).llm_generate_direct(
        provider_id="p1", prompt="hi", image_urls=["u1"], temperature=0.7, max_tokens=128
    )
    assert result == "direct"
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 128


async def test_llm_generate_direct_via_provider_manager(bridge) -> None:
    from types import SimpleNamespace

    class Provider:
        def text_chat(self, **kwargs):
            return "managed"

    class Manager:
        async def get_provider_by_id(self, pid):
            return Provider()

    ctx = SimpleNamespace(provider_manager=Manager())
    result = await bridge(ctx).llm_generate_direct(provider_id="p1", prompt="hi", image_urls=["u1"])
    assert result == "managed"


async def test_llm_generate_direct_via_inst_map(bridge) -> None:
    from types import SimpleNamespace

    class Provider:
        def text_chat(self, **kwargs):
            return "instmap"

    ctx = SimpleNamespace(provider_manager=SimpleNamespace(inst_map={"p1": Provider()}))
    result = await bridge(ctx).llm_generate_direct(provider_id="p1", prompt="hi", image_urls=["u1"])
    assert result == "instmap"


async def test_llm_generate_direct_rejects_bad_provider(bridge) -> None:
    from types import SimpleNamespace

    async def get_provider_by_id(pid):
        return None

    ctx = SimpleNamespace(get_provider_by_id=get_provider_by_id)
    with pytest.raises(RuntimeError, match="Provider 不可用"):
        await bridge(ctx).llm_generate_direct(provider_id="p1", prompt="hi", image_urls=["u1"])


async def test_llm_generate_direct_rejects_images_unsupported(bridge) -> None:
    from types import SimpleNamespace

    class Provider:
        def text_chat(self, prompt, contexts):
            return "no-images"

    ctx = SimpleNamespace(get_provider_by_id=lambda pid: Provider())
    with pytest.raises(RuntimeError, match="不支持图片输入"):
        await bridge(ctx).llm_generate_direct(provider_id="p1", prompt="hi", image_urls=["u1"])


# ============================================================================
# resolve_provider_id
# ============================================================================


async def test_resolve_provider_id_preferred_wins(bridge) -> None:
    from types import SimpleNamespace

    assert await bridge(SimpleNamespace()).resolve_provider_id("umo1", "p9") == "p9"


async def test_resolve_provider_id_via_current_chat(bridge) -> None:
    from types import SimpleNamespace

    async def get_current_chat_provider_id(umo):
        return "p-curr"

    ctx = SimpleNamespace(get_current_chat_provider_id=get_current_chat_provider_id)
    assert await bridge(ctx).resolve_provider_id("umo1", "") == "p-curr"


async def test_resolve_provider_id_via_using_id(bridge) -> None:
    from types import SimpleNamespace

    class ProviderId:
        async def get_using_provider_id(self, umo):
            return "p-using"

    ctx = SimpleNamespace(get_using_provider_id=ProviderId().get_using_provider_id)
    assert await bridge(ctx).resolve_provider_id("umo1", "") == "p-using"


async def test_resolve_provider_id_via_using_provider_meta(bridge) -> None:
    from types import SimpleNamespace

    class UsingProvider:
        def meta(self):
            return {"id": "p-meta"}

    ctx = SimpleNamespace(get_using_provider=lambda: UsingProvider())
    assert await bridge(ctx).resolve_provider_id("umo1", "") == "p-meta"


async def test_resolve_provider_id_empty_when_unavailable(bridge) -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace()
    assert await bridge(ctx).resolve_provider_id("umo1", "") == ""


async def test_provider_id_from_meta_forms(bridge) -> None:
    from types import SimpleNamespace

    assert await bridge._provider_id_from_meta(None) == ""
    assert await bridge._provider_id_from_meta(SimpleNamespace(meta=lambda: {"id": "d1"})) == "d1"
    assert (
        await bridge._provider_id_from_meta(SimpleNamespace(meta=lambda: SimpleNamespace(id="o1")))
        == "o1"
    )

    class BrokenMeta:
        def meta(self):
            raise OSError("meta broken")

    assert await bridge._provider_id_from_meta(BrokenMeta()) == ""


# ============================================================================
# read_astrbot_history
# ============================================================================


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


async def test_read_history_parse_error(bridge) -> None:
    from types import SimpleNamespace

    class Conversation:
        history = "{broken json"

    class Manager:
        async def get_curr_conversation_id(self, umo):
            return "cid1"

        async def get_conversation(self, umo, cid):
            return Conversation()

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
    assert records[0].sender_id == ""
    assert records[1].role == "user"
    assert records[1].name == "用户"
    assert records[1].sender_id == "u1"
    assert records[2].role == "assistant"
    assert records[2].name == "Bot"
    assert records[2].at == 0.0
    # limit=5 收缩窗口：before limit 被截断，无效项过滤后只剩末尾有效记录
    records2 = await bridge(ctx).read_astrbot_history("umo1", limit=5)
    assert len(records2) == 2
    assert records2[0].sender_id == "u1"


async def test_read_history_non_list_history(bridge) -> None:
    from types import SimpleNamespace

    class Conversation:
        history = {"not": "list"}

    class Manager:
        async def get_curr_conversation_id(self, umo):
            return "cid1"

        async def get_conversation(self, umo, cid):
            return Conversation()

    ctx = SimpleNamespace(conversation_manager=Manager())
    assert await bridge(ctx).read_astrbot_history("umo1", limit=5) == []
