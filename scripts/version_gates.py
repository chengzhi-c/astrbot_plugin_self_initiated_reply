#!/usr/bin/env python3
"""版本断言门禁：ruff 在「CI / pre-commit / pyproject」三处声明必须同版。

动机：ruff 0.15 → 0.16 起检查 Markdown 代码围栏，旧版通过的改动在 CI 变红。
ci.yml 里原本只有一句注释声明"与 .pre-commit-config.yaml 的 rev 对齐"，没有任何
断言核验，改一处忘另一处不会有人发现——后果是本地 pre-commit 全绿而 CI 的 lint
作业变红（或反之，本地被旧版拦下）。

为什么钉版之外还要留这个门禁：钉版是**安装期**预防（装的时候就装对），本脚本是
**任何时刻**检测（pyproject 改了但没重装的工作树、rev 与 pip install 行漂移）。
两者不重复。

历史：本脚本曾有"本机 vs 声明"的默认模式（探测本机装了什么版本），但 CI、
pre-commit、gates.py 三个调用方全部只跑跨源比对，本机模式没有任何自动化消费者，
已删除。``--cross-only`` 参数名保留以兼容既有调用方，行为即默认且唯一行为。
"""

from __future__ import annotations

import re
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
    import tomllib

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

    返回问题描述列表（空列表表示通过）。本函数**只用 stdlib**，故在任何环境都能跑
    （含只装了 ruff 的 CI lint 作业），也被 tests/test_config_schema.py
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


def main() -> int:
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
