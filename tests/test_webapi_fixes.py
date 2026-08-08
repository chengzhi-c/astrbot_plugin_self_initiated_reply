"""webapi 边界测试补盲（0.8.1 阶段 C）。

覆盖 webapi.py 的拒绝路径与分支：strict 校验、provider 收集链、
cleanup/status/theme API、回滚 dropped 分支与 _request_json 回退。
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from .host_stubs import with_plugin

UMO = "fake:group:123"
PACKAGE = "selfreply_main_test_package"


def _webapi() -> Any:
    return sys.modules[f"{PACKAGE}.webapi"]


@pytest.fixture(autouse=True)
def _bootstrap():
    from .host_stubs import load_main

    load_main()
    yield
    web = sys.modules.get("astrbot.api.web")
    if web is not None:
        web.request.payload = {}


# ============================================================================
# _parse_config_updates 拒绝路径
# ============================================================================


@pytest.mark.parametrize(
    "payload, field",
    [
        ({"enabled": 1}, "enabled"),
        ({"decision_model_enabled": "yes"}, "decision_model_enabled"),
        ({"decision_temperature": "hot"}, "decision_temperature"),
        ({"decision_temperature": True}, "decision_temperature"),
        ({"decision_temperature": float("nan")}, "decision_temperature"),
        ({"decision_temperature": float("inf")}, "decision_temperature"),
        ({"decision_timeout_sec": "x"}, "decision_timeout_sec"),
        ({"cooldown_sec": True}, "cooldown_sec"),
        ({"cooldown_sec": "5s"}, "cooldown_sec"),
        ({"message_delay_sec": "5s"}, "message_delay_sec"),
        ({"min_silence_sec": "x"}, "min_silence_sec"),
        ({"patrol_inactive_after_sec": "x"}, "patrol_inactive_after_sec"),
        ({"decision_history_min_messages": "x"}, "decision_history_min_messages"),
        ({"proactive_inherit_tools": 1}, "proactive_inherit_tools"),
        ({"vision_judge_enabled": "1"}, "vision_judge_enabled"),
        ({"vision_main_enabled": "1"}, "vision_main_enabled"),
        ({"vision_skip_stickers": "1"}, "vision_skip_stickers"),
        ({"vision_max_images": "x"}, "vision_max_images"),
        ({"vision_image_age_sec": "x"}, "vision_image_age_sec"),
        ({"vision_timeout_sec": "x"}, "vision_timeout_sec"),
        ("not a dict", "请求体必须是 JSON 对象"),
        ({"whitelist_sessions": "abc"}, "whitelist_sessions 必须是数组"),
    ],
)
def test_parse_config_updates_rejects_invalid(payload: Any, field: str) -> None:
    with pytest.raises(ValueError):
        _webapi()._parse_config_updates(payload)


def test_parse_config_updates_whitelist_rules() -> None:
    webapi = _webapi()
    # 超长拒绝
    with pytest.raises(ValueError):
        webapi._parse_config_updates({"whitelist_sessions": ["ok", "x" * 201]})
    # 非法字符拒绝
    with pytest.raises(ValueError):
        webapi._parse_config_updates({"whitelist_sessions": ['bad"quote']})
    with pytest.raises(ValueError):
        webapi._parse_config_updates({"whitelist_sessions": ["bad\\slash"]})
    # 200 边界接受
    ok = webapi._parse_config_updates({"whitelist_sessions": ["x" * 200]})
    assert ok["whitelist_sessions"] == ["x" * 200]
    # 空条目跳过、去空白
    updates = webapi._parse_config_updates({"whitelist_sessions": [" a ", "", "b"]})
    assert updates["whitelist_sessions"] == ["a", "b"]


def test_parse_config_updates_rejects_legacy_aliases() -> None:
    """0.9.2 兼容别名层移除：别名键 fail loud 拒绝（旧前端得显式错误而非静默吞掉）。"""
    webapi = _webapi()
    for alias in (
        "cooldown_seconds",
        "idle_trigger_seconds",
        "min_context_messages",
        "proactive_threshold",
        "vision_enabled",
        "whitelist",
    ):
        with pytest.raises(ValueError, match="未知配置键"):
            webapi._parse_config_updates({alias: 1})


def test_parse_config_updates_formal_defaults() -> None:
    webapi = _webapi()
    updates = webapi._parse_config_updates(
        {
            "cooldown_sec": 30,
            "message_delay_sec": 60,
            "decision_history_min_messages": 3,
            "decision_prompt_template": "   ",
            "judge_provider_id": 0,
            "vision_provider_id": 42,
            "vision_judge_provider_id": 0,
        }
    )
    assert updates["cooldown_sec"] == 30
    assert updates["message_delay_sec"] == 60
    assert updates["decision_history_min_messages"] == 3
    assert updates["decision_prompt_template"] == webapi.DEFAULT_DECISION_PROMPT_TEMPLATE
    assert updates["judge_provider_id"] == ""
    assert updates["vision_provider_id"] == "42"
    # 0 是 falsy：与 judge_provider_id 一致的规范化语义
    assert updates["vision_judge_provider_id"] == ""


# ============================================================================
# provider 收集链
# ============================================================================


def test_provider_config_helpers_dict_variants() -> None:
    webapi = _webapi()
    assert webapi._provider_config({"provider_config": {"id": "a"}}) == {"id": "a"}
    assert webapi._provider_config({"config": {"id": "b"}}) == {"id": "b"}
    assert webapi._provider_config({"id": "c"}) == {"id": "c"}
    assert webapi._provider_id({"provider_id": "pid"}, "fb") == "pid"
    assert webapi._provider_id({}, "") == ""
    assert webapi._provider_id({"id": "  "}, "fb") == ""
    # 非 dict 配置（如普通对象属性值）走 getattr 分支
    assert webapi._config_value("cfg", "id", "dft") == "dft"
    # label 等于 provider_id 时不重复拼接
    assert webapi._provider_label({"model": "gpt"}, "gpt") == "gpt"
    assert webapi._provider_label({}, "only-id") == "only-id"
    # 空 id 的 provider 不产出 option
    assert webapi._provider_option({}, "") is None


def test_collect_provider_options_from_context(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        plugin.context.get_all_providers = lambda: {
            "p1": SimpleNamespace(provider_config={"id": "pid1", "display_name": "模型A"}),
            "p2": SimpleNamespace(provider_config={"id": "pid2", "model": "gpt"}),
        }
        options = webapi._collect_provider_options(plugin)
        # 按 label 排序（"gpt (pid2)" 排在 "模型A (pid1)" 前）
        assert options == [
            {"id": "pid2", "label": "gpt (pid2)"},
            {"id": "pid1", "label": "模型A (pid1)"},
        ]

    with_plugin(tmp_path, scenario)


def test_collect_provider_options_dedup_and_manager_fallback(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        # 同 id 去重
        plugin.context.get_all_providers = lambda: [
            ("k1", SimpleNamespace(provider_config={"id": "x", "name": "A"})),
            ("k2", SimpleNamespace(provider_config={"id": "x", "model": "B"})),
        ]
        options = webapi._collect_provider_options(plugin)
        assert len(options) == 1
        assert options[0]["id"] == "x"
        # 无 get_all_providers → manager inst_map 兜底
        del plugin.context.get_all_providers
        plugin.context.provider_manager = SimpleNamespace(
            inst_map={"pm": SimpleNamespace(config={"id": "m1", "model": "M"})}
        )
        options = webapi._collect_provider_options(plugin)
        assert options[0]["id"] == "m1"

        # get_all_providers 抛异常 → 同样兜底
        def boom():
            raise RuntimeError("boom")

        plugin.context.get_all_providers = boom
        options = webapi._collect_provider_options(plugin)
        assert options[0]["id"] == "m1"

    with_plugin(tmp_path, scenario)


def test_api_providers_happy_and_error(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        result = await webapi._api_providers(plugin)
        assert result["ok"] is True
        assert result["providers"] == []

        def boom():
            raise RuntimeError("boom")

        # 收集器内部异常被消化为 fallback 空列表
        plugin.context.get_all_providers = boom
        result = await webapi._api_providers(plugin)
        assert result["ok"] is True
        assert result["providers"] == []
        # 收集器抛非预期异常 → API 层兜底 ok:False
        original = webapi._collect_provider_options

        def hard_boom(plugin):
            raise RuntimeError("collector failed")

        webapi._collect_provider_options = hard_boom
        try:
            result = await webapi._api_providers(plugin)
            assert result["ok"] is False
            assert result["providers"] == []
        finally:
            webapi._collect_provider_options = original

    with_plugin(tmp_path, scenario)


# ============================================================================
# status / cleanup / theme API
# ============================================================================


def test_api_status(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        status = await webapi._api_status(plugin)
        assert status["ok"] is True
        assert status["runtime_enabled"] is plugin.runtime_enabled
        assert status["whitelist_count"] == len(plugin.settings.whitelist)
        assert status["decision_model_enabled"] is plugin.settings.decision_model_enabled
        # 调试面板导出（ticket 14）：代次/运行中/任务数/缓存规模/最近裁决
        assert "gate" in status and "generation" in status["gate"]
        assert set(status["tasks"]) == {"delay", "running_check", "background"}
        assert set(status["caches"]) == {"events", "image_events", "sessions"}
        assert isinstance(status["last_decisions"], dict)
        plugin._last_decisions["s1"] = {
            "at": 1.0,
            "trigger": "patrol",
            "should_reply": False,
            "reason": "冷却中",
        }
        status = await webapi._api_status(plugin)
        assert status["last_decisions"]["s1"]["reason"] == "冷却中"
        assert plugin._delay_tasks is not None and plugin._background_tasks is not None

    with_plugin(tmp_path, scenario)


def test_api_cleanup_image_cache_paths(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        # 关闭中拒绝
        plugin._stopping = True
        result = await webapi._api_cleanup_image_cache(plugin)
        assert result["ok"] is False
        plugin._stopping = False

        # 成功路径
        async def fake_cleanup():
            return 3

        plugin._run_image_cleanup = fake_cleanup
        result = await webapi._api_cleanup_image_cache(plugin)
        assert result["ok"] is True
        assert result["removed"] == 3
        assert result["max_age_sec"] == int(plugin.settings.vision_image_age_sec)

        # 异常路径
        async def boom():
            raise RuntimeError("disk")

        plugin._run_image_cleanup = boom
        result = await webapi._api_cleanup_image_cache(plugin)
        assert result["ok"] is False

        # CancelledError 不吞掉，原样上抛
        async def cancelled():
            raise asyncio.CancelledError()

        plugin._run_image_cleanup = cancelled
        with pytest.raises(asyncio.CancelledError):
            await webapi._api_cleanup_image_cache(plugin)

    with_plugin(tmp_path, scenario)


def test_ui_theme_load_and_get(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        # 损坏文件回退 auto
        plugin._ui_prefs_path.write_text("{bad json", encoding="utf-8")
        assert webapi._load_ui_theme(plugin) == "auto"
        # 非法主题回退 auto
        plugin._ui_prefs_path.write_text('{"theme": "neon"}', encoding="utf-8")
        assert webapi._load_ui_theme(plugin) == "auto"
        # 正常读取
        plugin._ui_prefs_path.write_text('{"theme": "dark"}', encoding="utf-8")
        assert webapi._load_ui_theme(plugin) == "dark"
        # GET 返回实例当前主题
        plugin._ui_theme = "light"
        result = await webapi._api_get_ui_theme(plugin)
        assert result["ok"] is True
        assert result["theme"] == "light"

    with_plugin(tmp_path, scenario)


def test_api_post_ui_theme_paths(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        web = sys.modules["astrbot.api.web"]
        # 非 dict 请求体
        web.request.payload = ["dark"]
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is False
        # 无效主题
        web.request.payload = {"theme": "neon"}
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is False
        # 保存失败 → error（父目录不存在）
        plugin._ui_prefs_path = tmp_path / "no_dir" / "ui_prefs.json"
        web.request.payload = {"theme": "dark"}
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is False
        # 成功
        plugin._ui_prefs_path = tmp_path / "ui_prefs.json"
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is True
        assert result["theme"] == "dark"
        assert plugin._ui_theme == "dark"
        # 相同主题不重复写盘
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is True

    with_plugin(tmp_path, scenario)


# ============================================================================
# _request_json 回退与 config API 边界
# ============================================================================


def test_request_json_fallbacks(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        original = webapi.request

        class OnlyGetJson:
            async def get_json(self, silent=False):
                return {"via": "get_json"}

        class Nothing:
            pass

        class Boom:
            async def json(self, default=None):
                raise RuntimeError("json read failed")

        class TypeErrBoom:
            # json(default=...) 签名不兼容 → 回退无参调用
            async def json(self):
                return {"via": "bare"}

        try:
            webapi.request = OnlyGetJson()
            assert await webapi._request_json() == {"via": "get_json"}
            webapi.request = Nothing()
            with pytest.raises(RuntimeError):
                await webapi._request_json()
            webapi.request = TypeErrBoom()
            assert await webapi._request_json() == {"via": "bare"}
            # _api_post_ui_theme 在 json 读取失败时回退空 dict → 无效主题
            webapi.request = Boom()
            result = await webapi._api_post_ui_theme(plugin)
            assert result["ok"] is False
        finally:
            webapi.request = original

    with_plugin(tmp_path, scenario)


def test_api_get_config_error_path(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]

        class BoomSettings:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        plugin.settings = BoomSettings()
        result = await webapi._api_get_config(plugin)
        assert result["ok"] is False

    with_plugin(tmp_path, scenario)


def test_api_post_config_stopping(tmp_path) -> None:
    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"cooldown_sec": 10}
        plugin._stopping = True
        result = await plugin._api_post_config()
        assert result["ok"] is False

    with_plugin(tmp_path, scenario)


def test_apply_vision_change_clears_parser_cache(tmp_path) -> None:
    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        plugin._image_parsers["x"] = object()
        plugin._image_parser_timeout = 30.0
        web.request.payload = {"vision_provider_id": "new"}
        result = await plugin._api_post_config()
        assert result["ok"] is True
        assert plugin.settings.vision_provider_id == "new"
        assert plugin._image_parsers == {}
        assert plugin._image_parser_timeout is None

    with_plugin(tmp_path, scenario)


def test_rollback_drops_unknown_delay_umo(tmp_path) -> None:
    """回滚重调度时，快照 delay_umos 中不在 sessions 的会话走 dropped 分支。"""

    async def scenario(plugin, main):
        umo2 = "fake:group:999"
        done = asyncio.get_running_loop().create_task(asyncio.sleep(0))
        await done
        plugin._delay_tasks[umo2] = done

        async def boom():
            raise OSError("disk full")

        plugin._save_storage = boom
        web = sys.modules["astrbot.api.web"]
        # 白名单变更使配置应用路径进入回滚
        web.request.payload = {"whitelist_sessions": []}
        result = await plugin._api_post_config()
        assert result.get("ok") is False
        # umo2 不在 sessions：回滚不为其重建延迟检查（原任务已被移除且无新任务）
        assert umo2 not in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def test_rollback_reschedule_failure_is_logged(tmp_path) -> None:
    """回滚重调度抛异常时只记录 debug 日志，不中断回滚。"""

    async def scenario(plugin, main):
        plugin.sessions[UMO] = plugin._state_for(UMO)
        done = asyncio.get_running_loop().create_task(asyncio.sleep(0))
        await done
        plugin._delay_tasks[UMO] = done

        async def boom():
            raise OSError("disk full")

        plugin._save_storage = boom
        plugin._schedule_delayed_check = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("reschedule failed")
        )
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist_sessions": []}
        result = await plugin._api_post_config()
        assert result.get("ok") is False

    with_plugin(tmp_path, scenario)


# ============================================================================
# CONFIG_SCHEMA_KEYS 与 _conf_schema.json 一致性（三方镜像漂移防线）
# ============================================================================


def test_config_schema_keys_cover_schema_json(tmp_path) -> None:
    """webapi 配置白名单必须与 _conf_schema.json 全键一致。

    schema 新增键时该守卫立即变红，迫使同步更新 webapi 解析分支，
    防止新字段被静默吞掉（MP1-4 的同类问题不再复发）。
    """

    import json

    from .host_stubs import ROOT

    async def scenario(plugin, main):
        schema_path = ROOT / "_conf_schema.json"
        schema_keys = set(json.loads(schema_path.read_text(encoding="utf-8")))
        # 0.9.2 起别名层已移除：白名单 == schema 键，无额外兼容键
        assert _webapi().CONFIG_SCHEMA_KEYS == schema_keys

    with_plugin(tmp_path, scenario)
