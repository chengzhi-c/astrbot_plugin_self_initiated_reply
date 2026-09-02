"""长函数/高复杂度函数的 docstring 硬规则：违规即 CI 变红。

与 coverage_gates.py 同模式（规则化断言，不追百分比）。

为什么是规则而不是覆盖率百分比：无差别把 docstring 补到 90% 只会产出
「返回 x。」这类同义反复的噪音，反而稀释真正需要解释的地方。真正需要文字的是
两类函数——长到读者无法一屏看完流程的，和分支多到无法一眼推断失败行为的。
本脚本只对这两类断言，其余函数由命名与类型注解自解释。

规则（0.9.3 B4 引入，0.9.3 复审修正 CC 阈值）：
  行数 > 50 或 圈复杂度 >= 12 的函数必须有 docstring，
  说明「做什么 + 失败时怎样」。
  所有生产函数的圈复杂度必须 <= 21，不能靠补 docstring 合法化继续增长。

阈值口径与既有门槛一致：**行数阈值**只升不降。放宽等于让新增的长函数免检，
属回归——若某函数确实无需 docstring，正确做法是把它拆短，而不是调阈值。

CC 阈值为何是 12 而不是初版的 16（复审实测修正，别再改回去）：
初版两个阈值都从「当前实测最大值」反推，结果 CC 那一半从落地起就是**死规则**——
实测全仓行数 <= 50 的函数里 CC 最高只有 14，永远够不到 16，24 个命中全部由
行数触发。阈值必须由「规则想抓什么」决定，而不是由「当前恰好有什么」决定：
反推法只能保证当下全绿，代价是规则失去作用。降到 12 后独立命中 3 个短高复杂度
函数（adapters.read_astrbot_history CC=14、recorder_bridge.get_local_image_path
CC=14、parser._fetch_image_data_url CC=12），其中 2 个缺 docstring，已在同次复审
补齐。因此 CC 阈值属**修正后生效**：下调至此不是放宽门禁，而是让门禁开始工作。

用法：从仓库根执行 `python scripts/docstring_gates.py`（无需先跑测试）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 超过此行数（含 def 行与 docstring）必须有 docstring
MAX_LINES_WITHOUT_DOC = 50
# 圈复杂度达到此值必须有 docstring（粗估口径见 _complexity）
# 12 是复审修正值，理由见模块 docstring —— 初版 16 是死规则，勿改回。
MIN_CC_REQUIRING_DOC = 12
# 三个历史热点拆分后，剩余生产函数实测最大值为 21。统一拒绝更高复杂度；
# 不按文件或函数名豁免，也不为降低数字顺手拆解职责仍集中的函数。
MAX_CC = 21

# 生产代码口径：与 coverage_gates.py 一致，排除测试与脚本自身。
# .scratch/ 是 git 忽略的本地草稿区（报告、临时探针），不是交付物：
# 实测它此前会被 rglob 收进来，一个 60 行的临时探针就能让门禁 FAIL，
# 且 ast.parse 不传 filename，报错定位显示为 <unknown> 难以溯源。
# .venv/ / venv/ 同理：仓库内本地虚拟环境会被 rglob 扫进生产口径，
# 第三方库函数 CC 远超 MAX_CC，本地门禁会红而 CI（无 .venv）仍绿。
EXCLUDED_PREFIXES = ("tests/", "scripts/", ".scratch/", ".venv/", "venv/")

# 圈复杂度粗估计入的分支节点。口径固定，避免换算法导致阈值失去可比性。
_BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.And,
    ast.Or,
    ast.IfExp,
    ast.Assert,
)


def _complexity(node: ast.AST) -> int:
    """圈复杂度粗估：1 + 分支节点计数。

    刻意不用第三方工具（radon 等），因为本项目运行时零第三方依赖，
    开发依赖也应保持最小。粗估口径足够稳定地识别"分支多到需要解释"的函数。
    """
    return 1 + sum(isinstance(child, _BRANCH_NODES) for child in ast.walk(node))


def _iter_production_files() -> list[Path]:
    files = []
    for path in sorted(ROOT.rglob("*.py")):
        try:
            relative = path.resolve().relative_to(ROOT)
        except ValueError:
            continue
        text = str(relative).replace("\\", "/")
        if "__pycache__" in text or text.startswith(EXCLUDED_PREFIXES):
            continue
        files.append(path)
    return files


def main() -> int:
    docstring_violations: list[str] = []
    complexity_violations: list[str] = []
    checked = 0

    for path in _iter_production_files():
        relative = str(path.resolve().relative_to(ROOT)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print(f"FAIL {relative}: 语法错误无法解析：{exc}")
            return 1

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lines = (node.end_lineno or node.lineno) - node.lineno + 1
            cc = _complexity(node)
            if cc > MAX_CC:
                complexity_violations.append(
                    f"{relative}:{node.lineno} {node.name}（CC≈{cc} > {MAX_CC}）"
                )
            long_enough = lines > MAX_LINES_WITHOUT_DOC
            complex_enough = cc >= MIN_CC_REQUIRING_DOC
            if not (long_enough or complex_enough):
                continue
            checked += 1
            if ast.get_docstring(node) is not None:
                continue
            reasons = []
            if long_enough:
                reasons.append(f"{lines} 行 > {MAX_LINES_WITHOUT_DOC}")
            if complex_enough:
                reasons.append(f"CC≈{cc} >= {MIN_CC_REQUIRING_DOC}")
            docstring_violations.append(
                f"{relative}:{node.lineno} {node.name}（{' 且 '.join(reasons)}）"
            )

    if complexity_violations:
        print(f"FAIL: {len(complexity_violations)} 个生产函数超过统一复杂度上限：")
        for item in complexity_violations:
            print(f"  - {item}")
    if docstring_violations:
        print(f"FAIL: {len(docstring_violations)}/{checked} 个命中规则的函数缺少 docstring：")
        for item in docstring_violations:
            print(f"  - {item}")
    if complexity_violations or docstring_violations:
        print()
        print("请拆分真实职责，或补 docstring 说明「做什么 + 失败时怎样」。")
        print("不要为了通过而调高本脚本的阈值。")
        return 1

    print(
        f"OK: {checked} 个命中规则的函数（>{MAX_LINES_WITHOUT_DOC} 行 或 "
        f"CC>={MIN_CC_REQUIRING_DOC}）全部有 docstring；生产函数 CC<={MAX_CC}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
