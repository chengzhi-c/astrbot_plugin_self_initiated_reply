"""Wheel 内容断言：开发物不得泄漏，运行时必需文件必须存在。

与 coverage_gates.py 同模式，由 CI build 作业在 `hatch build` 之后强制。
依据（2026-08-07 实测）：整目录打包会把 tests/.scratch/scripts/docs/.github
与开发配置全部打进生产 wheel（原 0.8.3 wheel 102 文件 / 2.8MB），
pyproject 的 exclude 修复后必须由本脚本断言闭环，防止配置漂移复发。

用法：先 `hatch build`（或 pip wheel .），再从仓库根执行
`python scripts/check_wheel.py`。
"""

from __future__ import annotations

import glob
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 禁止出现的开发物前缀（相对 wheel 内路径）
FORBIDDEN_PREFIXES = ("tests/", ".scratch/", "scripts/", "docs/", ".github/", ".gitignore")
# 必须存在的运行时文件（相对 wheel 内路径）
REQUIRED_FILES = ("metadata.yaml", "_conf_schema.json", "main.py", "logo.png", "pages/")
# 开发配置类：wheel 内不应出现
FORBIDDEN_SUFFIXES = (".pre-commit-config.yaml",)


def _find_wheel() -> Path:
    candidates = sorted(glob.glob(str(ROOT / "dist" / "*.whl")))
    if not candidates:
        print("FAIL: 未找到 wheel，请先执行 hatch build 或 pip wheel .")
        sys.exit(1)
    # 取字典序最后一个（dist/ 下通常只有一个 wheel；多版本并存时取最高版本号）
    return Path(candidates[-1])


def _expected_version() -> str:
    """metadata.yaml 的发布版本（wheel 文件名与 dist-info 的基准）。"""
    import re

    meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version: (.+)$", meta, re.M)
    if match is None:
        print("FAIL: metadata.yaml 缺少 version 字段")
        sys.exit(1)
    return match.group(1).strip()


def main() -> int:
    wheel = _find_wheel()
    expected = _expected_version()
    print(f"检查 wheel: {wheel.name} ({wheel.stat().st_size / 1024:.0f} KB)")
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        dist_meta = [n for n in names if n.endswith(".dist-info/METADATA")]
        dist_text = zf.read(dist_meta[0]).decode("utf-8") if dist_meta else ""

    failures: list[str] = []
    for name in names:
        # hatchling 生成的 wheel 路径带 "./" 前缀，规范化后再断言
        normalized = name.replace("\\", "/").lstrip("./")
        if normalized.startswith(FORBIDDEN_PREFIXES) or normalized.endswith(FORBIDDEN_SUFFIXES):
            failures.append(f"开发物泄漏: {normalized}")

    for required in REQUIRED_FILES:
        if not any(name.replace("\\", "/").lstrip("./").startswith(required) for name in names):
            failures.append(f"缺少必需文件: {required}")

    # 版本一致性（0.8.3 发布缺陷回归守卫：wheel 文件名与 dist-info 曾停在 0.8.3）
    if expected not in wheel.name:
        failures.append(f"wheel 文件名 {wheel.name} 不含版本 {expected}")
    if not dist_meta:
        failures.append("wheel 内缺少 .dist-info/METADATA")
    elif f"Version: {expected}" not in dist_text:
        failures.append(f"dist-info 版本与 metadata {expected} 不一致")

    if failures:
        print("FAIL:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(f"OK: {len(names)} 个文件，无开发物泄漏，必需文件齐全，版本 {expected} 一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
