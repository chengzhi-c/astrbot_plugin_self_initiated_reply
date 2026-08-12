"""一键本地门禁（stdlib 顺序编排）。

用法::

    python scripts/gates.py

顺序：ruff check → ruff format --check → mypy → docstring/version →
前端 syntax + contract → pytest → coverage_gates。
任一失败非零退出。wheel 检查在 dist/ 有 .whl 时追加。
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
    _run("ruff check", [sys.executable, "-m", "ruff", "check", "."])
    _run("ruff format --check", [sys.executable, "-m", "ruff", "format", "--check", "."])
    _run("mypy", [sys.executable, "-m", "mypy"])
    _run("docstring_gates", [sys.executable, "scripts/docstring_gates.py"])
    _run(
        "version_gates",
        [sys.executable, "scripts/version_gates.py", "--cross-only"],
    )

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

    _run("pytest", [sys.executable, "-m", "pytest", "-q"])
    _run("coverage_gates", [sys.executable, "scripts/coverage_gates.py"])

    wheels = sorted((ROOT / "dist").glob("*.whl")) if (ROOT / "dist").is_dir() else []
    if wheels:
        _run("check_wheel", [sys.executable, "scripts/check_wheel.py"])
    else:
        print("skip check_wheel (no dist/*.whl)")
    print("OK: all gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
