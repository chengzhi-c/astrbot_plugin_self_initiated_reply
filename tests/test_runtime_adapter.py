"""runtime_adapter 契约与宿主私有符号收敛。

覆盖：AgentRuntimeCapabilities 校验（缺失即红、软模式告警）、窄方法出口、
call_event_hook 两分支、处理器注解运行时可解析（复现宿主 4.27.2 的
eval_str=True 加载路径）与 astrbot.core 私有层 import 泄漏守卫。
"""

from __future__ import annotations

import pytest

from .host_stubs import ROOT, base_runtime_capabilities, load_modules, production_py_files

PACKAGE_NAME = "selfreply_runtime_test_package"

_PRIVATE_IMPORT_RE = r"(^|\n)\s*(from|import)\s+astrbot\.core"
_SYMBOL_WHITELIST = {
    "runtime_adapter.py",
    "scripts/compat_check.py",
    "tests/host_stubs.py",
}


def _load_adapter():
    return load_modules(PACKAGE_NAME, "runtime_adapter")[0]


def test_runtime_adapter_validates_private_agent_capabilities() -> None:
    runtime = _load_adapter()

    class ToolSet:
        pass

    class BuildConfig:
        def __init__(self, **kwargs):
            self.values = kwargs

    async def build_main_agent(*, event, plugin_context, config, req, apply_reset):
        return (event, plugin_context, config, req, apply_reset)

    async def get_session_conv(event, context):
        return event, context

    async def run_agent(agent_runner, *, max_step, **kwargs):
        yield agent_runner, max_step, kwargs

    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(
            runtime,
            tool_set=ToolSet,
            build_config=BuildConfig,
            build_main_agent=build_main_agent,
            get_session_conv=get_session_conv,
            run_agent=run_agent,
        )
    )

    adapter.validate()
    assert isinstance(adapter.new_tool_set(), ToolSet)
    assert adapter.new_build_config(timeout=3).values == {"timeout": 3}


def test_runtime_adapter_reports_signature_mismatch() -> None:
    runtime = _load_adapter()

    async def incompatible(*, event, plugin_context, config, req):
        return None

    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, build_main_agent=incompatible)
    )

    with pytest.raises(RuntimeError, match="apply_reset"):
        adapter.validate()


def test_runtime_adapter_enforces_run_contract_params() -> None:
    """run_agent 缺少实际使用的运行参数时必须加载期失败。"""
    runtime = _load_adapter()

    async def run_agent(agent_runner, *, max_step):
        yield agent_runner, max_step

    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime, run_agent=run_agent))

    with pytest.raises(RuntimeError, match="show_tool_use"):
        adapter.validate()


def test_filter_final_tools_modes() -> None:
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))

    class Tool:
        def __init__(self, name: str):
            self.name = name

    class ToolSet:
        def __init__(self):
            self.tools = []

        def add_tool(self, tool):
            self.tools.append(tool)

        def remove_tool(self, name):
            self.tools = [t for t in self.tools if t.name != name]

    tool_set = ToolSet()
    tool_set.add_tool(Tool("send_message_to_user"))
    tool_set.add_tool(Tool("web_search"))
    req = type("Req", (), {"func_tool": tool_set})()

    assert adapter.filter_final_tools(req, keep=frozenset()) is True
    assert tool_set.tools == []

    # 无法枚举 -> fail closed
    bad_req = type("Req", (), {"func_tool": type("Bad", (), {"tools": None})()})()
    assert adapter.filter_final_tools(bad_req, keep=frozenset()) is False

    # 无工具集 -> 天然空，放行
    empty_req = type("Req", (), {"func_tool": None})()
    assert adapter.filter_final_tools(empty_req, keep=frozenset()) is True

    # denylist 模式：只移除指定工具，其余保留
    tool_set2 = ToolSet()
    tool_set2.add_tool(Tool("astr_kb_search"))
    tool_set2.add_tool(Tool("third_party_weather"))
    req2 = type("Req", (), {"func_tool": tool_set2})()
    assert (
        adapter.filter_final_tools(req2, drop=frozenset({"astr_kb_search", "create_future_task"}))
        is True
    )
    assert [t.name for t in tool_set2.tools] == ["third_party_weather"]


# ============================================================================
# validate 契约：缺失即红、软模式告警
# ============================================================================


def test_validate_full_contract_green() -> None:
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))
    assert adapter.validate() == []


def test_validate_event_result_missing_method_red() -> None:
    """事件结果缺 set_result_content_type：缺失即红（硬模式 raise）。"""
    runtime = _load_adapter()
    caps = base_runtime_capabilities(
        runtime,
        event_result_cls=type("BrokenResult", (), {"message": lambda self, text: self}),
    )
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="set_result_content_type"):
        adapter.validate()


def test_validate_event_type_missing_member_red() -> None:
    runtime = _load_adapter()
    caps = base_runtime_capabilities(
        runtime,
        event_type=type("ET", (), {"OnDecoratingResultEvent": "x", "OnAfterMessageSentEvent": "x"}),
    )
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="OnLLMRequestEvent"):
        adapter.validate()


def test_validate_provider_request_missing_field_red() -> None:
    runtime = _load_adapter()
    caps = base_runtime_capabilities(
        runtime,
        provider_request_cls=type(
            "BrokenReq", (), {"prompt": "", "image_urls": [], "func_tool": None}
        ),
    )
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="session_id"):
        adapter.validate()


def test_validate_call_event_hook_missing_red() -> None:
    runtime = _load_adapter()
    caps = base_runtime_capabilities(runtime, call_event_hook=None)
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="call_event_hook"):
        adapter.validate()


def test_validate_soft_warns_without_raising() -> None:
    """软模式（最新版漂移预警）：收集告警不阻塞。"""
    runtime = _load_adapter()
    caps = base_runtime_capabilities(
        runtime,
        event_type=type("ET", (), {"OnDecoratingResultEvent": "x", "OnAfterMessageSentEvent": "x"}),
    )
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    problems = adapter.validate(soft=True)
    assert any("OnLLMRequestEvent" in p for p in problems)


def test_validate_soft_green_silent() -> None:
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))
    assert adapter.validate(soft=True) == []


# ============================================================================
# 窄方法出口与事件钩子
# ============================================================================


def test_narrow_symbol_accessors() -> None:
    """适配层窄方法：事件结果/内容类型/事件类型/请求构造经适配层唯一出口。"""
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))
    result = adapter.new_event_result()
    assert callable(result.message) and callable(result.set_result_content_type)
    assert adapter.result_llm_type == "llm"
    assert adapter.event_type.OnLLMRequestEvent == "OnLLMRequestEvent"
    req = adapter.new_provider_request()
    assert req.session_id == ""


async def test_call_event_hook_awaits_async_callback() -> None:
    """call_event_hook 两分支（req 缺省/显式）对异步回调都走 maybe_await 正常 await。

    utils.maybe_await 是唯一实现，本测试锁住适配层调用点
    （此前该分支零覆盖：若导入/传参错误，测试不红）。
    """
    runtime = _load_adapter()
    calls: list[str] = []

    async def async_hook(event, event_type, req=None):
        calls.append(str(req))
        return "async-result"

    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, call_event_hook=async_hook)
    )
    assert await adapter.call_event_hook("evt", "OnLLMRequestEvent") == "async-result"
    assert await adapter.call_event_hook("evt", "OnLLMRequestEvent", req="req") == "async-result"
    assert calls == ["None", "req"]


async def test_call_event_hook_passes_through_sync_callback() -> None:
    """同步回调（非可等待值）原样返回，不误 await。"""
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, call_event_hook=lambda e, t, req=None: "sync")
    )
    assert await adapter.call_event_hook("evt", "t") == "sync"


def test_host_contract_checks_listed() -> None:
    """compat_check 的存在性清单与适配层契约单源（增删符号必须同步）。"""
    runtime = _load_adapter()
    contract = dict(runtime.AstrBotRuntimeAdapter.host_contract())
    for mod, attrs in contract.items():
        assert isinstance(mod, str) and isinstance(attrs, list) and attrs
    assert "astrbot.core.message.message_event_result" in contract
    assert "astrbot.core.star.star_handler" in contract


def test_command_handler_annotations_resolve_at_runtime() -> None:
    """所有宿主注册的处理器注解必须在运行时可解析。

    宿主那一步的精确位置（真机读源码确证，不是推断）：
    ``core/star/filter/command.py::CommandFilter.init_handler_md`` 在 4.23.3 是
    ``inspect.signature(handler)``，4.27.2 起是
    ``inspect.signature(handler, eval_str=True)``。一个参数之差，让
    ``from __future__ import annotations`` 产出的字符串注解在加载期真的被 eval，
    于是 TYPE_CHECKING-only 的名字在那里 NameError，插件整体拒绝加载。
    线上实测报错：``name 'CommandReply' is not defined``。

    4.23.3 上「宿主只读 signature.parameters」曾成立，故此前把 ``CommandReply``
    放在 TYPE_CHECKING 块里是安全的；4.27.2 起该前提失效。本测试用与宿主同一个
    调用（``eval_str=True``，非"等价物"）复现那一步，把「注解必须运行时可解析」
    钉成契约，不再依赖对宿主内部实现的假设。真机宿主上的同源守卫是
    ``scripts/compat_check.py::_handler_signature_gaps``。

    变异验证：把 main.py 的 ``CommandReply = AsyncGenerator[Any, None]`` 移回
    ``if TYPE_CHECKING:`` 块内，本测试即红（NameError: CommandReply）。
    """
    import inspect

    main_mod = load_modules(PACKAGE_NAME, "main")[0]
    plugin_cls = main_mod.SelfInitiatedReplyPlugin

    # 宿主注册的两类处理器：event_message_type 钩子与 /selfreply 指令族。
    handler_names = [
        name for name in dir(plugin_cls) if name == "on_message" or name.startswith("selfreply")
    ]
    assert "on_message" in handler_names
    # 指令族数量与 scripts/compat_check.py 的 EXPECTED_HANDLER_COUNT 同源：
    # 少于该数说明漏扫了（脚本侧同名常量防「检查空转」假绿）。
    from scripts.compat_check import EXPECTED_HANDLER_COUNT

    assert len([n for n in handler_names if n.startswith("selfreply")]) == EXPECTED_HANDLER_COUNT

    for name in handler_names:
        target = getattr(plugin_cls, name)
        func = getattr(target, "handler", target)
        if not callable(func) or not getattr(func, "__annotations__", None):
            continue
        # 与宿主加载期同一个调用；TYPE_CHECKING-only 名字在此 NameError。
        sig = inspect.signature(func, eval_str=True)
        annotation = sig.parameters["event"].annotation
        # 注解已被 eval（不再是字符串），说明这一步真的走过了解析而非原样透传。
        assert not isinstance(annotation, str), f"{name} 的 event 注解未被解析"


# ============================================================================
# 宿主私有层 import 收敛
# ============================================================================


def test_private_host_symbols_confined() -> None:
    """宿主私有层 import 只出现在适配层、宿主桩与兼容检查。"""
    import re

    pattern = re.compile(_PRIVATE_IMPORT_RE)
    violations = []
    for path in production_py_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in _SYMBOL_WHITELIST:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            violations.append(rel)
    assert not violations, f"宿主私有层 import 泄漏到：{', '.join(violations)}"
