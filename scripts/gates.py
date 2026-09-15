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


def _release_artifacts() -> tuple[list[Path], list[Path]]:
    dist = ROOT / "dist"
    if not dist.is_dir():
        return [], []
    return sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))


def main(*, require_release: bool = False) -> int:
    # ruff 在 git 仓库内默认尊重 .gitignore（.venv/ 等本地目录已被忽略），
    # 与 CI lint 作业的 `ruff check .` 同一口径，无需手工维护文件列表。
    _run("ruff check", [sys.executable, "-m", "ruff", "check", "."])
    _run("ruff format --check", [sys.executable, "-m", "ruff", "format", "--check", "."])
    _run("mypy", [sys.executable, "-m", "mypy"])
    _run("version_gates", [sys.executable, "scripts/version_gates.py"])

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
