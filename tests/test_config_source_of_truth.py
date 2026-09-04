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


def _decision_prompt_value_keys() -> set[str]:
    """AST 取 build_decision_prompt 里 values 字典的键（后端可注入的模板变量）。"""
    tree = ast.parse((ROOT / "decision.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_decision_prompt":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "values" for t in sub.targets
                ):
                    assert isinstance(sub.value, ast.Dict)
                    return {k.value for k in sub.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("build_decision_prompt values dict not found")


def test_prompt_preview_keys_are_injectable_variables() -> None:
    """前端预览示例的每个变量名都必须是后端真会注入的键。

    后端把 {latest_message} 改名而前端预览仍高亮旧名，用户看到的预览就与实际
    发给模型的内容不符——这条把该漂移变红灯。方向是子集：后端新增键而前端暂不
    预览只是不高亮（无害），前端多出后端没有的键才是误导。
    """
    form = (ROOT / "pages" / "主动回复设置" / "config-form.mjs").read_text(encoding="utf-8")
    block = form.split("PROMPT_PREVIEW_VALUES = {", 1)[1].split("\n};", 1)[0]
    preview_keys = set(re.findall(r"^\s*([a-z_]+):", block, re.M))
    assert preview_keys, "PROMPT_PREVIEW_VALUES 解析为空"
    injectable = _decision_prompt_value_keys()
    extra = preview_keys - injectable
    assert not extra, f"前端预览了后端不注入的变量：{sorted(extra)}"


def test_frontend_bool_defaults_match_config_specs() -> None:
    """data-config-default 属性值必须等于对应 ConfigSpec.default。

    number 边界已有 test_frontend_number_bounds_match_panel_specs 守，bool 默认值
    此前无人守：前端写 data-config-default="true" 而后端默认 False 时，未加载态的
    开关显示与实际保存值相反。
    """
    from .host_stubs import install_astrbot_stubs, load_package

    install_astrbot_stubs()
    models = load_package("selfreply_config_sot_bool_defaults", "models")
    html = (ROOT / "pages" / "主动回复设置" / "index.html").read_text(encoding="utf-8")
    drift: list[str] = []
    for tag in re.findall(r"<input\b[^>]*>", html):
        key_match = re.search(r'data-config-key="([a-z0-9_]+)"', tag)
        default_match = re.search(r'data-config-default="([^"]+)"', tag)
        if not key_match or not default_match:
            continue
        spec = models.CONFIG_SPEC_BY_KEY[key_match.group(1)]
        expected = "true" if spec.default is True else "false"
        if default_match.group(1) != expected:
            drift.append(
                f"{key_match.group(1)}: HTML={default_match.group(1)} spec={spec.default!r}"
            )
    assert not drift, "data-config-default 与规格表默认值漂移：\n" + "\n".join(drift)


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
