#!/usr/bin/env python3
"""版本断言门禁：工具版本在「本机 / CI / pre-commit / pyproject」四处的一致性检查。

动机：ruff 0.15 → 0.16 起检查 Markdown 代码围栏，旧版通过的改动在 CI 变红。
本脚本对比两类事实：

1. **本机 vs 声明**（默认模式）：本机装的版本是否满足 pyproject 的 ``[dev]`` 区间，
   以及是否等于 ci.yml 里明确钉版的工具版本。管的是"本地环境过期"。
2. **声明之间**（``--cross-only`` 也会跑，且不需要装任何工具）：ruff 在 ci.yml、
   .pre-commit-config.yaml、pyproject 三处必须同版。管的是"配置互相漂移"。

第 2 类补的缺口：ci.yml 里原本只有一句注释声明"与
.pre-commit-config.yaml 的 rev 对齐"，没有任何断言核验，改一处忘另一处不会有人发现——
后果是本地 pre-commit 全绿而 CI 的 lint 作业变红（或反之，本地被旧版拦下）。

为什么钉版之外还要留这个门禁：钉版是**安装期**预防（装的时候就装对），本脚本是
**任何时刻**检测（长期开着的开发机、手动 pip install 覆盖过的环境、pyproject 改了
但没重装的工作树）。两者不重复。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github/workflows/ci.yml"
PRE_COMMIT_YML = ROOT / ".pre-commit-config.yaml"
PYPROJECT = ROOT / "pyproject.toml"

# 三处声明必须同版的工具。只列真正跨配置重复出现的：ruff 同时被 CI lint 作业直装、
# 被 pre-commit 以 rev 形式装、又在 [dev] 里（供 python -m ruff 本地调用）。
# mypy 刻意不在此列：pre-commit 的 mirrors-mypy rev 是 v1.19.0 而 [dev] 允许到 <3，
# 实测两版对本仓库结果相同（均 Success），强求字面同版只会制造无信息量的红。
CROSS_SOURCE_TOOLS = ("ruff",)


def _read_pyproject() -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def extract_pinned_versions() -> dict[str, str]:
    """从 ci.yml 提取明确钉版的工具（pip install tool==version）。"""
    if not CI_YML.exists():
        return {}
    text = CI_YML.read_text(encoding="utf-8")
    pinned: dict[str, str] = {}
    # 匹配 `pip install tool==X.Y.Z` 形式，version 必须是字面量（数字开头，排除模板变量）
    for match in re.finditer(r"pip install\s+([^\n]+)", text):
        line = match.group(1)
        for part in line.split():
            if "==" in part and "${{" not in part:  # 排除模板变量
                tool, version = part.split("==", 1)
                # version 必须以数字开头（排除 "${{ matrix.x }}" 等）
                if version and version[0].isdigit():
                    pinned[tool.strip()] = version.strip()
    return pinned


def extract_dev_specifiers() -> dict[str, str]:
    """从 pyproject 的 ``[project.optional-dependencies].dev`` 提取版本区间。

    返回 ``{包名: 版本区间}``；带环境标记（如 ``tomli; python_version < '3.11'``）
    的条目按当前解释器求值，不适用则跳过——否则在 3.11+ 上会去查一个本不该装的包。
    """
    from packaging.markers import Marker
    from packaging.requirements import Requirement

    specifiers: dict[str, str] = {}
    for raw in _read_pyproject()["project"]["optional-dependencies"]["dev"]:
        req = Requirement(raw)
        if req.marker is not None and not Marker(str(req.marker)).evaluate():
            continue
        specifiers[req.name] = str(req.specifier)
    return specifiers


def extract_dev_exact_pins() -> dict[str, str]:
    """只取 ``[dev]`` 里写成精确钉版（``tool==X.Y.Z``、无逗号）的条目。

    刻意不走 ``packaging``：本函数服务于 :func:`check_cross_source`，而那条检查要能在
    CI 的 lint 作业跑——该作业只装 ruff，不装 ``.[dev]``。纯 stdlib 才能挂在那里，
    否则得为一条断言在 lint 作业多装一个包（且那个包自身又没钉版）。
    """
    pins: dict[str, str] = {}
    for raw in _read_pyproject()["project"]["optional-dependencies"]["dev"]:
        entry = str(raw).split(";", 1)[0].strip()  # 去掉环境标记
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([0-9][^,\s]*)", entry)
        if match:
            pins[match.group(1)] = match.group(2)
    return pins


def extract_precommit_revs() -> dict[str, str]:
    """从 .pre-commit-config.yaml 提取各 repo 的 rev，按工具名归并。

    刻意用正则而非 yaml 解析：pre-commit 不是本仓库的运行时依赖，PyYAML 也不在
    ``[dev]`` 里，为一处 rev 引入依赖不划算。rev 形如 ``v0.16.1``，去掉前缀 v。
    """
    if not PRE_COMMIT_YML.exists():
        return {}
    text = PRE_COMMIT_YML.read_text(encoding="utf-8")
    revs: dict[str, str] = {}
    # repo 行与其后最近的 rev 行成对出现
    for match in re.finditer(r"-\s+repo:\s*(\S+)\s*\n\s*rev:\s*(\S+)", text):
        repo, rev = match.group(1), match.group(2).strip()
        version = rev.lstrip("v")
        if not version or not version[0].isdigit():
            continue  # 分支名/commit sha：无版本语义，跳过
        for tool in CROSS_SOURCE_TOOLS:
            # ruff → astral-sh/ruff-pre-commit；mypy → pre-commit/mirrors-mypy
            if tool in repo.rsplit("/", 1)[-1]:
                revs[tool] = version
    return revs


def check_cross_source() -> list[str]:
    """声明之间的一致性：同一工具在 CI / pre-commit / pyproject 三处必须同版。

    返回问题描述列表（空列表表示通过）。本函数**不探测本机、且只用 stdlib**，故在任何
    环境都能跑（含只装了 ruff 的 CI lint 作业），也被 tests/test_config_schema.py
    直接调用当断言用。
    """
    ci_pins = extract_pinned_versions()
    pre_commit = extract_precommit_revs()
    dev_pins = extract_dev_exact_pins()
    problems: list[str] = []

    for tool in CROSS_SOURCE_TOOLS:
        # 三处都**必须**给出精确版本。刻意不写成"比对能找到的那几处"：那样一来把某处
        # 松成区间（如 ruff>=0.16）就会让该源静默退出比对、剩下两处仍然一致而全绿，
        # 恰好放过本门禁要防的漂移——`.[dev]` 装到的 ruff 会与 lint 作业钉的不同版
        # （实测：本用例的变异 C 一次假绿，故改为"缺一处即红"）。
        sources: dict[str, str | None] = {
            f"ci.yml (pip install {tool}==)": ci_pins.get(tool),
            ".pre-commit-config.yaml (rev)": pre_commit.get(tool),
            "pyproject [dev] (精确钉版)": dev_pins.get(tool),
        }
        absent = sorted(src for src, version in sources.items() if version is None)
        if absent:
            problems.append(
                f"{tool}: 以下位置没有精确版本声明：{absent}。"
                f"某处松成区间或写法变了都会到这里——松版会让 pip install -e '.[dev]' "
                f"装到与 lint 作业不同的版本；若该工具确实不再需要跨源同版，"
                f"请从 CROSS_SOURCE_TOOLS 移除并说明理由"
            )
            continue
        declared = {src: version for src, version in sources.items() if version is not None}
        if len(set(declared.values())) > 1:
            detail = "，".join(f"{src}={ver}" for src, ver in sorted(declared.items()))
            problems.append(f"{tool}: 各处声明的版本不一致（{detail}）")
    return problems


def get_local_version(tool: str) -> str | None:
    """获取本机已安装版本。

    优先查包元数据（``importlib.metadata``）：httpx / pathspec / pytest-asyncio 这类
    没有 ``python -m tool --version`` 入口的包只能这样拿到版本。取不到再退回子进程，
    保留原有对 ruff / mypy / pytest 的探测口径。
    """
    import importlib.metadata as md

    try:
        return md.version(tool)
    except md.PackageNotFoundError:
        pass
    except Exception:
        pass

    try:
        result = subprocess.run(
            [sys.executable, "-m", tool, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        # ruff: "ruff 0.16.1"
        # mypy: "mypy 2.3.0 (compiled: yes)"
        # pytest: "pytest 9.1.1"
        first_line = result.stdout.strip().split("\n")[0]
        parts = first_line.split()
        if len(parts) >= 2:
            # 取第二个词作为版本号
            return parts[1]
        return None
    except Exception:
        return None


def check_local_env() -> tuple[list[str], list[str]]:
    """本机 vs 声明：返回 ``(问题列表, 修复命令列表)``。"""
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    ci_pins = extract_pinned_versions()
    dev = extract_dev_specifiers()
    problems: list[str] = []
    fixes: list[str] = []

    for tool, ci_version in sorted(ci_pins.items()):
        local = get_local_version(tool)
        if local != ci_version:
            problems.append(f"{tool}: CI 钉版 {ci_version}，本机 {local or '未安装或无法探测'}")
            fixes.append(f"pip install {tool}=={ci_version}")

    for tool, spec in sorted(dev.items()):
        if not spec or tool in ci_pins:
            continue  # 无区间；或已由上面的精确比对覆盖
        local = get_local_version(tool)
        if local is None:
            problems.append(f"{tool}: pyproject 要求 {spec}，本机未安装")
            fixes.append('pip install -e ".[dev]"')
            continue
        try:
            satisfied = Version(local) in SpecifierSet(spec)
        except InvalidVersion:
            continue  # 本机版本号非标准（如源码安装的 dev 版）：不判死
        if not satisfied:
            problems.append(f"{tool}: pyproject 要求 {spec}，本机 {local}")
            fixes.append('pip install -e ".[dev]"')
    return problems, fixes


def main() -> int:
    cross_only = "--cross-only" in sys.argv[1:]

    cross_problems = check_cross_source()
    if cross_problems:
        print("FAIL: 配置声明之间的版本不一致")
        print()
        for problem in cross_problems:
            print(f"  {problem}")
        print()
        print("这类漂移的后果是本地 pre-commit 与 CI lint 结论相反，改一处务必改全部。")
        return 1
    print(f"PASS: {len(CROSS_SOURCE_TOOLS)} 个跨源工具在各配置中同版")

    if cross_only:
        return 0

    problems, fixes = check_local_env()
    if problems:
        print()
        print("FAIL: 本机工具版本与声明不符")
        print()
        for problem in problems:
            print(f"  {problem}")
        print()
        print("修复命令：")
        for fix in dict.fromkeys(fixes):  # 去重且保序
            print(f"  {fix}")
        return 1

    dev = extract_dev_specifiers()
    print(f"PASS: {len(dev)} 个开发依赖均满足 pyproject 声明的版本区间")
    return 0


if __name__ == "__main__":
    sys.exit(main())
