"""README 配置表与 ``CONFIG_SPECS`` 的守卫。

README 是配置的第四份副本（另外三份是 ``_conf_schema.json`` / ``CONFIG_SPECS`` /
设置页表单），前三份有两两闭合的测试，README 此前一份都没有：改默认值只要三方
同步就不会被发现。这里钉住配置段的键总数、表格键集合与默认值。
"""

from __future__ import annotations

import functools
import json
import pathlib
import re

from .host_stubs import load_modules

REPO = pathlib.Path(__file__).resolve().parents[1]

TABLE_KEYS = [
    "decision_model_enabled",
    "judge_provider_id",
    "generation_timeout_sec",
    "decision_temperature",
    "decision_prompt_template",
    "decision_timeout_sec",
    "decision_history_min_messages",
    "min_silence_sec",
    "message_delay_sec",
    "cooldown_sec",
    "abandon_stale_on_new_message",
    "skip_after_direct_call",
    "quote_mode",
    "quote_probability",
    "mention_mode",
    "mention_probability",
]


@functools.cache
def _facts():
    (models,) = load_modules("selfreply_readme_config", "models")
    schema = json.loads((REPO / "_conf_schema.json").read_text(encoding="utf-8"))
    specs = {spec.key: spec for spec in models.CONFIG_SPECS}
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    section = readme.split("### 常用配置项", 1)[1].split("\n---\n", 1)[0]
    return schema, specs, readme, section


def _key_names(text: str) -> list[str]:
    """配置段里出现的反引号小写名（数字范围与枚举取值不算）。"""
    literals = {"random", "model", "always", "off", "balanced"}
    return [token for token in re.findall(r"`([a-z][a-z0-9_]*)`", text) if token not in literals]


def _table_rows(section: str) -> list[tuple[str, str]]:
    """(键名, 整行) 列表，只取配置表的正文数据行。"""
    rows = []
    for line in section.splitlines():
        if not line.startswith("| ") or set(line) <= set("|- "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "配置项":
            continue
        names = _key_names(cells[0])
        if names:
            rows.append((names[0], line))
    return rows


def test_schema_and_specs_agree_on_every_key() -> None:
    """前置事实：schema 与 SPECS 的键集合与顺序一致，配置段写的总数取自这里。"""
    schema, specs, _readme, section = _facts()
    assert list(schema) == list(specs), "schema 与 CONFIG_SPECS 的键顺序不一致"
    assert f"{len(specs)} 个键" in section, (
        f"配置段没有写明键总数 {len(specs)}：{section.splitlines()[0]}"
    )


def test_table_keys_and_order_are_stable() -> None:
    _schema, _specs, _readme, section = _facts()
    keys = [key for key, _line in _table_rows(section)]
    assert keys == TABLE_KEYS, f"配置表键集合或顺序变了：{keys}"


def test_config_section_cites_only_real_keys() -> None:
    _schema, specs, _readme, section = _facts()
    unknown = sorted(set(_key_names(section)) - set(specs))
    assert not unknown, f"配置段出现不存在的键名：{unknown}"


def test_table_defaults_match_specs() -> None:
    """表格里的默认值必须与 SPECS 逐条一致——README 漂移最常见的形态。"""
    _schema, specs, _readme, section = _facts()
    rows = _table_rows(section)
    assert rows, "配置表里没有一个键名"
    by_key = dict(rows)
    for key, line in rows:
        spec = specs[key]
        if spec.kind in ("int", "float"):
            found = re.search(r"默认\s*`([0-9.]+)`", line)
            assert found, f"{key} 行缺「默认 `值`」：{line}"
            assert float(found.group(1)) == float(spec.default), f"{key} 的默认值与 SPECS 不符"
        elif spec.kind == "bool":
            marker = "开启" if spec.default else "默认关"
            assert marker in line, f"{key} 的默认取值表述与实际 {spec.default} 不符：{line}"
        elif spec.kind == "enum":
            assert "默认" in line, f"{key} 的默认选项表述缺失：{line}"
    for key, marker in {
        "judge_provider_id": "留空",
        "decision_prompt_template": "默认模板",
    }.items():
        assert marker in by_key[key], f"{key} 行应写明「{marker}」"


def test_security_relevant_keys_carry_their_semantics() -> None:
    """两个有安全含义的键必须点名并写出后果，不能只留在表单里。"""
    _schema, _specs, _readme, section = _facts()
    private_rows = "\n".join(
        line for line in section.splitlines() if "enabled_private_sessions" in line
    )
    assert private_rows, "私聊主动回复开关没有点名"
    assert "默认开启" in private_rows and "群聊" in private_rows, (
        f"私聊开关的默认值与作用范围未写明：{private_rows}"
    )
    assert "`proactive_inherit_tools`" in section, "工具继承开关没有点名"
    tool_rows = "\n".join(
        line for line in section.splitlines() if "proactive_inherit_tools" in line
    )
    assert "默认关" in tool_rows, "工具继承打开后的权限升级未标注默认关闭"
    assert "危险" in tool_rows or "Shell" in tool_rows, "工具继承的风险边界未写明"
