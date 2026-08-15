"""一键本地门禁（stdlib 顺序编排）。

用法::

    python scripts/gates.py

顺序：ruff check → ruff format --check → mypy → docstring/version →
前端 syntax + contract → pytest → coverage_gates。
任一失败非零退出。wheel 检查在 dist/ 有 .whl 时追加。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "主动回复设置"
RUFF_SUFFIXES = {".py", ".pyi"}
FALLBACK_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".scratch",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "venv",
}


def _run(label: str, argv: list[str]) -> None:
    print(f"==> {label}")
    print(" ".join(argv))
    completed = subprocess.run(argv, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _tracked_files() -> list[Path] | None:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return [ROOT / os.fsdecode(path) for path in completed.stdout.split(b"\0") if path]


def _source_tree_files() -> list[Path]:
    return [
        path
        for path in sorted(ROOT.rglob("*.py")) + sorted(ROOT.rglob("*.pyi"))
        if not any(part in FALLBACK_EXCLUDED_DIRS for part in path.relative_to(ROOT).parts)
    ]


def ruff_targets() -> list[str]:
    files = _tracked_files()
    if files is None:
        print("INFO: git tracked-file list unavailable; checking Python files from the source tree")
        files = _source_tree_files()
    return [
        path.relative_to(ROOT).as_posix()
        for path in files
        if path.suffix in RUFF_SUFFIXES and path.is_file()
    ]


def main() -> int:
    targets = ruff_targets()
    if not targets:
        print("FAIL: no Python source files found for Ruff")
        return 1
    _run("ruff check", [sys.executable, "-m", "ruff", "check", *targets])
    _run("ruff format --check", [sys.executable, "-m", "ruff", "format", "--check", *targets])
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
