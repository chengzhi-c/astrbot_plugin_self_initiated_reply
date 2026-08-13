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


def _webapi_get_keys() -> set[str]:
    web = (ROOT / "webapi.py").read_text(encoding="utf-8")
    m = re.search(
        r"async def _api_get_config[\s\S]*?return \{\n(?P<body>[\s\S]*?)\n\s*\}",
        web,
    )
    assert m, "_api_get_config return block not found"
    return set(re.findall(r'"([a-z0-9_]+)"\s*:', m.group("body")))


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
    keys = _webapi_get_keys()
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
