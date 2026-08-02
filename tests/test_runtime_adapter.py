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
        runtime.AgentRuntimeCapabilities(
            import_error=None,
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
        runtime.AgentRuntimeCapabilities(
            import_error=None,
            tool_set=object,
            build_config=object,
            build_main_agent=incompatible,
            get_session_conv=lambda *_args: None,
            run_agent=lambda *_args, **_kwargs: (),
        )
    )

    with pytest.raises(RuntimeError, match="apply_reset"):
        adapter.validate()


def test_runtime_adapter_enforces_run_contract_params() -> None:
    """run_agent 缺少实际使用的运行参数时必须加载期失败。"""
    runtime = _load_adapter()

    async def run_agent(agent_runner, *, max_step):
        yield agent_runner, max_step

    adapter = runtime.AstrBotRuntimeAdapter(
        runtime.AgentRuntimeCapabilities(
            import_error=None,
            tool_set=object,
            build_config=object,
            build_main_agent=lambda **_k: None,
            get_session_conv=lambda *_a: None,
            run_agent=run_agent,
        )
    )

    with pytest.raises(RuntimeError, match="show_tool_use"):
        adapter.validate()


def test_restrict_final_tools_enforces_allowlist() -> None:
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(
        runtime.AgentRuntimeCapabilities(
            import_error=None,
            tool_set=object,
            build_config=object,
            build_main_agent=lambda **_k: None,
            get_session_conv=lambda *_a: None,
            run_agent=lambda *_a, **_k: (),
        )
    )

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

    assert adapter.restrict_final_tools(req, set()) is True
    assert tool_set.tools == []

    # 无法枚举 -> fail closed
    bad_req = type("Req", (), {"func_tool": type("Bad", (), {"tools": None})()})()
    assert adapter.restrict_final_tools(bad_req, set()) is False

    # 无工具集 -> 天然空，放行
    empty_req = type("Req", (), {"func_tool": None})()
    assert adapter.restrict_final_tools(empty_req, set()) is True
