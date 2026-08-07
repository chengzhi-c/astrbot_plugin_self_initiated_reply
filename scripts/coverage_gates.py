"""模块级覆盖率门槛：关键模块低于阈值即 CI 变红。

与 pyproject.toml 的 fail_under（整体门槛）互补：整体门槛会被高覆盖模块
掩盖局部盲区。本脚本按文件粒度校验关键模块，口径与整体门槛一致（生产
口径：排除 tests/），阈值按"实测 - 5%"留缓冲（同 fail_under 规约）。

用法：先跑 pytest 生成 .coverage，再从仓库根执行 `python scripts/coverage_gates.py`。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import coverage

# 文件路径（相对仓库根）→ 最低覆盖率百分比。
# 实测基线（2026-08-07，Py 3.14.5，569 tests，0.9.0 D1 补盲后）：main.py 96%、
# image/parser.py 100%、scheduler.py 98%、decision.py 97%、generation.py 85%、
# delivery.py 94%、whitelist.py 100%、session_coordinator.py 96%、storage.py 95%。
# events.py 已并入 utils.py（0.9.0 B4），对应门槛同步移除。
# 阈值随补盲推进上调，禁止下调（调低即回归）；跨 Python 版本留缓冲，
# 故部分阈值低于公式值（实测 − 5%）属有意为之。
THRESHOLDS = {
    # 08 补盲后 69% → 96%，门槛按 ticket 目标定 80（留缓冲）
    "main.py": 80,
    # 09 补盲后 63.4% → 100%，门槛按 ticket 目标定 80（跨 Python 版本留缓冲）
    "image/parser.py": 80,
    # 0.9.0 D1 巡检/清理循环补盲后 76% → 98%，实测 − 5%
    "scheduler.py": 93,
    # 03 拆分后新模块，实测 97% − 5% 缓冲
    "decision.py": 92,
    # 04 拆分后新模块，实测 85% − 5% 缓冲
    "generation.py": 80,
    # 0.9.0 D1 发送异常分支补盲后 74% → 94%，实测 − 5%
    "delivery.py": 89,
    # 06 拆分后新模块，实测 100% − 5% 缓冲
    "whitelist.py": 95,
    # 07 拆分后新模块，实测 96% − 5% 缓冲
    "session_coordinator.py": 91,
    # 0.9.0 D1 容错分支补盲后 72% → 95%，实测 − 5%
    "storage.py": 90,
}

ROOT = Path(__file__).resolve().parents[1]


def _relative(fname: str) -> str:
    try:
        return str(Path(fname).resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(fname).replace("\\", "/")


def main() -> int:
    cov = coverage.Coverage()
    cov.load()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "coverage.json"
        try:
            cov.json_report(outfile=str(out))
        except coverage.exceptions.NoDataError:
            print("未找到覆盖率数据（.coverage）。请先运行 pytest 再执行本脚本。")
            return 1
        report = json.loads(out.read_text(encoding="utf-8"))
    files = report.get("files", {})
    by_relative = {_relative(fname): entry for fname, entry in files.items()}

    failures: list[str] = []
    for relative, threshold in sorted(THRESHOLDS.items()):
        entry = by_relative.get(relative)
        if entry is None:
            failures.append(f"{relative}: 未测到（模块未被测量）")
            continue
        summary = entry["summary"]
        statements = summary["num_statements"]
        covered = summary["covered_lines"]
        percent = 100.0 * covered / statements if statements else 100.0
        if percent >= threshold:
            print(f"PASS {relative}: {percent:.1f}% (threshold {threshold}%)")
        else:
            print(f"FAIL {relative}: {percent:.1f}% (threshold {threshold}%)")
            failures.append(f"{relative}: {percent:.1f}% < {threshold}%")
    if failures:
        print("模块级覆盖率门槛未达标：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
