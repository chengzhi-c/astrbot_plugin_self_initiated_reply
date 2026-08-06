"""真实宿主兼容性检查：插件绑定的私有 AstrBot API 符号存在性 + 契约断言。

在装有真实 ``astrbot`` 包的环境中运行（CI 兼容矩阵 job 用）：
- 锁定版（默认）：契约缺口（符号缺失/签名缺参/危险工具未覆盖）即 exit 1
- 最新版（--warn-latest）：同一组检查降级为漂移预警，只告警不阻塞

存在性清单与契约断言单源：符号清单来自 runtime_adapter.host_contract()，
参数契约来自 AstrBotRuntimeAdapter.validate()——增删符号只需改适配层一处。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# astrbot 包 import 会在 cwd 生成运行时 data/ 目录：切到临时目录防污染工作区
os.chdir(tempfile.mkdtemp(prefix="astrbot-compat-"))

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


def _denylist_gaps() -> dict[str, list[str]]:
    """宿主危险工具全集与 HOST_DANGEROUS_TOOL_IDS 的缺口（空 = 全覆盖）。"""
    from models import HOST_DANGEROUS_TOOL_IDS

    return {
        mod: sorted(names - HOST_DANGEROUS_TOOL_IDS)
        for mod, names in _enumerate_tool_names().items()
        if names - HOST_DANGEROUS_TOOL_IDS
    }


def run_contract_checks(*, warn: bool) -> int:
    """符号存在性 + 契约断言 + denylist 覆盖；返回进程退出码。"""
    import importlib

    import runtime_adapter

    failures: list[str] = []
    for mod_name, attrs in runtime_adapter.AstrBotRuntimeAdapter.host_contract():
        mod = importlib.import_module(mod_name)
        for attr in attrs:
            if not hasattr(mod, attr):
                failures.append(f"{mod_name}.{attr} 缺失——宿主私有 API 漂移")

    event_type_members = runtime_adapter.EVENT_TYPE_MEMBERS
    star_handler = importlib.import_module("astrbot.core.star.star_handler")
    for member in event_type_members:
        if not hasattr(star_handler.EventType, member):
            failures.append(f"EventType.{member} 缺失——宿主私有 API 漂移")

    adapter = runtime_adapter.AstrBotRuntimeAdapter.from_host()
    problems = adapter.validate(soft=True)
    gaps = _denylist_gaps()
    for module_name, missing in gaps.items():
        failures.append(f"denylist 未覆盖 {module_name}: {', '.join(missing)}")

    all_problems = failures + problems
    if not all_problems:
        print("host compat OK")
        return 0
    for problem in all_problems:
        level = "warn" if warn else "error"
        print(f"[{level}] {problem}", file=sys.stderr)
    return 0 if warn else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warn-latest",
        action="store_true",
        help="最新版漂移预警：契约缺口只告警不阻塞（锁定版硬门禁为默认）",
    )
    args = parser.parse_args()
    return run_contract_checks(warn=args.warn_latest)


if __name__ == "__main__":
    raise SystemExit(main())
