"""一键本地门禁（stdlib 顺序编排）。

用法::

    python scripts/gates.py [--with-mutation]

顺序：ruff check → ruff format --check → mypy →
前端 syntax + contract → 浏览器质量（Playwright）→ pytest → 真实宿主兼容
（有 astrbot 时）。

``--with-mutation`` 才跑变异门禁（另约 90s）。它锚定的是「既有测试还能抓既有缺陷」，
CI 把它限制为 nightly / 手动触发（见 ``.github/workflows/ci.yml`` 的 mutation 作业），
逐次本地都跑是纯浪费；本地默认快车道不含它，需要时显式打开。

与 CI 的差异（**本脚本是本地快车道，CI 才是权威**）：
- ``frontend-browser`` 需要本机已 ``npm ci`` 且装过 Chromium；缺少时明确 SKIP
  而非静默跳过（``npx playwright test`` 自带失败退出）。
- ``compat`` 需要真实 ``astrbot`` 宿主包。CI 装 4.23.3 / 4.27.2 / latest 三条腿，
  本地通常只有一条，故这里是「装了就跑」；未装时打印 SKIP 原因——本地缺宿主
  不是插件缺陷，但也不代表该项已验。
- CI 的 ``test`` 矩阵跨 Python 3.12/3.14 两版，本地只跑当前解释器。

发布产物（手工部署 zip）不经此处：由 ``git archive`` 单命令导出，
排除规则见仓库根 ``.gitattributes``，决策记录见 docs/DECISIONS.md。
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "主动回复设置"


def _run(label: str, argv: list[str]) -> None:
    print(f"==> {label}")
    print(" ".join(argv))
    completed = subprocess.run(argv, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _skip(label: str, reason: str) -> None:
    print(f"==> {label}: SKIP ({reason})")


def _run_browser_gate() -> None:
    """Playwright 浏览器质量门禁（CI ``frontend-browser`` 作业的本地对应）。"""
    npx = shutil.which("npx")
    if npx is None:
        _skip("browser quality gate", "npx not found on PATH")
        return
    if not (ROOT / "node_modules" / "@playwright" / "test").exists():
        _skip("browser quality gate", "run `npm ci` first (node_modules missing)")
        return
    _run("browser quality gate", [npx, "playwright", "test"])


def _run_compat_gate() -> None:
    """真实宿主兼容检查（CI ``compat`` 作业的本地对应，装了 astrbot 才跑）。"""
    try:
        available = importlib.util.find_spec("astrbot") is not None
    except (ImportError, ValueError):
        available = False
    if not available:
        _skip("host compatibility", "astrbot not installed in this interpreter")
        return
    _run("host compatibility", [sys.executable, "scripts/compat_check.py"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="本地质量门禁快车道（CI 才是权威）",
    )
    parser.add_argument(
        "--with-mutation",
        action="store_true",
        help="额外跑变异门禁（约 90s；CI 由 nightly/手动触发，本地默认不跑）",
    )
    args = parser.parse_args()
    # ruff 在 git 仓库内默认尊重 .gitignore（.venv/ 等本地目录已被忽略），
    # 与 CI lint 作业的 `ruff check .` 同一口径，无需手工维护文件列表。
    _run("ruff check", [sys.executable, "-m", "ruff", "check", "."])
    _run("ruff format --check", [sys.executable, "-m", "ruff", "format", "--check", "."])
    _run("mypy", [sys.executable, "-m", "mypy"])

    fe_sources = sorted(PAGE.glob("*.js")) + sorted(PAGE.glob("*.mjs"))
    if not fe_sources:
        print("FAIL: no frontend sources under pages/")
        return 1
    for path in fe_sources:
        rel = path.relative_to(ROOT).as_posix()
        _run(f"node --check {rel}", ["node", "--check", str(path)])
    _run(
        "frontend contract",
        ["node", "--test", "tests/frontend_contract.test.mjs"],
    )
    # 放在 pytest 之前：浏览器用例与被测 JS 同源，前端坏了先在这里暴露，
    # 不必等一分钟的 Python 用例跑完。
    _run_browser_gate()

    _run(
        "pytest",
        # 覆盖率用路径方式（.）追踪——动态加载使模块名 cov 失效（实测）。
        [sys.executable, "-m", "pytest", "-q", "--cov=.", "--cov-report=term-missing"],
    )
    # 放在 pytest 之后：变异门禁会临时改写源码并逐字节恢复，此时全量用例已跑完，
    # 两者不共享同一轮工作树状态。默认不跑——它锚定的缺陷只在改动那些测试或锚点时
    # 才可能回归，逐次跑是纯浪费（CI 的 mutation 作业同理只在 nightly/手动触发）。
    if args.with_mutation:
        _run("mutation gate", [sys.executable, "scripts/mutation_gate.py"])
    else:
        _skip("mutation gate", "pass --with-mutation to run it (CI runs it nightly)")
    # 最后跑：compat_check 会在临时目录里 import 真实宿主（cwd 由它自己切走），
    # 与前面各步无状态交叠。
    _run_compat_gate()
    print("OK: all gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
