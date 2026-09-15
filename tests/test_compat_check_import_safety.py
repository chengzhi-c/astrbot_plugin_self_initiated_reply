"""compat_check 必须可被 import：测试只为取常量而加载它。

脚本本体在模块级做进程级副作用（sys.path 注入 / chdir 临时目录 / 注册假包），
而 tests/test_host_contract.py 为取 EXPECTED_HANDLER_COUNT 而 import 它——
副作用会改掉 pytest 进程的 cwd、并在宿主真包已装时用假包顶掉 sys.modules 里的
同名条目。副作用因此收敛进 _bootstrap()，只由 __main__ 入口调用。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_importing_compat_check_keeps_process_state_intact() -> None:
    """import 后 cwd 不变、sys.modules 不出现假包、sys.path 未被改写。"""
    probe = """
import sys
from pathlib import Path

before_cwd = Path.cwd()
before_path = list(sys.path)
before_module = sys.modules.get("astrbot_plugin_self_initiated_reply")

import scripts.compat_check  # noqa: F401

assert Path.cwd() == before_cwd, f"cwd changed to {Path.cwd()}"
assert sys.path == before_path, "sys.path mutated at import time"
after_module = sys.modules.get("astrbot_plugin_self_initiated_reply")
assert after_module is before_module, "fake package injected into sys.modules at import time"
print(scripts.compat_check.EXPECTED_HANDLER_COUNT)
"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1].isdigit(), proc.stdout


def test_importing_compat_check_does_not_chdir_even_once_loaded() -> None:
    """同一进程内二次 import（已缓存）同样不留副作用。"""
    before = Path.cwd()
    module = importlib.import_module("scripts.compat_check")
    assert Path.cwd() == before
    assert isinstance(module.EXPECTED_HANDLER_COUNT, int)
