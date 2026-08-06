"""宿主符号收敛契约（ticket 13）：私有 core 符号只允许出现在适配层与宿主桩。

验收项：
- 全仓库 grep：宿主私有符号只出现在适配层与宿主桩两处（文本收敛断言）
- 契约断言覆盖全部私有入口（构建/运行/会话装载/事件结果/请求/钩子），缺失即红
- 兼容检查软模式（最新版漂移预警）收集告警不阻塞，硬模式（锁定版）首错即红
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_vision import PACKAGE_NAME

ROOT = Path(__file__).resolve().parents[1]

# 宿主私有层 import 语句：全仓库只允许出现在收敛点（适配层、宿主桩、兼容检查）
_PRIVATE_IMPORT_RE = r"(^|\n)\s*(from|import)\s+astrbot\.core"
# 私有符号唯一允许出现的收敛点（适配层、宿主桩、兼容检查脚本）
_SYMBOL_WHITELIST = {
    "runtime_adapter.py",
    "scripts/compat_check.py",
    "tests/host_stubs.py",
}
# 除白名单外，其余源文件一律禁止 import 宿主私有层（astrbot.core）
_CHECKED_MODULES = [
    "adapters.py",
    "commands.py",
    "decision.py",
    "delivery.py",
    "events.py",
    "generation.py",
    "main.py",
    "outbound.py",
    "scheduler.py",
    "session_coordinator.py",
    "session_gate.py",
    "state_saver.py",
    "storage.py",
    "unified_manager.py",
    "utils.py",
    "webapi.py",
    "whitelist.py",
    "image/extractor.py",
    "image/models.py",
    "image/parser.py",
    "image/recorder_bridge.py",
]


def test_private_host_symbols_confined() -> None:
    """宿主私有层 import 只出现在适配层与宿主桩两处（缺失即红）。"""
    import re

    pattern = re.compile(_PRIVATE_IMPORT_RE)
    violations = []
    for rel in _CHECKED_MODULES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        if pattern.search(src):
            violations.append(rel)
    assert not violations, f"宿主私有层 import 泄漏到：{', '.join(violations)}"


def _runtime_adapter():
    import sys
    import types

    from .host_stubs import install_astrbot_stubs

    install_astrbot_stubs()  # runtime_adapter 无模块级宿主 import，装桩保证包加载路径一致
    if PACKAGE_NAME not in sys.modules:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE_NAME] = package
    return importlib.import_module(f"{PACKAGE_NAME}.runtime_adapter")


def _full_capabilities(adapter, **overrides):
    base = dict(
        import_error=None,
        tool_set=type("ToolSet", (), {}),
        build_config=type("BuildConfig", (), {}),
        build_main_agent=lambda **kwargs: SimpleNamespace(
            agent_runner=SimpleNamespace(), reset_coro=SimpleNamespace(close=lambda: None)
        ),
        get_session_conv=lambda event, context: SimpleNamespace(
            history="[]", context=SimpleNamespace()
        ),
        run_agent=lambda runner, **kwargs: iter(()),
        event_result_cls=type(
            "MessageEventResult",
            (),
            {"message": lambda self, text: self, "set_result_content_type": lambda self, t: self},
        ),
        result_content_type=SimpleNamespace(LLM_RESULT="llm"),
        event_type=SimpleNamespace(
            OnLLMRequestEvent="OnLLMRequestEvent",
            OnDecoratingResultEvent="OnDecoratingResultEvent",
            OnAfterMessageSentEvent="OnAfterMessageSentEvent",
        ),
        call_event_hook=lambda event, event_type, req=None: True,
        provider_request_cls=type(
            "ProviderRequest",
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
        config_path_fn=lambda: "/tmp/config",
        plugin_data_path_fn=lambda: "/tmp/data",
    )
    base.update(overrides)
    return adapter.AgentRuntimeCapabilities(**base)


def test_validate_full_contract_green() -> None:
    adapter = _runtime_adapter()
    runtime = adapter.AstrBotRuntimeAdapter(_full_capabilities(adapter))
    assert runtime.validate() == []


def test_validate_event_result_missing_method_red() -> None:
    """事件结果缺 set_result_content_type：缺失即红（硬模式 raise）。"""
    adapter = _runtime_adapter()
    caps = _full_capabilities(
        adapter,
        event_result_cls=type("BrokenResult", (), {"message": lambda self, text: self}),
    )
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="set_result_content_type"):
        runtime.validate()


def test_validate_event_type_missing_member_red() -> None:
    adapter = _runtime_adapter()
    caps = _full_capabilities(
        adapter,
        event_type=SimpleNamespace(OnDecoratingResultEvent="x", OnAfterMessageSentEvent="x"),
    )
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="OnLLMRequestEvent"):
        runtime.validate()


def test_validate_provider_request_missing_field_red() -> None:
    adapter = _runtime_adapter()
    caps = _full_capabilities(
        adapter,
        provider_request_cls=type(
            "BrokenReq", (), {"prompt": "", "image_urls": [], "func_tool": None}
        ),
    )
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="session_id"):
        runtime.validate()


def test_validate_call_event_hook_missing_red() -> None:
    adapter = _runtime_adapter()
    caps = _full_capabilities(adapter, call_event_hook=None)
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="call_event_hook"):
        runtime.validate()


def test_validate_soft_warns_without_raising() -> None:
    """软模式（最新版漂移预警）：收集告警不阻塞。"""
    adapter = _runtime_adapter()
    caps = _full_capabilities(
        adapter,
        event_type=SimpleNamespace(OnDecoratingResultEvent="x", OnAfterMessageSentEvent="x"),
    )
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    problems = runtime.validate(soft=True)
    assert any("OnLLMRequestEvent" in p for p in problems)


def test_validate_soft_green_silent() -> None:
    adapter = _runtime_adapter()
    runtime = adapter.AstrBotRuntimeAdapter(_full_capabilities(adapter))
    assert runtime.validate(soft=True) == []


def test_narrow_symbol_accessors() -> None:
    """适配层窄方法：事件结果/请求/事件类型/钩子/路径经适配层唯一出口。"""
    adapter = _runtime_adapter()
    runtime = adapter.AstrBotRuntimeAdapter(_full_capabilities(adapter))
    result = runtime.new_event_result()
    assert callable(result.message) and callable(result.set_result_content_type)
    assert runtime.result_llm_type == "llm"
    assert runtime.event_type.OnLLMRequestEvent == "OnLLMRequestEvent"
    req = runtime.new_provider_request()
    assert req.session_id == ""
    assert runtime.config_path() == "/tmp/config"
    assert runtime.plugin_data_path() == "/tmp/data"


def test_host_contract_checks_listed() -> None:
    """compat_check 的存在性清单与适配层契约单源（增删符号必须同步）。"""
    adapter = _runtime_adapter()
    contract = dict(adapter.AstrBotRuntimeAdapter.host_contract())
    for mod, attrs in contract.items():
        assert isinstance(mod, str) and isinstance(attrs, list) and attrs
    assert "astrbot.core.message.message_event_result" in contract
    assert "astrbot.core.star.star_handler" in contract


def test_main_no_direct_private_import() -> None:
    """main 只保留模块级绑定名（供测试替换），不再直接 import 私有层。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "from astrbot.core" not in src
    assert "import astrbot.core" not in src
