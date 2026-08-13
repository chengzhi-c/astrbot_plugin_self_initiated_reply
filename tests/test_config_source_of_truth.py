"""配置单源契约：CONFIG_SPECS ↔ schema；前端声明的可写键必须可读写。"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _config_specs_block() -> str:
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    start = models.find("CONFIG_SPECS")
    assert start >= 0
    end = models.find("\nDEFAULT_", start)
    if end < 0:
        end = models.find("\nclass ", start)
    return models[start:end]


def _config_spec_keys() -> set[str]:
    return set(re.findall(r'ConfigSpec\(\s*"([a-z0-9_]+)"', _config_specs_block()))


def _config_spec_defaults() -> dict[str, object]:
    """Parse CONFIG_SPECS positional defaults: ConfigSpec("key", "type", default, ...)."""
    out: dict[str, object] = {}
    for m in re.finditer(
        r'ConfigSpec\(\s*"([a-z0-9_]+)"\s*,\s*"[a-z]+"\s*,\s*([^,\n]+)',
        _config_specs_block(),
    ):
        key, raw = m.group(1), m.group(2).strip()
        try:
            out[key] = ast.literal_eval(raw)
        except Exception:
            continue
    return out


def _schema_keys() -> set[str]:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    return {k for k, v in schema.items() if isinstance(v, dict)}


def _expected_get_config_keys() -> set[str]:
    """GET /config 的期望键：panel 面 + 三个视图字段。真实形状由行为测试钉住。"""
    from .host_stubs import install_astrbot_stubs, load_package

    install_astrbot_stubs()
    models = load_package("selfreply_config_sot_package", "models")
    return {spec.key for spec in models.panel_config_specs()} | {
        "ok",
        "runtime_enabled",
        "decision_prompt_default",
    }


def _fe_default_block() -> str:
    text = (ROOT / "pages" / "主动回复设置" / "config-form.mjs").read_text(encoding="utf-8")
    m = re.search(r"export const DEFAULT_CONFIG = \{([\s\S]*?)\};", text)
    assert m, "DEFAULT_CONFIG not found"
    return m.group(1)


def _fe_default_keys() -> set[str]:
    return set(re.findall(r"([a-z0-9_]+)\s*:", _fe_default_block()))


def _fe_default_values() -> dict[str, object]:
    out: dict[str, object] = {}
    for km in re.finditer(r"([a-z0-9_]+)\s*:\s*([^,\n]+)", _fe_default_block()):
        key, raw = km.group(1), km.group(2).strip().rstrip(",")
        try:
            out[key] = ast.literal_eval(raw)
        except Exception:
            continue
    return out


def _fe_writable_keys() -> set[str]:
    text = (ROOT / "pages" / "主动回复设置" / "config-io.mjs").read_text(encoding="utf-8")
    match = re.search(r"export const CONFIG_SAVE_KEYS = Object\.freeze\(\[([\s\S]*?)\]\);", text)
    assert match, "CONFIG_SAVE_KEYS declaration not found"
    keys = set(re.findall(r'"([a-z0-9_]+)"', match.group(1)))
    assert keys, "CONFIG_SAVE_KEYS empty"
    return keys


def test_config_specs_match_schema_keys() -> None:
    specs = _config_spec_keys()
    schema = _schema_keys()
    assert specs, "CONFIG_SPECS empty"
    missing = specs - schema
    assert not missing, f"CONFIG_SPECS keys missing from schema: {sorted(missing)}"


def test_webapi_get_exposes_fe_writable_fields() -> None:
    keys = _expected_get_config_keys()
    writable = _fe_writable_keys()
    for required in ("ok", "enabled", "whitelist_sessions"):
        assert required in keys, f"GET config missing {required}"
    missing = writable - keys
    assert not missing, f"GET config missing FE-writable keys: {sorted(missing)}"
    assert "pipeline_mode" not in keys
    # FE 可写键必须也在 CONFIG_SPECS（配置键名，非 Settings 属性名）
    specs = _config_spec_keys()
    orphan = writable - specs
    assert not orphan, f"FE-writable keys not in CONFIG_SPECS: {sorted(orphan)}"


def test_fe_writable_keys_match_panel_surfaces() -> None:
    """前端可写键必须等于规格表里标了 panel 的键，两边不得各写一份。"""
    from .host_stubs import install_astrbot_stubs, load_package

    install_astrbot_stubs()
    models = load_package("selfreply_config_sot_package", "models")
    panel = {spec.key for spec in models.panel_config_specs()}
    writable = _fe_writable_keys()
    assert writable == panel, (
        f"FE CONFIG_SAVE_KEYS 与 panel 面漂移："
        f"FE 独有 {sorted(writable - panel)}，panel 独有 {sorted(panel - writable)}"
    )
    assert "patrol_inactive_after_sec" not in writable


def test_fe_default_config_keys_subset_of_specs() -> None:
    defaults = _fe_default_keys()
    specs = _config_spec_keys()
    assert defaults, "FE DEFAULT_CONFIG empty"
    orphan = defaults - specs
    assert not orphan, f"FE DEFAULT_CONFIG keys not in CONFIG_SPECS: {sorted(orphan)}"


def test_fe_default_config_values_match_specs() -> None:
    """FE DEFAULT_CONFIG values must match CONFIG_SPECS defaults (numeric equality)."""
    fe = _fe_default_values()
    specs = _config_spec_defaults()
    assert fe, "FE DEFAULT_CONFIG values empty"
    for key, fe_val in fe.items():
        assert key in specs, f"FE key {key} missing from CONFIG_SPECS defaults"
        spec_val = specs[key]
        if isinstance(fe_val, (int, float)) and isinstance(spec_val, (int, float)):
            assert float(fe_val) == float(spec_val), (
                f"DEFAULT_CONFIG[{key}]={fe_val!r} != CONFIG_SPECS default {spec_val!r}"
            )
        else:
            assert fe_val == spec_val, (
                f"DEFAULT_CONFIG[{key}]={fe_val!r} != CONFIG_SPECS default {spec_val!r}"
            )


def _fe_whitelist_illegal_pattern() -> str:
    text = (ROOT / "pages" / "主动回复设置" / "config-io.mjs").read_text(encoding="utf-8")
    match = re.search(r"export const WHITELIST_ILLEGAL_RE = /(.+)/;", text)
    assert match, "WHITELIST_ILLEGAL_RE not found"
    return match.group(1)


def test_frontend_whitelist_illegal_chars_match_backend() -> None:
    """前端白名单非法字符集必须与 STRING_LIST_ILLEGAL_RE 判定同一批字符。"""
    from .host_stubs import install_astrbot_stubs, load_package

    install_astrbot_stubs()
    models = load_package("selfreply_config_sot_package", "models")
    backend = models.STRING_LIST_ILLEGAL_RE
    frontend = re.compile(_fe_whitelist_illegal_pattern())
    probes = [chr(code) for code in range(32)] + ['"', "'", "\\", "ok", "qq:GroupMessage:1"]
    drifted = [
        probe for probe in probes if bool(backend.search(probe)) != bool(frontend.search(probe))
    ]
    assert not drifted, f"whitelist illegal charset drifted on {drifted!r}"
    assert backend.search("has\ttab")
    assert backend.search('has"quote')
    assert frontend.search("has\ttab")
    assert frontend.search("has'quote")


def test_frontend_number_bounds_match_panel_specs() -> None:
    """自定义页 number 控件的 min/max 必须等于规格表边界。"""
    from .host_stubs import install_astrbot_stubs, load_package

    install_astrbot_stubs()
    models = load_package("selfreply_config_sot_package", "models")
    html = (ROOT / "pages" / "主动回复设置" / "index.html").read_text(encoding="utf-8")
    io = (ROOT / "pages" / "主动回复设置" / "config-io.mjs").read_text(encoding="utf-8")
    key_by_control = {
        control: key
        for key, control in re.findall(
            r"([a-z0-9_]+):\s*num\(\s*e\.(\w+)\.value",
            io,
        )
    }
    assert key_by_control, "未从 config-io.mjs 解析到 number 控件绑定"

    seen: set[str] = set()
    drift: list[str] = []
    for match in re.finditer(
        r'<input type="number" id="(\w+)" min="([^\"]+)" max="([^\"]+)"',
        html,
    ):
        control_id, raw_min, raw_max = match.groups()
        key = key_by_control.get(control_id)
        assert key is not None, f"HTML number #{control_id} 没有对应的保存键"
        spec = models.CONFIG_SPEC_BY_KEY[key]
        seen.add(key)
        if float(raw_min) != float(spec.minimum):
            drift.append(f"{key}: HTML min={raw_min} spec={spec.minimum}")
        if float(raw_max) != float(spec.maximum):
            drift.append(f"{key}: HTML max={raw_max} spec={spec.maximum}")
    missing = [
        spec.key
        for spec in models.panel_config_specs()
        if spec.kind in {"int", "float"} and spec.key not in seen
    ]
    assert not missing, f"panel 数值键没有 HTML number 控件：{missing}"
    assert not drift, "HTML number 边界与规格表漂移：\n" + "\n".join(drift)
