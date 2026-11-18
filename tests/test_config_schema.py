"""配置 schema（_conf_schema.json）与 CONFIG_SCHEMA_KEYS 一致性守卫。

schema 驱动 AstrBot 设置面板渲染；CONFIG_SCHEMA_KEYS 决定 webapi 接受哪些
配置键（名单之外一律 fail loud）。二者漂移的两种后果：
- schema 有而 KEYS 无：面板字段提交被拒（400 未知键）；
- KEYS 有而 schema 无：字段不在面板上，形同死配置。
硬性断言 schema 键 == CONFIG_SCHEMA_KEYS：二者一一对应（无兼容别名层），
改任何一侧都必须同步另一侧，否则变红。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from .host_stubs import ROOT

PACKAGE = "selfreply_main_test_package"


@pytest.fixture(autouse=True)
def _bootstrap():
    from .host_stubs import load_main

    load_main()
    yield


def _webapi() -> Any:
    return sys.modules[f"{PACKAGE}.webapi"]


def _schema() -> dict[str, Any]:
    return json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))


def _schema_keys() -> set[str]:
    return set(_schema().keys())


def _models() -> Any:
    return sys.modules[f"{PACKAGE}.models"]


# schema 键 → Settings 字段名（唯一一处不同名：白名单）
_FIELD_ALIAS = {"whitelist_sessions": "whitelist"}

# 有意的空默认：schema 留空 = 用 Python 内置默认（8000 字判断提示词模板）。
# 面板留空即恢复默认是产品语义，不是漂移；此处显式收纳，防止被"修正"。
_INTENTIONAL_EMPTY_DEFAULT = {"decision_prompt_template"}


def test_list_specs_declare_one_machine_normalization_contract() -> None:
    """每个字符串 list/set 都必须声明容量、条目规则和空值策略。"""
    models = _models()
    list_specs = [spec for spec in models.CONFIG_SPECS if spec.kind == "list"]
    assert list_specs
    for spec in list_specs:
        assert spec.max_items is not None, spec.key
        assert spec.item_max_len is not None, spec.key
        assert spec.item_pattern, spec.key


def test_config_revision_is_canonical_and_restart_stable() -> None:
    """集合顺序不影响摘要，运行态不进入持久配置 revision。"""
    models = _models()
    first = models.Settings.from_config(
        {"ignored_sender_ids": ["b", "a"], "whitelist_sessions": ["group-b", "group-a"]}
    )
    second = models.Settings.from_config(
        {"ignored_sender_ids": ["a", "b"], "whitelist_sessions": ["group-a", "group-b"]}
    )
    assert models.config_revision(first) == models.config_revision(second)
    assert models.config_revision(first).startswith("sha256:")


def test_set_normalization_deduplicates_before_capacity() -> None:
    """set 容器的重复项不能消耗容量，排列变化不得改变保留集合。"""
    models = _models()
    spec = models.CONFIG_SPEC_BY_KEY["whitelist_sessions"]
    limit = int(spec.max_items)
    unique = [f"session-{index:04d}" for index in range(limit + 2)]
    first = models.normalize_string_list(spec, unique + [unique[0]], mode="disk")
    second = models.normalize_string_list(spec, list(reversed(unique)) + [unique[-1]], mode="disk")
    assert first == second
    assert len(first) == limit
    assert first == set(sorted(unique)[:limit])


def test_api_mode_set_capacity_applies_after_canonical_sort() -> None:
    """api 模式同样「先去重排序，再截容量」：提交排列不得改变保留值。

    缺陷形态：``normalize_string_list`` 在规范化前就 ``raw = raw[:max_items]``，
    于是 1000 个 ``z-*`` 打头就整批占满容量，排序后本应保住的 ``a-*`` 全被丢弃。
    """
    models = _models()
    spec = models.CONFIG_SPEC_BY_KEY["whitelist_sessions"]
    limit = int(spec.max_items)
    later = [f"z-{index:04d}" for index in range(limit)]
    earlier = [f"a-{index:04d}" for index in range(limit)]
    assert models.normalize_string_list(spec, later + earlier, mode="api") == earlier
    assert models.normalize_string_list(spec, earlier + later, mode="api") == earlier


def test_api_mode_validates_items_beyond_capacity() -> None:
    """容量之外的尾部条目仍要过类型与长度校验，不得因截断被跳过。

    缺陷形态：前置截断让 ``[1000 条合法项, None]`` 整体成功保存——尾部非法项
    从未被检查，用户提交里的 None/超长/非法字符被静默吞掉。
    """
    models = _models()
    spec = models.CONFIG_SPEC_BY_KEY["whitelist_sessions"]
    legal = [f"s-{index:04d}" for index in range(int(spec.max_items))]
    cases = (
        ([*legal, None], "条目必须是字符串"),
        ([*legal, "q" * (models.MAX_STRING_LIST_ITEM_LEN + 1)], "条目过长"),
        ([*legal, 'bad"quote'], "条目含非法字符"),
    )
    for submitted, message in cases:
        with pytest.raises(ValueError, match=message):
            models.normalize_string_list(spec, submitted, mode="api")


def test_api_mode_rejects_non_string_list_items() -> None:
    """api 模式拒收非字符串条目：str(None) 会变成活条目 "None"。

    缺陷形态：_normalize_list_item 用 str(raw) 兜底，[1, null, true] 被规范成
    ["1", "None", "True"] 持久化进白名单/别名，而集合比较通过、UI 无感知。
    disk 模式对不可信落盘数据保持宽松（丢弃），由 from_config 使用。
    """
    models = _models()
    spec = models.CONFIG_SPEC_BY_KEY["whitelist_sessions"]
    for raw in (None, True, 1, {"a": 1}, ["b"]):
        try:
            models.normalize_string_list(spec, [raw], mode="api")
        except ValueError as exc:
            assert "条目必须是字符串" in str(exc)
        else:
            raise AssertionError(f"api mode accepted non-string item: {raw!r}")
    assert models.normalize_string_list(spec, [None, "ok", 2], mode="disk") == {"ok"}


def test_api_mode_splits_commas_like_the_page() -> None:
    """api 模式与设置页 textarea 同一套分隔符，["a,b"] 必须拆成两条。

    缺陷形态：页面按 [\\n,，]+ 切分展示与保存，后端原样存成一条；下一轮
    加载页面把它拆成两条，一个会话被无声劈成两个（裸 id 还是群通配符），
    计数与磁盘长期不一致。
    """
    models = _models()
    spec = models.CONFIG_SPEC_BY_KEY["whitelist_sessions"]
    # set 容器：api 模式返回排序 list（JSON 友好），disk 模式返回 set
    assert models.normalize_string_list(spec, ["a,b", "c\nd，e"], mode="api") == [
        "a",
        "b",
        "c",
        "d",
        "e",
    ]
    # disk 模式不切分：手改落盘文件里的逗号条目原样载入，下一轮保存才规范化
    assert models.normalize_string_list(spec, ["a,b"], mode="disk") == {"a,b"}


def test_list_trim_matches_the_page_trim() -> None:
    """条目 trim 必须与设置页 String.prototype.trim() 同字符集。

    缺陷形态：裸 str.strip() 连 U+0085（NEL）一起剥，而 JS trim 不剥；
    页面校验放行的 ``\\x85abc\\x85`` 被后端静默改写成 ``abc``，保存再加载后
    内容与用户输入不一致（差分模糊测试实测的唯一分歧源）。
    """
    models = _models()
    spec = models.CONFIG_SPEC_BY_KEY["whitelist_sessions"]
    assert models.normalize_string_list(spec, ["\x85abc\x85"], mode="api") == ["\x85abc\x85"]
    # JS trim 会剥的字符照剥
    assert models.normalize_string_list(spec, ["\u00a0abc\u2028"], mode="api") == ["abc"]


def test_malformed_formal_list_uses_legacy_fallback() -> None:
    """正式 list 键损坏时仍沿用既有 legacy migration 语义。"""
    models = _models()
    settings = models.Settings.from_config(
        {"whitelist_sessions": None, "whitelist": ["legacy-session"]}
    )
    assert settings.whitelist == {"legacy-session"}


def test_legacy_alias_keys_stay_out_of_schema() -> None:
    """别名键不得重新进入 _conf_schema.json。"""
    legacy_aliases = {
        "cooldown_seconds",
        "idle_trigger_seconds",
        "min_context_messages",
        "proactive_threshold",
        "vision_enabled",
        "whitelist",
    }
    overlap = legacy_aliases & _schema_keys()
    assert not overlap, f"兼容别名误入 schema: {sorted(overlap)}"


def test_schema_defaults_match_python_defaults() -> None:
    """schema 的 default 必须等于 Python 侧空配置解析结果。

    只断言键集合相等时，默认值漂移无人守。漂移后果是面板
    显示值与实际生效值不一致，用户看到 A、跑的是 B，且不报错。
    """
    schema = _schema()
    models = _models()
    python_defaults = models.Settings.from_config({})

    def normalize(value: Any) -> Any:
        if isinstance(value, set | frozenset):
            return sorted(str(item) for item in value)
        if isinstance(value, list):
            return sorted(str(item) for item in value)
        return value

    drift: list[str] = []
    for key, spec in schema.items():
        if key in _INTENTIONAL_EMPTY_DEFAULT:
            continue
        attr = _FIELD_ALIAS.get(key, key)
        actual = normalize(getattr(python_defaults, attr))
        expected = normalize(spec.get("default"))
        if actual != expected:
            drift.append(f"{key}: schema={expected!r} python={actual!r}")
    assert not drift, "schema 与 Python 默认值漂移：\n" + "\n".join(drift)


def test_intentional_empty_defaults_stay_intentional() -> None:
    """空默认白名单里的键必须真的「空 → 回落内置默认」，否则该出白名单。"""
    schema = _schema()
    models = _models()
    for key in _INTENTIONAL_EMPTY_DEFAULT:
        assert schema[key].get("default") == "", f"{key} 已不是空默认，应移出白名单"
    settings = models.Settings.from_config({})
    assert settings.decision_prompt_template == models.DEFAULT_DECISION_PROMPT_TEMPLATE.strip()
    assert len(settings.decision_prompt_template) > 100, "空默认必须回落到内置模板而非空串"


def test_schema_slider_bounds_match_python_clamps() -> None:
    """schema slider 的 min/max 必须等于 Python 侧实际夹取边界（行为断言）。

    不比较声明而是灌越界值看夹取结果：面板允许的范围与代码接受的范围
    不一致时，用户能拖到一个会被静默改写的值。
    """
    schema = _schema()
    models = _models()
    drift: list[str] = []
    for key, spec in schema.items():
        slider = spec.get("slider")
        if not slider:
            continue
        attr = _FIELD_ALIAS.get(key, key)
        low, high = slider["min"], slider["max"]
        clamped_low = getattr(models.Settings.from_config({key: low - 10}), attr)
        clamped_high = getattr(models.Settings.from_config({key: high + 10}), attr)
        if clamped_low != low:
            drift.append(f"{key}: schema min={low} 但夹取到 {clamped_low}")
        if clamped_high != high:
            drift.append(f"{key}: schema max={high} 但夹取到 {clamped_high}")
    assert not drift, "schema slider 边界与 Python 夹取漂移：\n" + "\n".join(drift)


# ============================================================================
# CONFIG_SPECS 规格表必须能完整表达 _conf_schema.json 的机器可校验部分
# ============================================================================


def test_spec_table_covers_schema_keys_in_order() -> None:
    """规格表键集合与顺序必须等于 schema，顺序即面板呈现顺序。"""
    models = _models()
    spec_keys = [spec.key for spec in models.CONFIG_SPECS]
    schema_keys = list(_schema().keys())
    assert spec_keys == schema_keys, (
        f"规格表与 schema 漂移：表独有 {sorted(set(spec_keys) - set(schema_keys))}，"
        f"schema 独有 {sorted(set(schema_keys) - set(spec_keys))}"
    )


def test_spec_table_expresses_every_schema_field() -> None:
    """schema 的机器可校验字段必须能由规格表复现。

    覆盖 type / default / slider(min,max) / options。文案字段（description/hint）
    与 slider 的 step 是纯 UI 拷贝，不进表。
    """
    models = _models()
    schema = _schema()
    drift: list[str] = []

    for spec in models.CONFIG_SPECS:
        entry = schema[spec.key]

        want_type = "string" if spec.kind in {"enum", "str"} else spec.kind
        if entry["type"] != want_type:
            drift.append(f"{spec.key}: type schema={entry['type']} spec={want_type}")

        # 默认值：list 型比较排序后的字符串，避免顺序噪音
        want, got = entry.get("default"), spec.default
        if isinstance(want, list) or isinstance(got, list):
            if sorted(map(str, want or [])) != sorted(map(str, got or [])):
                drift.append(f"{spec.key}: default schema={want!r} spec={got!r}")
        elif want != got:
            drift.append(f"{spec.key}: default schema={want!r} spec={got!r}")

        slider = entry.get("slider")
        if slider:
            for name, spec_value in (
                ("min", spec.minimum),
                ("max", spec.maximum),
            ):
                if spec_value is None or float(slider[name]) != float(spec_value):
                    drift.append(
                        f"{spec.key}: slider {name} schema={slider[name]} spec={spec_value}"
                    )
        else:
            # 无 slider 的键不得在表里声明数值边界，否则表与面板对不上
            if spec.minimum is not None or spec.maximum is not None:
                drift.append(f"{spec.key}: 表声明了边界但 schema 无 slider")

        if tuple(entry.get("options", ())) != spec.options:
            drift.append(f"{spec.key}: options schema={entry.get('options')} spec={spec.options}")

    assert not drift, "规格表无法表达 schema：\n" + "\n".join(drift)


def test_every_schema_ui_field_is_known_to_the_spec_table() -> None:
    """schema 里出现的字段必须都是已知字段，且取值与规格表一致。

    只校验字段名会让 ``_special`` / ``editor_mode`` 这类纯 UI 声明与规格表脱钩：
    某键在 schema 声明了 select_provider 而规格表漏写，Web API 与前端控件就按
    两套规则走。
    """
    known = {
        "type",
        "description",
        "hint",
        "default",
        "slider",
        "options",
        "_special",
        "editor_mode",
        "editor_language",
    }
    models = _models()
    unknown: list[str] = []
    drift: list[str] = []
    for key, entry in _schema().items():
        for field in entry:
            if field not in known:
                unknown.append(f"{key}.{field}")
        spec = models.CONFIG_SPEC_BY_KEY[key]
        if entry.get("_special", "") != spec.special:
            drift.append(f"{key}: _special schema={entry.get('_special')!r} spec={spec.special!r}")
        if bool(entry.get("editor_mode", False)) != spec.editor_mode:
            drift.append(
                f"{key}: editor_mode schema={entry.get('editor_mode')!r} spec={spec.editor_mode!r}"
            )
        if str(entry.get("editor_language", "")) != spec.editor_language:
            drift.append(
                f"{key}: editor_language schema={entry.get('editor_language')!r} "
                f"spec={spec.editor_language!r}"
            )
    assert not unknown, f"schema 出现未知字段: {sorted(unknown)}"
    assert not drift, "schema 的 UI 字段与规格表漂移：\n" + "\n".join(drift)


@pytest.mark.parametrize(
    ("key", "expected_default"),
    [
        ("enabled_private_sessions", True),
        ("abandon_stale_on_new_message", False),
    ],
)
def test_bool_config_defaults_and_dual_panel(key: str, expected_default: bool) -> None:
    """bool 键的 Python 默认、规格表声明与 schema 面板声明必须一致。

    自定义页与 Dashboard 共用同一键；默认值漂移会让面板显示与实际生效相反。
    """
    models = _models()
    settings = models.Settings.from_config({})
    assert getattr(settings, key, None) is expected_default
    spec = models.CONFIG_SPEC_BY_KEY.get(key)
    assert spec is not None
    assert spec.kind == "bool"
    assert spec.default is expected_default
    assert spec.surfaces == frozenset({"host", "panel"})
    entry = _schema()[key]
    assert entry["type"] == "bool"
    assert entry["default"] is expected_default
