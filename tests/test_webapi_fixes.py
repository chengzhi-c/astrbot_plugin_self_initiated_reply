"""webapi 边界测试补盲（0.8.1 阶段 C）。

覆盖 webapi.py 的拒绝路径与分支：strict 校验、provider 收集链、
cleanup/status/theme API、回滚 dropped 分支与 _request_json 回退。
"""

from __future__ import annotations

import asyncio
import json
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
    with pytest.raises(ValueError):
        webapi._parse_config_updates({"whitelist_sessions": ["has\ttab"]})
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
        assert "pipeline_mode" not in status
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


def test_api_status_contains_failure_and_hides_details(tmp_path) -> None:
    """状态端点失败时返回结构化错误，且不回显异常细节（0.9.4 阶段 1.7）。

    补这条 except 前，本端点是唯一没有兜底的 ``_api_*`` 处理器。补的当时并无
    可达异常（见该函数 docstring），故这里用删属性人工制造失败——不是模拟宿主
    API 异常，而是验证兜底本身：返回 ``ok=False``、给出中文文案、且异常原文
    （这里是属性名）不出现在响应里。
    """

    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        del plugin._last_decisions  # dict() 取值即 AttributeError

        status = await webapi._api_status(plugin)

        assert status["ok"] is False
        assert status["error"] == "状态读取失败"
        # 异常原文含属性名，不得出现在回显里
        assert "_last_decisions" not in str(status)

    with_plugin(tmp_path, scenario)


def test_bound_api_handlers_match_class_declarations() -> None:
    """``bind_api_handlers`` 绑定的名字集合 == 主类里的裸注解声明（0.9.4 阶段 2.2）。

    这四个处理器不在类里 ``def``，而是运行时以 ``partial(...)`` 挂到实例上（供约 30 处
    测试与外部以 ``plugin._api_*`` 调用）。主类新增了对应的裸注解，让读者在类里搜得到
    这些名字。两侧各自手工维护，漂移方向决定后果：

    - 绑了没声明：读者在类里搜不到，回到阶段 2.2 之前的状态（声明白写）；
    - 声明了没绑：注解承诺了一个运行时不存在的属性，比没有注解更误导——读者会以为
      ``plugin._api_xxx`` 可调用，实际 ``AttributeError``。

    刻意用 AST 读两侧源码而非运行时 ``dir(plugin)``：裸注解**不创建**类属性（这正是
    选它的原因——不遮蔽 partial 绑定），运行时反射看不到它，只有读源码才能比对。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    webapi_tree = ast.parse((root / "webapi.py").read_text(encoding="utf-8"))
    bound: set[str] = set()
    for node in ast.walk(webapi_tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "bind_api_handlers":
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "plugin"
                ):
                    bound.add(target.attr)
    assert bound, "未从 bind_api_handlers 提取到任何绑定（写法变了，需复核本守卫）"

    main_tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    declared: set[str] = set()
    for node in ast.walk(main_tree):
        if not isinstance(node, ast.ClassDef) or node.name != "SelfInitiatedReplyPlugin":
            continue
        for stmt in node.body:
            # 裸注解：AnnAssign 且 value 为 None（有 value 就是真赋值，会遮蔽 partial）
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id.startswith("_api_"):
                    assert stmt.value is None, (
                        f"{stmt.target.id} 被赋了值，会遮蔽 bind_api_handlers 的 partial 绑定；"
                        f"这里必须是裸注解"
                    )
                    declared.add(stmt.target.id)

    assert declared == bound, (
        f"bind_api_handlers 绑定 {sorted(bound)}，主类声明 {sorted(declared)}；"
        f"只绑未声明={sorted(bound - declared)}，只声明未绑={sorted(declared - bound)}"
    )


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

        plugin._scheduler.run_image_cleanup = fake_cleanup
        result = await webapi._api_cleanup_image_cache(plugin)
        assert result["ok"] is True
        assert result["removed"] == 3
        assert result["max_age_sec"] == int(plugin.settings.vision_image_age_sec)

        # 异常路径
        async def boom():
            raise RuntimeError("disk")

        plugin._scheduler.run_image_cleanup = boom
        result = await webapi._api_cleanup_image_cache(plugin)
        assert result["ok"] is False

        # CancelledError 不吞掉，原样上抛
        async def cancelled():
            raise asyncio.CancelledError()

        plugin._scheduler.run_image_cleanup = cancelled
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

        class BodyTypeErr:
            def __init__(self) -> None:
                self.calls = 0

            async def json(self, default=None):
                self.calls += 1
                raise TypeError("reader body failed")

        try:
            webapi.request = OnlyGetJson()
            assert await webapi._request_json() == {"via": "get_json"}
            webapi.request = Nothing()
            with pytest.raises(RuntimeError):
                await webapi._request_json()
            webapi.request = TypeErrBoom()
            assert await webapi._request_json() == {"via": "bare"}
            reader = BodyTypeErr()
            webapi.request = reader
            with pytest.raises(TypeError, match="reader body failed"):
                await webapi._request_json()
            assert reader.calls == 1
            # _api_post_ui_theme 在 json 读取失败时回退空 dict → 无效主题
            webapi.request = Boom()
            result = await webapi._api_post_ui_theme(plugin)
            assert result["ok"] is False
        finally:
            webapi.request = original

    with_plugin(tmp_path, scenario)


def test_api_get_config_error_path(tmp_path) -> None:
    """读取失败必须返回可序列化的失败载荷，不能返回 None（RL-5）。

    Quart 无法序列化 None：裸 ``return`` 会把一次可恢复的读取失败放大成 500。
    """

    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]

        class BoomSettings:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        plugin.settings = BoomSettings()
        result = await webapi._api_get_config(plugin)
        assert result is not None, "异常分支返回 None，Quart 会抛 TypeError 变成 500"
        assert isinstance(result, dict)
        assert result["ok"] is False
        assert json.dumps(result), "失败载荷必须可 JSON 序列化"

    with_plugin(tmp_path, scenario)


def test_api_post_config_stopping(tmp_path) -> None:
    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"cooldown_sec": 10}
        plugin._stopping = True
        result = await plugin._api_post_config()
        assert result["ok"] is False

    with_plugin(tmp_path, scenario)


def test_api_post_config_returns_normalized_values(tmp_path) -> None:
    """成功的 POST 必须返回规格表实际生效的规范值与调整字段。"""

    async def scenario(plugin, main):
        models = sys.modules[f"{PACKAGE}.models"]
        web = sys.modules["astrbot.api.web"]
        whitelist = [f"session-{index:04d}" for index in range(models.MAX_WHITELIST_SIZE + 1)]
        web.request.payload = {
            "cooldown_sec": 999999,
            "decision_temperature": 9,
            "decision_prompt_template": "p" * (models.MAX_PROMPT_LENGTH + 1),
            "whitelist_sessions": list(reversed(whitelist)),
        }

        result = await plugin._api_post_config()

        assert result["ok"] is True
        assert result["adjusted_fields"] == [
            "cooldown_sec",
            "decision_prompt_template",
            "decision_temperature",
            "whitelist_sessions",
        ]
        config = result["config"]
        assert config["cooldown_sec"] == int(models.CONFIG_SPEC_BY_KEY["cooldown_sec"].maximum)
        assert (
            config["decision_temperature"]
            == models.CONFIG_SPEC_BY_KEY["decision_temperature"].maximum
        )
        assert len(config["decision_prompt_template"]) == models.MAX_PROMPT_LENGTH
        assert config["whitelist_sessions"] == sorted(whitelist)[1:]
        assert config["whitelist_sessions"] == sorted(config["whitelist_sessions"])
        assert plugin.settings.to_config_dict() == config

    with_plugin(tmp_path, scenario)


def test_api_get_config_sorts_whitelist_sessions(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]

        class UnsortedSet(set[str]):
            def __iter__(self):
                return iter(("session-z", "session-a"))

        plugin.settings.whitelist = UnsortedSet({"session-a", "session-z"})

        result = await webapi._api_get_config(plugin)

        assert result["whitelist_sessions"] == ["session-a", "session-z"]

    with_plugin(tmp_path, scenario)


def test_api_get_config_exposes_panel_surface_only(tmp_path) -> None:
    """GET /config 只带 panel 面与三个视图字段，不含宿主独占键。"""

    async def scenario(plugin, main):
        models = sys.modules[f"{PACKAGE}.models"]
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        result = await webapi._api_get_config(plugin)
        panel_keys = {spec.key for spec in models.panel_config_specs()}
        view_keys = {"ok", "runtime_enabled", "decision_prompt_default"}
        assert set(result) == panel_keys | view_keys
        assert "patrol_inactive_after_sec" not in result
        assert result["ok"] is True
        assert result["runtime_enabled"] is plugin.runtime_enabled
        assert result["decision_prompt_default"] == models.DEFAULT_DECISION_PROMPT_TEMPLATE

    with_plugin(tmp_path, scenario)


def test_web_apis_do_not_register_overview_route(tmp_path) -> None:
    """白名单计数只走 GET /config，不再注册第三份 overview 副本。"""

    async def scenario(plugin, main):
        routes = [route for route, *_ in plugin.context.register_web_api_calls]
        assert not any(route.endswith("/unified/overview") for route in routes)
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        plugin.settings.whitelist = {"qq:GroupMessage:1", "qq:GroupMessage:2"}
        payload = await webapi._api_get_config(plugin)
        assert payload["whitelist_sessions"] == [
            "qq:GroupMessage:1",
            "qq:GroupMessage:2",
        ]

    with_plugin(tmp_path, scenario)


@pytest.mark.parametrize(
    "payload",
    [
        {"vision_provider_id": "new"},
        {"vision_skip_stickers": True},
    ],
)
def test_apply_vision_change_clears_parser_cache(tmp_path, payload) -> None:
    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        plugin._image_parsers["x"] = object()
        plugin._image_parser_timeout = 30.0
        web.request.payload = payload
        result = await plugin._api_post_config()
        assert result["ok"] is True
        for key, value in payload.items():
            assert getattr(plugin.settings, key) == value
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
        plugin._scheduler.schedule_delayed_check = lambda *a, **k: (_ for _ in ()).throw(
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
