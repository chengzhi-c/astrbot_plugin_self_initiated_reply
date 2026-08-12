"""配置单源契约：CONFIG_SPECS ↔ schema；FE DEFAULT_CONFIG ⊂ specs；GET 含写回关键键。"""

from __future__ import annotations

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


def _config_spec_keys() -> set[str]:
    models = (ROOT / "models.py").read_text(encoding="utf-8")
    start = models.find("CONFIG_SPECS")
    assert start >= 0
    end = models.find("\nDEFAULT_", start)
    if end < 0:
        end = models.find("\nclass ", start)
    block = models[start:end]
    return set(re.findall(r'ConfigSpec\(\s*"([a-z0-9_]+)"', block))


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


def _fe_default_keys() -> set[str]:
    text = (ROOT / "pages" / "主动回复设置" / "config-form.mjs").read_text(
        encoding="utf-8"
    )
    m = re.search(r"export const DEFAULT_CONFIG = \{([\s\S]*?)\};", text)
    assert m, "DEFAULT_CONFIG not found"
    return set(re.findall(r"([a-z0-9_]+)\s*:", m.group(1)))


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
