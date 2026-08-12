"""配置单源契约：CONFIG_SPECS ↔ schema；FE DEFAULT_CONFIG ⊂ specs；GET 含写回关键键。"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 设置页可写回、且 GET /config 应暴露的字段（与 app.js saveConfig 对齐）
_FE_WRITABLE = {
    "enabled",
    "decision_model_enabled",
    "judge_provider_id",
    "decision_prompt_template",
    "decision_temperature",
    "decision_timeout_sec",
    "decision_history_min_messages",
    "message_delay_sec",
    "min_silence_sec",
    "cooldown_sec",
    "vision_judge_enabled",
    "vision_main_enabled",
    "vision_skip_stickers",
    "vision_provider_id",
    "vision_judge_provider_id",
    "vision_max_images",
    "vision_image_age_sec",
    "vision_timeout_sec",
    "proactive_inherit_tools",
    "whitelist_sessions",
}


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


def test_config_specs_match_schema_keys() -> None:
    specs = _config_spec_keys()
    schema = _schema_keys()
    assert specs, "CONFIG_SPECS empty"
    missing = specs - schema
    assert not missing, f"CONFIG_SPECS keys missing from schema: {sorted(missing)}"


def test_webapi_get_exposes_fe_writable_fields() -> None:
    keys = _webapi_get_keys()
    for required in ("ok", "enabled", "whitelist_sessions"):
        assert required in keys, f"GET config missing {required}"
    missing = _FE_WRITABLE - keys
    assert not missing, f"GET config missing FE-writable keys: {sorted(missing)}"
    # FE 可写键必须也在 CONFIG_SPECS（配置键名，非 Settings 属性名）
    specs = _config_spec_keys()
    orphan = _FE_WRITABLE - specs
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
