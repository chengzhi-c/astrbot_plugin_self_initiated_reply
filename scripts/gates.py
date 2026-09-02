"""一键本地门禁（stdlib 顺序编排）。

用法::

    python scripts/gates.py

顺序：ruff check → ruff format --check → mypy → version →
前端 syntax + contract → pytest。存在完整 wheel/sdist 时追加发布产物检查；
`--release` 要求发布产物齐全。无产物的普通本地模式只报告 `NOT RELEASE-VERIFIED`，
不会输出发布级全绿。
"""

from __future__ import annotations

import argparse
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


def _release_artifacts() -> tuple[list[Path], list[Path]]:
    dist = ROOT / "dist"
    if not dist.is_dir():
        return [], []
    return sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))


def main(*, require_release: bool = False) -> int:
    targets = ruff_targets()
    if not targets:
        print("FAIL: no Python source files found for Ruff")
        return 1
    _run("ruff check", [sys.executable, "-m", "ruff", "check", *targets])
    _run("ruff format --check", [sys.executable, "-m", "ruff", "format", "--check", *targets])
    _run("mypy", [sys.executable, "-m", "mypy"])
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

    wheels, sdists = _release_artifacts()
    if len(wheels) != 1 or len(sdists) != 1:
        print(
            "NOT RELEASE-VERIFIED: expected exactly one wheel and one sdist "
            f"(found wheel={len(wheels)}, sdist={len(sdists)})"
        )
        if require_release:
            return 1
        print("OK: code gates passed; release artifacts were not verified")
        return 0

    _run("check_wheel", [sys.executable, "scripts/check_wheel.py"])
    _run("check_sdist", [sys.executable, "scripts/check_sdist.py"])
    _run("deploy zip", [sys.executable, "scripts/make_release_zip.py"])
    print("OK: all gates passed")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release",
        action="store_true",
        help="require exactly one validated wheel, sdist, and deploy zip",
    )
    raise SystemExit(main(require_release=parser.parse_args().release))
