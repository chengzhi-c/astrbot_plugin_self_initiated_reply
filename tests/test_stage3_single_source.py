"""阶段 3「单源锚定」断言：每个被消除的镜像配一条结构断言。

阶段 3 的主题是消除同一判断/同一表达式的多份实现（镜像）。行为测试管的是
"结果对不对"，管不住"是不是又抄了一份"——同一份口径在两个文件里各写一遍，
行为测试照样全绿，直到某天只改一边。本文件的断言落在**实现点数量**上：
用 AST 数调用者、看常量的引用者，而不是比对字面量文本。

风格对齐 ``tests/test_config_source_of_truth.py``。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from .source_contract import (
    _lookup,
    callers_of,
    calls_in,
    method_source,
    module_ast,
    source_of,
)

ROOT = Path(__file__).resolve().parents[1]


def _call_count(rel: str, qualname: str, target: str) -> int:
    """某个定义体内调用某目标表达式的次数。"""
    return sum(1 for name in calls_in(rel, qualname) if name == target)


def _production_modules() -> list[str]:
    """全仓生产模块的相对路径（含 image/ 与 scripts/，不含 tests/）。"""
    modules: list[str] = []
    for pattern in ("*.py", "image/*.py", "scripts/*.py"):
        modules.extend(path.relative_to(ROOT).as_posix() for path in sorted(ROOT.glob(pattern)))
    return modules


def _name_references(rel: str, target: str) -> list[str]:
    """模块内对该标识符的**任意**引用（AST 级，注释与文档串不计）。

    覆盖 ``Name``/属性访问/导入别名/``getattr`` 的字符串实参四种引入方式——
    只匹配 ``ast.Name`` 时，``_u.whitelist_storage_key`` 与
    ``getattr(_u, "whitelist_storage_key")`` 这两类写法都能溜过去。
    """
    hits: list[str] = []
    for node in ast.walk(module_ast(rel)):
        if isinstance(node, ast.Name) and node.id == target:
            hits.append(f"line {node.lineno}: {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr == target:
            hits.append(f"line {node.lineno}: {ast.unparse(node)[:60]}")
        elif isinstance(node, ast.alias) and node.name == target:
            hits.append(f"line {node.lineno}: import {node.name}")
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == target
        ):
            hits.append(f"line {node.lineno}: {node.value!r}")
    return hits


def _asserts_in(rel: str) -> list[str]:
    """某模块内全部 assert 语句的表达式文本（用于"这里不该有 assert"）。"""
    return [
        ast.unparse(node.test) for node in ast.walk(module_ast(rel)) if isinstance(node, ast.Assert)
    ]


def _colon_membership_probes(rel: str) -> list[str]:
    """模块内 ``":" in <x>`` 形式的段数探测（AST 判据，不受注释/文档串干扰）。"""
    probes: list[str] = []
    for node in ast.walk(module_ast(rel)):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Constant):
            continue
        if node.left.value != ":":
            continue
        if any(isinstance(operator, ast.In) for operator in node.ops):
            probes.append(ast.unparse(node))
    return probes


def _inline_regex_calls(rel: str, needles: tuple[str, ...]) -> list[str]:
    """模块内首参为字面量的 ``re.<fn>`` 调用（用于"这个模式该进常量"）。"""
    hits: list[str] = []
    for node in ast.walk(module_ast(rel)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if ast.unparse(node.func) not in {"re.sub", "re.search", "re.match", "re.split"}:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        if any(needle in first.value for needle in needles):
            hits.append(ast.unparse(node)[:70])
    return hits


# ============================================================================
# 3.1 群号启发式：完整 UMO 判据单点（utils.is_full_umo）
# ============================================================================


def test_full_umo_judgement_has_a_single_implementation() -> None:
    """裸冒号探测只能有一份实现（``utils.is_full_umo``）。

    裸冒号探测与 ``_UMO_PARTS`` 的段数语义并不等价（``a:b`` 会被算作完整 UMO），
    几处各写一遍时改口径必然漏一处；storage 的空状态补齐曾是第三份。
    """
    definitions = [
        node.name
        for node in ast.walk(module_ast("utils.py"))
        if isinstance(node, ast.FunctionDef) and node.name == "is_full_umo"
    ]
    assert definitions == ["is_full_umo"], f"is_full_umo 定义数异常：{definitions}"
    for rel in ("scheduler.py", "whitelist.py", "storage.py"):
        assert "is_full_umo" in source_of(rel), f"{rel} 未使用单点判据 is_full_umo"
    for rel in ("utils.py", "scheduler.py", "whitelist.py", "storage.py"):
        probes = _colon_membership_probes(rel)
        assert not probes, f"{rel} 又出现裸冒号 UMO 判断：{probes}"


# ============================================================================
# 3.2 message_ingress：代次推进只写一次
# ============================================================================


def test_accepted_content_invalidates_once() -> None:
    """被入口接住的消息推进代次，只在一处发生（合并后不得再抄回两分支）。"""
    body = method_source("message_ingress.py", "_accepted_content")
    count = body.count("invalidate(umo)")
    assert count == 1, f"message_ingress._accepted_content 里 invalidate(umo) 出现 {count} 次"


# ============================================================================
# 3.3 plugin_state：payload 构造单点
# ============================================================================


def test_state_payload_is_built_in_one_place() -> None:
    """同步/异步两条落盘路径共用 ``_build_payload``。"""
    owners = callers_of("plugin_state.py", "build_sessions_payload")
    assert owners == ["_build_payload"], (
        f"build_sessions_payload 的调用者应只有 _build_payload，实为 {owners}"
    )
    for name in ("save_storage_snapshot", "save_storage"):
        assert "_build_payload(plugin)" in method_source("plugin_state.py", name)


# ============================================================================
# 3.4 text 复位默认：只在读侧实现一次
# ============================================================================


def test_text_reset_default_lives_in_coerce_only() -> None:
    """写侧（webapi._strict_value）不得回落默认值，复位单点在 coerce。"""
    strict = method_source("webapi.py", "_strict_value")
    assert "reset_default" not in strict
    assert "reset_value" not in strict, (
        "写侧回落复位值会造出第二份口径：空提交在读侧才复位，写侧再落一次默认，"
        "「恢复默认 → 保存」就会被判成改过字段"
    )
    assert "reset_value" in method_source("models.py", "coerce_config_value")


# ============================================================================
# 3.6 extractor：贴纸过滤遍历单点
# ============================================================================


def test_sticker_filtering_has_one_traversal() -> None:
    """has_images 与 extract_images 共用 ``_eligible_image_entries``。"""
    owners = callers_of("image/extractor.py", "_eligible_image_entries")
    assert owners == ["ImageExtractor.extract_images", "ImageExtractor.has_images"], (
        f"_eligible_image_entries 的调用者应只有这两个判定入口，实为 {owners}"
    )
    assert "_eligible_image_entries" in callers_of("image/extractor.py", "_component_is_sticker")


# ============================================================================
# 3.9 / 3.18 webapi：关停门与运维端点定位
# ============================================================================


def test_mutating_webapi_endpoints_check_stopping() -> None:
    """会落盘的 POST 端点必须与 config 同口径地拒绝关停中写入。"""
    for name in ("_api_post_ui_theme", "_api_post_config", "_api_cleanup_image_cache"):
        assert "plugin._stopping" in method_source("webapi.py", name), (
            f"{name} 未检查 _stopping：teardown 之后写入的数据下次启动会被读回"
        )


def test_status_endpoint_is_declared_ops_only() -> None:
    """``/status`` 是运维端点：实现与契约文档都要写明面板零消费。"""
    assert "面板零消费" in method_source("webapi.py", "_api_status")
    contract = (ROOT / "docs" / "BEHAVIOR_CONTRACT.md").read_text(encoding="utf-8")
    assert "/status" in contract and "面板零消费" in contract


# ============================================================================
# 3.10 legacy 迁移：一次性、且在载入路径
# ============================================================================


def test_legacy_state_migration_is_not_on_the_read_path() -> None:
    """``state_for`` 不得再改写 ``sessions``；迁移是 ``load_sessions`` 的唯一调用。

    用调用者清单而不是子串匹配：子串断言里在 ``load_sessions`` 留一句带该名的
    注释就能通过，而"谁在调用"才是这条契约本身。且清单要扫**全仓**——只看
    storage.py 时，在别处新加一个调用点不会被发现。

    ``state_for`` 一侧禁的是整类写旁路：``del``/``pop``/``popitem``/``clear``/
    ``update``/``setdefault`` 都是"顺手改一下"的不同写法，只挡其中两种等于没挡。
    """
    for node in ast.walk(module_ast("plugin_state.py")):
        if not (isinstance(node, ast.FunctionDef) and node.name == "state_for"):
            continue
        writes = [
            ast.unparse(child)
            for child in ast.walk(node)
            if isinstance(child, ast.Delete)
            or (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in {"pop", "popitem", "clear", "update", "setdefault"}
                and "sessions" in ast.unparse(child.func.value)
            )
        ]
        assert not writes, f"state_for 又在热路径上做写操作（迁移/清理）：{writes}"
        break
    else:
        raise AssertionError("plugin_state.py 里找不到 state_for")

    callers = [
        (rel, name)
        for rel in _production_modules()
        for name in callers_of(rel, "_migrate_legacy_group_keys")
    ]
    assert callers == [("storage.py", "load_sessions")], (
        f"legacy 迁移的调用点不再是 load_sessions 单点：{callers}"
    )


# ============================================================================
# 3.11 assert → 显式 raise（-O 下 assert 会被剥掉）
# ============================================================================


def test_runtime_invariants_do_not_use_assert() -> None:
    """运行期不变式用 if/raise，不用 assert（``python -O`` 会剥除 assert）。"""
    for rel in ("models.py", "generation.py"):
        asserts = _asserts_in(rel)
        assert not asserts, f"{rel} 残留 assert 形式的运行期不变式：{asserts}"


# ============================================================================
# 3.12 SendAttempt：身份判等
# ============================================================================


def test_send_attempt_compares_by_identity() -> None:
    """SendAttempt 必须 ``eq=False``：账本按身份判成员，值相等会跨账本误判。"""
    for node in ast.walk(module_ast("models.py")):
        if not (isinstance(node, ast.ClassDef) and node.name == "SendAttempt"):
            continue
        decorators = [
            decorator
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call) and ast.unparse(decorator.func) == "dataclass"
        ]
        assert decorators, "SendAttempt 不再是 dataclass"
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in decorators[0].keywords}
        assert keywords.get("eq") == "False", (
            "SendAttempt 未设 eq=False：两个账本里字段全等的 attempt 会互相冒充成员，"
            "resolve/mark_in_flight 会把状态写到别的账本上"
        )
        return
    raise AssertionError("models.py 里找不到 SendAttempt")


# ============================================================================
# 3.13 style.css：分节标记与头注释同源
# ============================================================================


def test_css_sections_match_header_declaration() -> None:
    """头注释宣称的分节必须与文件内的实际标记**逐字同序**一致。

    只查"标记名出现在头注释里"会漏报子串退化：把标记改成「控件」而注释里
    仍是「控件（输入、开关、按钮）」时两边都能找到对方，漂移照样通过。
    """
    css = (ROOT / "pages" / "主动回复设置" / "style.css").read_text(encoding="utf-8")
    markers = re.findall(r"/\* ==== (.+?) ==== \*/", css)
    assert markers, "style.css 没有任何分节标记"
    header = css[: css.index("*/")]
    declared = [
        line.strip()
        for line in header.split("）：", 1)[1].splitlines()
        if line.strip() and not line.strip().startswith("===")
    ]
    assert declared == markers, (
        f"头注释声明的分节与文件内标记不一致：\n声明={declared}\n标记={markers}"
    )
    for name in markers:
        assert css.count(f"/* ==== {name} ==== */") == 1, f"分节标记重复：{name}"


# ============================================================================
# 3.14 状态键：调用方传 UMO，键在 plugin_state 派生
# ============================================================================


def test_storage_key_is_derived_in_plugin_state_only() -> None:
    """状态键的派生点只有 ``plugin_state``：其余模块一律传 UMO，不自己算键。

    ``whitelist_storage_key`` 是「状态键是什么」的唯一命名接缝。调用方各自
    先算键再传时，改一次口径要全仓搜；入口（message_ingress/commands）、
    scheduler、pipeline、whitelist 都曾各算一份。

    判据用标识符级扫描而非 ``ast.Name`` 匹配：``_u.whitelist_storage_key``、
    ``getattr(_u, "whitelist_storage_key")`` 与导入别名都是同一契约的绕过写法。
    扫描面覆盖**全部**生产模块（排除持有实现的三处），不再限定 5 个文件——
    限定清单时，往任何未列出的模块里加调用点都不会被发现。
    """
    allowed = {"utils.py", "storage.py", "plugin_state.py"}
    offenders = {
        rel: hits
        for rel in _production_modules()
        if rel not in allowed
        for hits in [_name_references(rel, "whitelist_storage_key")]
        if hits
    }
    assert not offenders, f"这些模块又自行派生状态键：{offenders}"

    assert "whitelist_storage_key(umo)" in method_source("plugin_state.py", "state_for")


# ============================================================================
# 3.16 发布脚本：成员路径规范化与禁运名单单点
# ============================================================================


def test_archive_member_normalization_has_one_home() -> None:
    """三个发布脚本不得再手抄分隔符归一的表达式。"""
    for name in ("check_sdist.py", "check_wheel.py", "make_release_zip.py"):
        text = source_of(f"scripts/{name}")
        assert 'replace("\\\\", "/")' not in text, f"{name} 又手抄了分隔符归一"
        assert "normalize_member" in text, f"{name} 未使用共享的 normalize_member"


def test_deploy_zip_forbidden_prefixes_derive_from_wheel_check() -> None:
    """deploy zip 的开发物前缀必须派生自 check_wheel 的权威名单。"""
    text = source_of("scripts/make_release_zip.py")
    assert "import FORBIDDEN_PREFIXES as DEV_PREFIXES" in text, (
        "make_release_zip 又维护了一份独立的开发物前缀窄名单"
    )
    assert "DEV_PREFIXES = (" not in text


# ============================================================================
# 3.17 常量与正则单点
# ============================================================================


def test_recent_limit_default_has_one_source() -> None:
    """``recent_message_limit`` 的默认值只声明一次。"""
    models = source_of("models.py")
    assert "RECENT_MESSAGE_LIMIT_DEFAULT" in models
    assert "deque(maxlen=20)" not in models, "SessionState 的兜底 maxlen 又硬编码了"
    assert '"recent_message_limit",' in models
    assert "RECENT_MESSAGE_LIMIT_DEFAULT" in method_source("models.py", "SessionState")


def test_whitespace_patterns_are_not_recompiled_inline() -> None:
    """空白正则单点在 models：utils/models 都不再内联 ``re.sub`` 空白模式。"""
    for rel in ("utils.py", "models.py"):
        inline = _inline_regex_calls(rel, ("\\s", "[^\\S"))
        assert not inline, f"{rel} 残留内联空白正则：{inline}"
    utils = source_of("utils.py")
    assert "WHITESPACE_PATTERN" in utils and "INLINE_SPACE_PATTERN" in utils
    assert "_WHITESPACE_PATTERN" not in utils, "utils 又自持一份空白正则"
    assert "_INLINE_SPACE_PATTERN" not in utils


# ============================================================================
# 3.19 首次规范化落盘的失败必须可见
# ============================================================================


def test_startup_persist_failure_is_not_swallowed() -> None:
    """``persist_settings_config`` 的返回值在启动路径上必须被消费。

    "被消费"的**行为**断言在 ``tests/test_main_runtime.py``
    （``test_startup_persist_failure_is_logged``，注入返回 False 的实现后要求
    出现 ERROR 日志）——本文件只补源码层的裸调用守卫：``if False and not
    persist(...)`` 这类"保留了分支却不再执行"的写法行为测试能抓，而裸调用
    与"只赋值不使用"只有这里能一眼看全。
    """
    bare_calls = [
        ast.unparse(node)
        for node in ast.walk(module_ast("main.py"))
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and "persist_settings_config" in ast.unparse(node.value)
    ]
    assert not bare_calls, f"启动路径又吞掉了 persist_settings_config 的返回值：{bare_calls}"
    assert _call_count("main.py", "SelfInitiatedReplyPlugin.__init__", "logger.error") >= 1


def _reset_value_expressions(rel: str, qualname: str) -> list[str]:
    """定义体内取 ``CONFIG_SPEC_BY_KEY["decision_prompt_template"].reset_value`` 的表达式。

    AST 级判定：``"reset_value" in 源码`` 这类子串检查会被注释满足（上一轮
    P2 批评过的同名注释手法），而且读法要钉准是**规格表取默认值**这一条，
    不是随便一处属性访问。
    """
    hits: list[str] = []
    for node in ast.walk(_lookup(rel, qualname)):
        if not (isinstance(node, ast.Attribute) and node.attr == "reset_value"):
            continue
        subscript = node.value
        if not isinstance(subscript, ast.Subscript):
            continue
        table = ast.unparse(subscript.value)
        key = subscript.slice
        if table == "CONFIG_SPEC_BY_KEY" and ast.unparse(key) == "'decision_prompt_template'":
            hits.append(ast.unparse(node))
    return hits


def test_default_prompt_is_consumed_through_the_spec() -> None:
    """默认判断提示词只经 ``ConfigSpec.reset_value`` 消费。

    同一默认值此前有四处各自的 ``.strip()`` 口径（coerce 收口、webapi 面板填充、
    decision 回落、``Settings.decision_prompt_custom`` 的比对）。模板常量字形一旦
    带首尾空白，四副面孔就会漂移成「恢复默认 → 保存被误报改过字段」与"喂给模型的
    默认值不等于面板显示的默认值"。本断言钉住消费面：除 models.py（定义与
    ``reset_default`` 声明）外，全仓生产模块都不得再出现模板常量名；三个消费点
    必须写规格表取值这一条表达式——均按 AST 判，注释与文档串不算数。
    """
    offenders = {
        rel: hits
        for rel in _production_modules()
        if rel != "models.py"
        for hits in [_name_references(rel, "DEFAULT_DECISION_PROMPT_TEMPLATE")]
        if hits
    }
    assert not offenders, f"这些模块又直接引用模板常量：{offenders}"

    for rel, qualname in (
        ("decision.py", "build_decision_prompt"),
        ("webapi.py", "_api_get_config"),
        ("models.py", "decision_prompt_custom"),
    ):
        assert _reset_value_expressions(rel, qualname), (
            f"{rel}.{qualname} 未从规格表取默认值："
            f"{_name_references(rel, 'reset_value') or '（无 reset_value 引用）'}"
        )
