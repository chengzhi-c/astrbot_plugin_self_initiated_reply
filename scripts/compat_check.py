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

# 与 runtime_adapter.from_host() 的实际引用保持一致；增删引用时必须同步此处
CHECKS = [
    ("astrbot.core.agent.tool", ["ToolSet"]),
    ("astrbot.core.astr_agent_run_util", ["run_agent"]),
    (
        "astrbot.core.astr_main_agent",
        ["MainAgentBuildConfig", "_get_session_conv", "build_main_agent"],
    ),
]


def main() -> int:
    import importlib

    import runtime_adapter

    for mod_name, attrs in CHECKS:
        mod = importlib.import_module(mod_name)
        for attr in attrs:
            assert hasattr(mod, attr), f"{mod_name}.{attr} 缺失——宿主私有 API 漂移"

    adapter = runtime_adapter.AstrBotRuntimeAdapter.from_host()
    adapter.validate()
    print("host compat OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
