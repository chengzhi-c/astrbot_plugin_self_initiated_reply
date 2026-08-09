"""配置 schema（_conf_schema.json）与 CONFIG_SCHEMA_KEYS 一致性守卫（0.8.8）。

schema 驱动 AstrBot 设置面板渲染；CONFIG_SCHEMA_KEYS 决定 webapi 接受哪些
配置键（名单之外一律 fail loud）。二者漂移的两种后果：
- schema 有而 KEYS 无：面板字段提交被拒（400 未知键）；
- KEYS 有而 schema 无：字段不在面板上，形同死配置。
0.8.8 起硬性断言：schema 键 == CONFIG_SCHEMA_KEYS；0.9.2 起兼容别名层已
移除，二者一一对应，改任何一侧都必须同步另一侧，否则变红。
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


def test_schema_keys_align_with_config_schema_keys() -> None:
    """schema 键集合 == CONFIG_SCHEMA_KEYS（0.9.2 起无别名，一一对应）。"""
    schema_keys = _schema_keys()
    webapi = _webapi()
    canonical = set(webapi.CONFIG_SCHEMA_KEYS)
    assert schema_keys == canonical, (
        f"schema 与 CONFIG_SCHEMA_KEYS 漂移："
        f"schema 独有 {sorted(schema_keys - canonical)}，"
        f"KEYS 独有 {sorted(canonical - schema_keys)}"
    )


def test_legacy_alias_keys_stay_out_of_schema() -> None:
    """历史兼容别名已于 0.9.2 移除，不得重新进入 _conf_schema.json。"""
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

    0.9.3 补强：此前只断言键集合相等，默认值漂移无人守。漂移后果是面板
    显示值与实际生效值不一致——用户看到 A、跑的是 B，且不报错。
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


def test_schema_options_match_python_choices() -> None:
    """枚举型键的 schema options 必须等于 Python/webapi 侧接受的取值集合。"""
    schema = _schema()
    webapi = _webapi()
    assert set(schema["reply_length_mode"]["options"]) == set(webapi._REPLY_LENGTH_MODES)


# ============================================================================
# 阶段 2：CONFIG_SPECS 规格表必须能完整表达 _conf_schema.json
#
# 这是替换四个消费者（Settings 字段/from_config/to_config_dict/
# CONFIG_SCHEMA_KEYS/_parse_config_updates）的前提证明：若表表达不了现有
# schema，重构就会退化成「表 + 一堆例外」，那不如不做（0.9.2 Phase E 的
# 教训）。故此处逐字段双向比对，不留「大致一致」的余地。
# ============================================================================


def test_spec_table_covers_schema_keys_in_order() -> None:
    """规格表键集合与顺序必须等于 schema——顺序即面板呈现顺序。"""
    models = _models()
    spec_keys = [spec.key for spec in models.CONFIG_SPECS]
    schema_keys = list(_schema().keys())
    assert spec_keys == schema_keys, (
        f"规格表与 schema 漂移：表独有 {sorted(set(spec_keys) - set(schema_keys))}，"
        f"schema 独有 {sorted(set(schema_keys) - set(spec_keys))}"
    )


def test_spec_table_expresses_every_schema_field() -> None:
    """schema 的每个机器可校验字段都必须能由规格表复现。

    覆盖 type / default / slider(min,max,step) / options / _special /
    editor_mode / editor_language。文案字段（description/hint）有意不进表，
    因此不比对——它们是纯 UI 拷贝，进表只会变成 schema 的第二份副本。
    """
    models = _models()
    schema = _schema()
    drift: list[str] = []

    for spec in models.CONFIG_SPECS:
        entry = schema[spec.key]

        if entry["type"] != spec.schema_type:
            drift.append(f"{spec.key}: type schema={entry['type']} spec={spec.schema_type}")

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
                ("step", spec.step),
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
        if str(entry.get("_special", "")) != spec.special:
            drift.append(f"{spec.key}: _special schema={entry.get('_special')} spec={spec.special}")
        if bool(entry.get("editor_mode", False)) != spec.editor_mode:
            drift.append(f"{spec.key}: editor_mode 漂移")
        if str(entry.get("editor_language", "")) != spec.editor_language:
            drift.append(f"{spec.key}: editor_language 漂移")

    assert not drift, "规格表无法表达 schema：\n" + "\n".join(drift)


def test_every_schema_ui_field_is_known_to_the_spec_table() -> None:
    """schema 里不得出现规格表不认识的字段——否则表驱动会静默丢掉它。

    这条是防「未来给某个键加了新 UI 属性，表没跟上」：新属性要么进表，
    要么显式记入已知文案字段白名单，不允许无声漂移。
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
    unknown: list[str] = []
    for key, entry in _schema().items():
        for field in entry:
            if field not in known:
                unknown.append(f"{key}.{field}")
    assert not unknown, f"schema 出现规格表未覆盖的字段: {sorted(unknown)}"


def test_spec_table_reproduces_python_defaults() -> None:
    """规格表逐键驱动的解析结果必须等于 Settings.from_config({})。

    覆盖范围（如实说明，勿高估）：from_config 已表驱动化，两侧共用
    ``coerce_config_value``，因此这条**不覆盖 coerce 本身**的正确性——那由
    ``test_schema_defaults_match_python_defaults``（对照 _conf_schema.json）
    兜住。本条能抓的是 ``attr``/``container`` 写错，即「表里声明的字段名或
    容器类型与 Settings 实际不符」，那会让 from_config 构造出错或类型走形。
    """
    models = _models()
    settings = models.Settings.from_config({})
    drift: list[str] = []
    for spec in models.CONFIG_SPECS:
        via_spec = models.read_config_value(spec, {})
        actual = getattr(settings, spec.attr)
        if via_spec != actual:
            drift.append(f"{spec.key}: 表驱动={via_spec!r} from_config={actual!r}")
    assert not drift, "表驱动解析与 from_config 结果不一致：\n" + "\n".join(drift)


# 注：曾有一条 test_spec_table_clamps_match_from_config_clamps（比较「表驱动夹取」
# 与「from_config 夹取」）。from_config 表驱动化之后两侧走的是同一条
# coerce_config_value，该断言退化为同义反复——实测把 int 分支的夹取整段删掉，
# 它仍然绿，只有下方对照 _conf_schema.json 的
# test_schema_slider_bounds_match_python_clamps 变红。故删除而非保留：
# 夹取行为的真锚点是 schema 声明，不是另一条同源调用。


def test_spec_table_legacy_fallback_matches_from_config() -> None:
    """旧键回退语义必须与 from_config 一致（存量配置迁移不能回归）。

    同上：两侧共用 ``read_config_value``，本条不覆盖回退算法本身，只钉住
    ``legacy_keys`` 声明与 ``attr``/``container`` 的一致性。回退算法的真实
    锚点是 ``test_config_hot_reload.py::test_from_config_migrates_legacy_alias_keys``
    与 ``test_vision.py::test_legacy_vision_enabled_migrates_to_both_toggles``
    ——它们用存量配置的真实键名断言迁移结果。
    """
    models = _models()
    drift: list[str] = []
    for spec in models.CONFIG_SPECS:
        for legacy in spec.legacy_keys:
            probe = {
                "bool": True,
                "int": 7,
                "float": 7.0,
                "list": ["qq:GroupMessage:7"],
                "str": "probe",
                "enum": "short",
                "text": "probe-template",
            }[spec.kind]
            via_spec = models.read_config_value(spec, {legacy: probe})
            actual = getattr(models.Settings.from_config({legacy: probe}), spec.attr)
            if via_spec != actual:
                drift.append(f"{spec.key} via {legacy}: 表驱动={via_spec!r} from_config={actual!r}")
    assert not drift, "旧键回退语义漂移：\n" + "\n".join(drift)


def test_audited_keys_come_from_spec_table() -> None:
    """审计名单必须由规格表派生，不得再手工维护第二份。"""
    models = _models()
    webapi = _webapi()
    from_table = {spec.key for spec in models.CONFIG_SPECS if spec.audited}
    assert from_table == set(webapi._AUDITED_CONFIG_KEYS), (
        f"审计名单与规格表漂移：表={sorted(from_table)} "
        f"webapi={sorted(webapi._AUDITED_CONFIG_KEYS)}"
    )


def test_wheel_required_files_covered_by_pyproject() -> None:
    """check_wheel 的 REQUIRED_FILES 每一项都必须能被 pyproject 打包覆盖。

    漂移后果：check_wheel 在 CI 红但本地构建永远绿（要求了打包层根本
    不会包含的文件），守卫失效。断言 artifacts 前缀 ∪ packages 目录。
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        import tomli as tomllib  # type: ignore[no-redef]

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tool = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    artifacts = [str(a).strip() for a in tool.get("artifacts", [])]
    packages = [str(p).strip() for p in tool.get("packages", [])]

    import runpy

    check_wheel = runpy.run_path(str(ROOT / "scripts" / "check_wheel.py"))
    for required in check_wheel["REQUIRED_FILES"]:
        covered = any(
            pkg == "." or required.startswith(pkg.rstrip("/") + "/") for pkg in packages
        ) or any(required.startswith(a.rstrip("*/")) for a in artifacts)
        assert covered, f"REQUIRED_FILES 的 {required} 未被 pyproject 打包覆盖"
