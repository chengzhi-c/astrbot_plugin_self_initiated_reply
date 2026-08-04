"""真实宿主兼容性冒烟：插件绑定的私有 AstrBot API 符号存在性 + 签名校验。

在装有真实 ``astrbot`` 包的环境中运行（CI 兼容矩阵 job 用）：
- 断言插件引用的私有模块/符号全部存在（漂移即报错）
- 用真实宿主能力跑 AstrBotRuntimeAdapter.validate()（签名参数缺漏即报错）
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# astrbot 包 import 会在 cwd 生成运行时 data/ 目录：切到临时目录防污染工作区
os.chdir(tempfile.mkdtemp(prefix="astrbot-compat-"))

# 与 main.py / runtime_adapter.from_host() 的实际引用保持一致；增删引用时必须同步此处
CHECKS = [
    ("astrbot.core.agent.tool", ["ToolSet"]),
    ("astrbot.core.astr_agent_run_util", ["run_agent"]),
    (
        "astrbot.core.astr_main_agent",
        ["MainAgentBuildConfig", "_get_session_conv", "build_main_agent"],
    ),
    ("astrbot.core.message.message_event_result", ["MessageEventResult", "ResultContentType"]),
    ("astrbot.core.pipeline.context", ["call_event_hook"]),
    ("astrbot.core.provider.entities", ["ProviderRequest"]),
    ("astrbot.core.star.star_handler", ["EventType"]),
]

# main.py 实际使用的 EventType 枚举成员：漂移（改名/移除）即报错
EVENT_TYPE_MEMBERS = [
    "OnLLMRequestEvent",
    "OnDecoratingResultEvent",
    "OnAfterMessageSentEvent",
]

# 宿主危险内置工具模块：这些模块内所有 FunctionTool 子类的 name 必须全部被
# models.HOST_DANGEROUS_TOOL_IDS 覆盖——宿主新增/改名危险工具时缺失即报错，
# 防止 denylist（"最终防线"）静默失效。与 models.py 的清单同步维护。
DANGEROUS_TOOL_MODULES = [
    "astrbot.core.tools.cron_tools",
    "astrbot.core.tools.knowledge_base_tools",
    "astrbot.core.tools.computer_tools.fs",
    "astrbot.core.tools.computer_tools.shell",
    "astrbot.core.tools.computer_tools.python",
    "astrbot.core.tools.computer_tools.shipyard_neo.browser",
]


def _enumerate_tool_names() -> dict[str, set[str]]:
    """从宿主危险工具模块枚举所有 FunctionTool 子类的 name 类属性。"""
    import importlib
    import inspect

    from astrbot.core.agent.tool import FunctionTool

    found: dict[str, set[str]] = {}
    for mod_name in DANGEROUS_TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        names = {
            str(getattr(obj, "name", "")).strip()
            for _, obj in inspect.getmembers(mod, inspect.isclass)
            if issubclass(obj, FunctionTool) and obj is not FunctionTool
        }
        found[mod_name] = {name for name in names if name}
    return found


def _assert_denylist_covers_host() -> None:
    """宿主危险工具全集必须 ⊆ HOST_DANGEROUS_TOOL_IDS，缺口即报错。"""
    from models import HOST_DANGEROUS_TOOL_IDS

    uncovered = {
        mod: sorted(names - HOST_DANGEROUS_TOOL_IDS)
        for mod, names in _enumerate_tool_names().items()
        if names - HOST_DANGEROUS_TOOL_IDS
    }
    if uncovered:
        for mod, missing in uncovered.items():
            print(f"denylist 未覆盖 {mod}: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)


def main() -> int:
    import importlib

    import runtime_adapter

    for mod_name, attrs in CHECKS:
        mod = importlib.import_module(mod_name)
        for attr in attrs:
            assert hasattr(mod, attr), f"{mod_name}.{attr} 缺失——宿主私有 API 漂移"

    star_handler = importlib.import_module("astrbot.core.star.star_handler")
    for member in EVENT_TYPE_MEMBERS:
        assert hasattr(star_handler.EventType, member), f"EventType.{member} 缺失——宿主私有 API 漂移"

    adapter = runtime_adapter.AstrBotRuntimeAdapter.from_host()
    adapter.validate()
    _assert_denylist_covers_host()
    print("host compat OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
