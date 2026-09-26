"""一键本地门禁（stdlib 顺序编排）。

用法::

    python scripts/gates.py [--with-mutation]

顺序：ruff check → ruff format --check → mypy →
前端 syntax + contract → 浏览器质量（Playwright）→ pytest → 真实宿主兼容
（有 astrbot 时）。

``--with-mutation`` 才跑变异门禁（基线缓存后约 180s）。它锚定的是「既有测试还能抓既有缺陷」，
CI 把它限制为 nightly / 手动触发（见 ``.github/workflows/ci.yml`` 的 mutation 作业），
逐次本地都跑是纯浪费；本地默认快车道不含它，需要时显式打开。

与 CI 的差异（**本脚本是本地快车道，CI 才是权威**）：
- ``frontend-browser`` 需要本机已 ``npm ci`` 且装过 Chromium；缺少时明确 SKIP
  而非静默跳过（``npx playwright test`` 自带失败退出）。
- ``compat`` 需要真实 ``astrbot`` 宿主包。CI 装 4.23.3 / 4.27.2 / latest 三条腿，
  本地通常只有一条，故这里是「装了就跑」；未装时打印 SKIP 原因，本地缺宿主
  不是插件缺陷，但也不代表该项已验。
- CI 的 ``test`` 矩阵跨 Python 3.12/3.14 两版，本地只跑当前解释器。

pytest 一步不沿用 shell 的 ``TEMP``/``TMP``：本轮自建专属临时根，``--basetemp``
显式指向其下（先 ``mkdir``），env 里 ``TEMP``/``TMP`` 也指向它。宿主 AstrBot
同为常驻进程，共用系统临时根时两个 Python 进程会各自建 ``pytest-of-*``，Windows
上撞名后 pytest 只留 warning 继续跑，等于悄悄换用别人的临时数据。顺带剔掉
``PYTEST_ADDOPTS`` / ``PYTEST_DEBUG_TEMPROOT``：前者会让显式 basetemp 与超时
判据失效，后者会保留 rootdir 使清理不完整。

发布产物（手工部署 zip）不经此处：由 ``git archive`` 单命令导出，
排除规则见仓库根 ``.gitattributes``，决策记录见 docs/DECISIONS.md。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "pages" / "主动回复设置"

# 外部遗留的这两项会让显式 --basetemp 与「保留 rootdir 便于排查」的行为互相
# 打架，必须剔除而不是覆盖（空串在 pytest 里仍然有效）。
PYTEST_ENV_INHERIT_DENY = ("PYTEST_ADDOPTS", "PYTEST_DEBUG_TEMPROOT")

PYTEST_ARGS = [
    # 覆盖率用路径方式（.）追踪，动态加载使模块名 cov 失效（实测）。
    "-q",
    "--cov=.",
    "--cov-report=term-missing",
]


def _run(label: str, argv: list[str], *, env: dict[str, str] | None = None) -> None:
    print(f"==> {label}")
    print(" ".join(argv))
    completed = subprocess.run(argv, cwd=ROOT, check=False, env=env)
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


def _scratch_root() -> Path:
    """本轮的专属临时根：不硬编码盘符（``tempfile.mkdtemp``），也不落在仓库内。"""
    return Path(tempfile.mkdtemp(prefix="gates-"))


def _run_env(scratch: Path) -> dict[str, str]:
    """pytest 子进程的 env：TEMP/TMP 同源指向专属根；不借 PYTEST_ADDOPTS。"""
    root = str(scratch)
    env = dict(os.environ, TEMP=root, TMP=root)
    for name in PYTEST_ENV_INHERIT_DENY:
        env.pop(name, None)
    return env


def _prepare_basetemp(scratch: Path) -> Path:
    """建本轮唯一的 pytest 临时目录；删不干净就抛，不启动 pytest。

    Windows 上删不掉通常是残留句柄（查看器、索引器、杀软扫描）。此时若照常启动，
    pytest 只留 warning 后继续从别人的临时目录取数，把「不知道」当通过。
    """
    basetemp = scratch / "basetemp"
    if basetemp.is_dir():
        # 不用 ignore_errors：删不干净时报的是真实 PermissionError，而不是被吞成
        # 后面 mkdir 的 FileExistsError，后者读起来像"目录已存在"这种无害事。
        shutil.rmtree(basetemp)
    elif basetemp.exists():
        basetemp.unlink()
    basetemp.mkdir(parents=True)
    return basetemp


def _cleanup_scratch(scratch: Path) -> None:
    """清理本轮临时根；失败只告警，绝不改写 pytest 的退出码或掩盖原异常。"""
    try:
        shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            print(f"WARN: 临时根仍存在（可能有进程占用）：{scratch}", file=sys.stderr)
    except OSError as exc:
        print(f"WARN: 清理临时根失败: {exc}", file=sys.stderr)


def _run_pytest() -> None:
    """跑全量 pytest：显式 basetemp + TEMP/TMP 都落在本轮专属根，跑完即清。"""
    scratch = _scratch_root()
    try:
        basetemp = _prepare_basetemp(scratch)
        _run(
            "pytest",
            [sys.executable, "-m", "pytest", *PYTEST_ARGS, f"--basetemp={basetemp}"],
            env=_run_env(scratch),
        )
    finally:
        _cleanup_scratch(scratch)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="本地质量门禁快车道（CI 才是权威）",
    )
    parser.add_argument(
        "--with-mutation",
        action="store_true",
        help="额外跑变异门禁（基线缓存后约 180s；CI 由 nightly/手动触发，本地默认不跑）",
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

    _run_pytest()
    # 放在 pytest 之后：变异门禁会临时改写源码并逐字节恢复，此时全量用例已跑完，
    # 两者不共享同一轮工作树状态。默认不跑，它锚定的缺陷只在改动那些测试或锚点时
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
