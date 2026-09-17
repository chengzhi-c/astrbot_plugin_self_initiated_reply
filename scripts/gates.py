"""一键本地门禁（stdlib 顺序编排）。

用法::

    python scripts/gates.py

顺序：ruff check → ruff format --check → mypy →
前端 syntax + contract → pytest → 变异门禁。

发布产物（手工部署 zip）不经此处：由 ``git archive`` 单命令导出，
排除规则见仓库根 ``.gitattributes``，决策记录见 docs/DECISIONS.md。
"""

from __future__ import annotations

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


def main() -> int:
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

    _run(
        "pytest",
        # 覆盖率用路径方式（.）追踪——动态加载使模块名 cov 失效（实测）。
        [sys.executable, "-m", "pytest", "-q", "--cov=.", "--cov-report=term-missing"],
    )
    # 放在 pytest 之后：变异门禁会临时改写源码并逐字节恢复，此时全量用例已跑完，
    # 两者不共享同一轮工作树状态。
    _run("mutation gate", [sys.executable, "scripts/mutation_gate.py"])
    print("OK: all gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
