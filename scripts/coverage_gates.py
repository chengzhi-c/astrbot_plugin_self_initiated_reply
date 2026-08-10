"""模块级覆盖率门槛：关键模块低于阈值即 CI 变红。

与 pyproject.toml 的 fail_under（整体门槛）互补：整体门槛会被高覆盖模块
掩盖局部盲区。本脚本按文件粒度校验关键模块，口径与整体门槛一致（生产
口径：排除 tests/），阈值按"实测 - 5%"留缓冲（同 fail_under 规约）。

未覆盖行不是一律该补的。宿主异常兜底、防御性早退、降级日志三类刻意不补
（mock 硬凑会锁死宿主 API 实现细节，宿主升级时先红的是测试）；宿主对象
形态变体是真实逻辑，必须补测。改阈值前先读 pyproject.toml 的 fail_under
说明与本文件各条阈值上方的来历注释。

用法：先跑 pytest 生成 .coverage，再从仓库根执行 `python scripts/coverage_gates.py`。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import coverage

# 文件路径（相对仓库根）→ 最低覆盖率百分比。
# 每条阈值下方的行内注释记录它的来历（补盲前后实测值 + 定档理由），是修改
# 阈值时唯一需要读的依据；本脚本运行时会打印各模块当前实测值，故此处不再
# 复制一份会过期的基线快照。
# events.py 已并入 utils.py（0.9.0 B4），对应门槛同步移除。
# runtime_adapter.py 为宿主兼容层（宿主升级最易出问题），P2 补盲后入门禁。
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
    # 04 拆分后新模块。0.9.4 阶段 4 清偿 tracked_send 透传欠账后实测 89.1%。
    # 刻意不按本文件的"实测 − 5%"惯例取 84：实测三种回退（退 244/245 → 88.24%、
    # 再退 409 恢复分支断言 → 87.78%）在 84 门槛下**全部仍能通过**，等于白升。
    # 取 89 才真正锁住这三行，代价是缓冲仅 0.14 个百分点；剩余 24 行中 19 行是
    # A 类明确不补，语句总数不变时该缓冲只会被
    # "新增未覆盖行"吃掉，而那按维护约定 1 本就该先分类再决定，不该被缓冲吸收。
    "generation.py": 89,
    # 0.9.0 D1 发送异常分支补盲后 74% → 94%，实测 − 5%
    "delivery.py": 89,
    # 06 拆分后新模块，实测 100% − 5% 缓冲
    "whitelist.py": 95,
    # 07 拆分后新模块，实测 96% − 5% 缓冲
    "session_coordinator.py": 91,
    # 0.9.0 D1 容错分支补盲后 72% → 95%，实测 − 5%
    "storage.py": 90,
    # 0.9.0 P2 降级宿主分支补盲后 86% → 100%，实测 − 5%；
    # 宿主兼容层升级风险最高，门禁守护防回归
    "runtime_adapter.py": 95,
    # 0.9.3 C2：盲区分类后补真实逻辑测试（event_umo 群组改写 / dedupe 保留最新 /
    # reason 截断 / content_to_text 多形态），86% → 91.3%，实测 − 5%。
    # 此前它是三个薄弱模块中唯一无门禁者——补测收益无人守护。
    # 0.9.4 欠账清理批次：清偿 323/379/394/405/407/501 后实测 93.9%（290/309）。
    # 同 generation.py，不按"实测 − 5"取 88（那会让下面全部回退都照样通过），
    # 改为实测回退定档。93 是**能通过的最紧值**（94 会当场红：实测 93.85 < 94）：
    #   撤全部新用例 → 91.3%（27 缺）FAIL ✓   撤 is_admin 一条 → 92.9%（22 缺）FAIL ✓
    #   撤别名两条  → 92.9%（22 缺）FAIL ✓
    #   撤 405 一条 → 93.5%（20 缺）**仍 PASS ✗**（撤 501 同）
    # 即：本门槛锁住"成组回退"，锁不住"单撤 405 / 501 这类只值 1 行的用例"。
    # 那两条的真实保护来自用例自身的语义断言而非数字（等价变异体不锁数字），
    # 不为了锁它们把门槛设成注定失败的 94。
    "utils.py": 93,
    # 0.9.3 C2：组件类型枚举形态与 URL/file scheme 互换补测，85% → 87.4%，实测 − 5%。
    # 剩余盲区按 A/B/C/D 四类判定留痕，不再追数字。
    # 0.9.4 欠账清理批次：清偿 42/202（混合消息的图文配对）后实测 88.4%（175/198）。
    # 取 88 而非"实测 − 5"的 83：实测回退**两种都被挡住**——
    #   撤任一条 → 87.9%（24 缺）FAIL ✓     撤两条 → 87.4%（25 缺）FAIL ✓
    # 缓冲 0.38 个百分点，与 generation.py 89 同类取舍（见该条注释）。
    "image/extractor.py": 88,
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
