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
