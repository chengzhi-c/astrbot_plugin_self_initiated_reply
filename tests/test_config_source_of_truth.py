"""配置单源契约：CONFIG_SPECS ↔ schema；前端声明的可写键必须可读写。"""

from __future__ import annotations

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


def _fe_writable_keys() -> set[str]:
    html = (ROOT / "pages" / "主动回复设置" / "index.html").read_text(encoding="utf-8")
    keys = set(re.findall(r'data-config-key="([a-z0-9_]+)"', html))
    assert keys, "index.html has no data-config-key declarations"
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
        f"FE data-config-key 与 panel 面漂移："
        f"FE 独有 {sorted(writable - panel)}，panel 独有 {sorted(panel - writable)}"
    )
    assert "patrol_inactive_after_sec" not in writable


def test_frontend_has_no_handwritten_default_config() -> None:
    form = (ROOT / "pages" / "主动回复设置" / "config-form.mjs").read_text(encoding="utf-8")
    io = (ROOT / "pages" / "主动回复设置" / "config-io.mjs").read_text(encoding="utf-8")
    assert "DEFAULT_CONFIG" not in form
    assert "DEFAULT_CONFIG" not in io


def _fe_whitelist_illegal_pattern() -> str:
    text = (ROOT / "pages" / "主动回复设置" / "config-form.mjs").read_text(encoding="utf-8")
    match = re.search(r"export const WHITELIST_ILLEGAL_RE = /(.+)/;", text)
    assert match, "WHITELIST_ILLEGAL_RE not found"
    io_text = (ROOT / "pages" / "主动回复设置" / "config-io.mjs").read_text(encoding="utf-8")
    assert "WHITELIST_ILLEGAL_RE = /" not in io_text, (
        "config-io.mjs must re-export WHITELIST_ILLEGAL_RE, not redefine it"
    )
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
    """自定义页 number 控件的 min/max/step 必须等于规格表。"""
    from .host_stubs import install_astrbot_stubs, load_package

    install_astrbot_stubs()
    models = load_package("selfreply_config_sot_package", "models")
    html = (ROOT / "pages" / "主动回复设置" / "index.html").read_text(encoding="utf-8")
    seen: set[str] = set()
    drift: list[str] = []
    for tag in re.findall(r"<input\b[^>]*>", html):
        if 'type="number"' not in tag:
            continue
        control = re.search(r'id="(\w+)"', tag)
        key_match = re.search(r'data-config-key="([a-z0-9_]+)"', tag)
        raw_min = re.search(r'min="([^\"]+)"', tag)
        raw_max = re.search(r'max="([^\"]+)"', tag)
        raw_step = re.search(r'step="([^\"]+)"', tag)
        assert control is not None, f"number control without id: {tag}"
        assert key_match is not None, f"HTML number #{control.group(1)} has no data-config-key"
        assert raw_min is not None and raw_max is not None, (
            f"HTML number #{control.group(1)} lacks bounds"
        )
        key = key_match.group(1)
        spec = models.CONFIG_SPEC_BY_KEY[key]
        seen.add(key)
        if float(raw_min.group(1)) != float(spec.minimum):
            drift.append(f"{key}: HTML min={raw_min.group(1)} spec={spec.minimum}")
        if float(raw_max.group(1)) != float(spec.maximum):
            drift.append(f"{key}: HTML max={raw_max.group(1)} spec={spec.maximum}")
        html_step = None if raw_step is None else float(raw_step.group(1))
        spec_step = None if spec.step is None else float(spec.step)
        if html_step != spec_step:
            drift.append(f"{key}: HTML step={html_step} spec={spec_step}")
    missing = [
        spec.key
        for spec in models.panel_config_specs()
        if spec.kind in {"int", "float"} and spec.key not in seen
    ]
    assert not missing, f"panel 数值键没有 HTML number 控件：{missing}"
    assert not drift, "HTML number 边界与规格表漂移：\n" + "\n".join(drift)
