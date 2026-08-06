from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "selfreply_runtime_test_package"


def _load_adapter():
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package
    return importlib.import_module(f"{PACKAGE_NAME}.runtime_adapter")


def _base_caps(runtime, **overrides):
    """完整契约 capabilities：ticket 13 后 validate 覆盖全部私有入口。"""
    base = dict(
        import_error=None,
        tool_set=object,
        build_config=object,
        build_main_agent=lambda **_k: None,
        get_session_conv=lambda *_a: None,
        run_agent=lambda *_a, **_k: (),
        event_result_cls=type(
            "Result",
            (),
            {"message": lambda self, t: self, "set_result_content_type": lambda self, t: self},
        ),
        result_content_type=type("CT", (), {"LLM_RESULT": "llm"}),
        event_type=type(
            "ET",
            (),
            {
                "OnLLMRequestEvent": "OnLLMRequestEvent",
                "OnDecoratingResultEvent": "OnDecoratingResultEvent",
                "OnAfterMessageSentEvent": "OnAfterMessageSentEvent",
            },
        ),
        call_event_hook=lambda *_a, **_k: True,
        provider_request_cls=type(
            "Req",
            (),
            {
                "prompt": "",
                "image_urls": [],
                "audio_urls": [],
                "func_tool": None,
                "session_id": "",
                "conversation": None,
                "contexts": [],
            },
        ),
    )
    base.update(overrides)
    return runtime.AgentRuntimeCapabilities(**base)


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
        _base_caps(
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

    adapter = runtime.AstrBotRuntimeAdapter(_base_caps(runtime, build_main_agent=incompatible))

    with pytest.raises(RuntimeError, match="apply_reset"):
        adapter.validate()


def test_runtime_adapter_enforces_run_contract_params() -> None:
    """run_agent 缺少实际使用的运行参数时必须加载期失败。"""
    runtime = _load_adapter()

    async def run_agent(agent_runner, *, max_step):
        yield agent_runner, max_step

    adapter = runtime.AstrBotRuntimeAdapter(_base_caps(runtime, run_agent=run_agent))

    with pytest.raises(RuntimeError, match="show_tool_use"):
        adapter.validate()


def test_filter_final_tools_modes() -> None:
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(_base_caps(runtime))

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
