"""配置 schema（_conf_schema.json）与 CONFIG_SCHEMA_KEYS 一致性守卫（0.8.8）。

schema 驱动 AstrBot 设置面板渲染；CONFIG_SCHEMA_KEYS 决定 webapi 接受哪些
配置键（名单之外一律 fail loud）。二者漂移的两种后果：
- schema 有而 KEYS 无：面板字段提交被拒（400 未知键）；
- KEYS 有而 schema 无：字段不在面板上，形同死配置。
0.8.8 起硬性断言：schema 键 == 正式键（KEYS 去掉兼容别名），改任何一侧
都必须同步另一侧，否则变红。
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


def _schema_keys() -> set[str]:
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    return set(schema.keys())


def test_schema_keys_align_with_config_schema_keys() -> None:
    """schema 键集合 == CONFIG_SCHEMA_KEYS - 兼容别名（别名不进面板 schema）。"""
    schema_keys = _schema_keys()
    webapi = _webapi()
    canonical = set(webapi._SCHEMA_FORMAL_KEYS)
    assert schema_keys == canonical, (
        f"schema 与 CONFIG_SCHEMA_KEYS 漂移："
        f"schema 独有 {sorted(schema_keys - canonical)}，"
        f"KEYS 独有 {sorted(canonical - schema_keys)}"
    )


def test_alias_keys_stay_out_of_schema() -> None:
    """兼容别名不得进入 _conf_schema.json（旧兼容键不渲染到新面板）。"""
    webapi = _webapi()
    overlap = set(webapi._SCHEMA_ALIAS_KEYS) & _schema_keys()
    assert not overlap, f"兼容别名误入 schema: {sorted(overlap)}"


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
