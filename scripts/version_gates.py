#!/usr/bin/env python3
"""版本断言门禁：本机工具版本与 CI 钉版一致性检查。

动机：ruff 0.15 → 0.16 起检查 Markdown 代码围栏，旧版通过的改动在 CI 变红。
本脚本从 .github/workflows/ci.yml 提取明确钉版的工具，与本机版本对比，
不一致时报错并给出安装命令。

只检查 CI 明确钉版的工具（`pip install tool==X.Y.Z` 形式）；
未钉版的工具（走 `.[dev]` 或 `>=X`）不检查（向后兼容性假设）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github/workflows/ci.yml"


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


def get_local_version(tool: str) -> str | None:
    """获取本机工具版本（python -m tool --version）。"""
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


def main() -> None:
    pinned = extract_pinned_versions()
    if not pinned:
        print("SKIP: CI 配置未明确钉版任何工具")
        return

    mismatches: list[tuple[str, str, str | None]] = []
    for tool, ci_version in pinned.items():
        local_version = get_local_version(tool)
        if local_version != ci_version:
            mismatches.append((tool, ci_version, local_version))

    if not mismatches:
        print(f"PASS: {len(pinned)} 个钉版工具与 CI 一致")
        for tool, version in pinned.items():
            print(f"  {tool}=={version}")
        return

    print("FAIL: 本机工具版本与 CI 钉版不一致")
    print()
    for tool, ci_version, local_version in mismatches:
        local_str = local_version if local_version else "未安装或无法探测"
        print(f"  {tool}:")
        print(f"    CI 要求: {ci_version}")
        print(f"    本机版本: {local_str}")
    print()
    print("修复命令：")
    for tool, ci_version, _ in mismatches:
        print(f"  pip install {tool}=={ci_version}")
    sys.exit(1)


if __name__ == "__main__":
    main()
