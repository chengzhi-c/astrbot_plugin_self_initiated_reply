"""webapi 拒绝路径与分支。

覆盖 webapi.py 的拒绝路径与分支：strict 校验、provider 收集链、
cleanup/status/theme API、回滚 dropped 分支与 _request_json 回退。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ._support import make_event
from .host_stubs import (
    capture_logs,
    until,
    webapi_module,
    with_plugin,
)

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
        # FakeRequest 是进程级单例（webapi 在 import 期绑定它）：error 不随之清零，
        # 本文件中途失败就会把「请求体不是合法 JSON」传染给之后每一个 POST，
        # 级联出成片与失败原因无关的假红。
        web.request.payload = {}
        web.request.error = None
        web.request.swallow_errors = False


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
        # 几百位整数字面量：json.loads 产出任意精度 int，float() 抛的是
        # OverflowError（ArithmeticError，不是 ValueError）。只断言「抛了
        # ValueError」时，一个漏接的 except 会让它逃到最外层兜底。
        ({"decision_temperature": 10**400}, "decision_temperature"),
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
        ({"judge_provider_id": True}, "judge_provider_id"),
        ({"vision_provider_id": {"a": 1}}, "vision_provider_id"),
        ({"vision_judge_provider_id": ["a"]}, "vision_judge_provider_id"),
        ("not a dict", "请求体必须是 JSON 对象"),
        ({"whitelist_sessions": "abc"}, "whitelist_sessions 必须是数组"),
    ],
)
def test_parse_config_updates_rejects_invalid(payload: Any, field: str) -> None:
    # match 钉住字段名：只断言「抛了 ValueError」时，函数顶部一句无差别
    # raise 能让全部行保持绿，而用户拿到的是定位不到字段的报错。
    with pytest.raises(ValueError, match=re.escape(field)):
        _webapi()._parse_config_updates(payload)


def test_parse_config_updates_str_keys_accept_strings_and_none() -> None:
    """str 键写侧严格类型但宽容取值：字符串照收（含超长，交由 coerce 截断），None 视作复位。"""
    webapi = _webapi()
    assert webapi._parse_config_updates({"judge_provider_id": "prov-1"})["judge_provider_id"] == (
        "prov-1"
    )
    assert webapi._parse_config_updates({"judge_provider_id": None})["judge_provider_id"] == ""
    long_value = webapi._parse_config_updates({"judge_provider_id": "p" * 300})
    assert long_value["judge_provider_id"] == "p" * 300


def test_str_provider_keys_truncate_on_persist(tmp_path) -> None:
    """超长 provider id 经读侧 coerce 截断到 MAX_PROVIDER_ID_LEN 并留 warning。"""

    async def scenario(plugin, main):
        models = sys.modules[f"{PACKAGE}.models"]
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"judge_provider_id": "p" * (models.MAX_PROVIDER_ID_LEN + 50)}

        result = await webapi_module(main)._api_post_config(plugin)

        assert result["ok"] is True
        assert len(result["config"]["judge_provider_id"]) == models.MAX_PROVIDER_ID_LEN
        assert plugin.settings.judge_provider_id == "p" * models.MAX_PROVIDER_ID_LEN

    with_plugin(tmp_path, scenario)


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
    """别名键 fail loud 拒绝（旧前端得显式错误而非静默吞掉）。"""
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
    # 写侧只做类型/空白规范化，不回落内置模板：复位语义单点在读侧
    # （models.coerce_config_value），落到响应里的默认值由 3.4 的锚定测试钉住。
    assert updates["decision_prompt_template"] == ""
    assert updates["judge_provider_id"] == ""
    assert updates["vision_provider_id"] == "42"
    # 0 是 falsy：与 judge_provider_id 一致的规范化语义
    assert updates["vision_judge_provider_id"] == ""


def test_parse_config_updates_rejects_container_types_for_text() -> None:
    """text 规格与 str 规格同口径拒绝容器类型。

    否则 ``{"decision_prompt_template": {"a": 1}}`` 会落盘成 Python repr
    字符串 ``"{'a': 1}"``（随后被长度上限截断），既不是用户输入也不是有效
    模板，属静默损坏。
    """
    webapi = _webapi()
    for bad in (True, {"a": 1}, ["x"]):
        with pytest.raises(ValueError, match="必须是字符串"):
            webapi._parse_config_updates({"decision_prompt_template": bad})


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
    """状态端点的字段级读数必须与插件实况一致，不能是恒真的 ``is`` 比较。

    缺陷形态：宿主桩默认 ``enabled=True`` / ``decision_model_enabled=False``，而
    场景从不翻面运行态，``status["runtime_enabled"] is plugin.runtime_enabled``
    退化成 ``True is True``；gate 段两条只是键存在性断言。把这些字段换成硬编码
    常量，旧用例全绿——而 ``runtime_enabled`` 正是排障时判断"插件是不是还活着"
    的承重字段（持久配置在 DEGRADED 下仍显示 True），字段级谎报直接指向错误方向。

    变异锚定：把 ``_api_status`` 里的 ``plugin.runtime_enabled`` 换成硬编码
    ``True``、``decision_model_enabled`` 换成硬编码 ``False``、或把 gate 两张表
    换成 ``{}`` / ``[]``，本用例红。
    """

    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        # 翻面：不翻的话 `is` 比较恒真，硬编码常量也能全绿
        plugin.runtime_enabled = False
        plugin.settings.decision_model_enabled = True
        status = await webapi._api_status(plugin)
        assert status["ok"] is True
        assert status["runtime_enabled"] is False, status["runtime_enabled"]
        assert status["whitelist_count"] == len(plugin.settings.whitelist)
        assert status["decision_model_enabled"] is True, status["decision_model_enabled"]
        assert "pipeline_mode" not in status
        # 代次与运行集按内容断言：键存在性放不掉硬编码空表
        plugin._gate.advance(UMO)
        plugin._gate.mark_running(UMO)
        status = await webapi._api_status(plugin)
        assert status["gate"]["generation"].get(UMO) == plugin._gate.current(UMO), (
            f"代次表内容与实况不符：{status['gate']['generation']}"
        )
        assert status["gate"]["running"] == sorted(plugin._gate.running_sessions_view), (
            f"运行集内容与实况不符：{status['gate']['running']}"
        )
        assert UMO in status["gate"]["running"], status["gate"]["running"]
        # 调试面板导出：任务数/缓存规模/最近裁决
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

    with_plugin(tmp_path, scenario)


def test_api_status_contains_failure_and_hides_details(tmp_path) -> None:
    """状态端点失败时返回结构化错误，且不回显异常细节。

    补这条 except 前，本端点是唯一没有兜底的 ``_api_*`` 处理器。补的当时并无
    可达异常（见该函数 docstring），故这里用删属性人工制造失败，不是模拟宿主
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


def test_ui_prefs_corrupt_logs_warning(tmp_path, caplog) -> None:
    """ui_prefs.json 损坏必须留痕：静默回退 + 后续整块覆写会永久抹掉原偏好。

    缺陷形态：``load_ui_prefs`` 的 except 分支裸 ``return "auto", False, False``。
    回退本身是对的，但下一次 POST /ui/theme 会整块写 theme/dim/bold 三个字段，
    原本存着的偏好于是被静默改掉，而日志里没有任何一行——排障者只看到「主题变
    回去了」，无从判断是文件坏了还是用户改的（契约 §9「不记则无从定位」；同仓库
    state.json 的损坏路径记了两条 ERROR，这边一条都没有）。

    变异锚定：删掉那条 ``logger.warning``，本用例红。
    """
    import logging

    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        plugin._ui_prefs_path.write_text("{bad json", encoding="utf-8")
        with capture_logs(caplog, webapi_module(main).logger, logging.WARNING):
            assert webapi.load_ui_prefs(plugin) == ("auto", False, False)
        warnings = [
            record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert any("ui prefs unreadable" in msg for msg in warnings), (
            f"ui_prefs 损坏未记 warning，原偏好会被静默抹掉且无从定位：{warnings}"
        )

    with_plugin(tmp_path, scenario)


def test_ui_theme_load_and_get(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        # 损坏文件回退 auto
        plugin._ui_prefs_path.write_text("{bad json", encoding="utf-8")
        assert webapi.load_ui_prefs(plugin) == ("auto", False, False)
        # 非法主题回退 auto；缺 dim/bold 默认关
        plugin._ui_prefs_path.write_text('{"theme": "neon"}', encoding="utf-8")
        assert webapi.load_ui_prefs(plugin) == ("auto", False, False)
        # 正常读取
        plugin._ui_prefs_path.write_text(
            '{"theme": "dark", "dim": true, "bold": false}', encoding="utf-8"
        )
        assert webapi.load_ui_prefs(plugin) == ("dark", True, False)
        # GET 返回实例当前偏好
        plugin._ui_theme = "light"
        plugin._ui_dim = False
        plugin._ui_bold = True
        result = await webapi._api_get_ui_theme(plugin)
        assert result == {"ok": True, "theme": "light", "dim": False, "bold": True}

    with_plugin(tmp_path, scenario)


def test_concurrent_ui_theme_posts_preserve_both_fields(tmp_path) -> None:
    """并发 POST（一个改 dim、一个改 bold）不得互相覆盖。

    读-改-写块（243-267）之间无 await，asyncio 单线程下天然原子；这条测试
    验证该不变量，若未来有人在块内插入 await 则变红。
    """

    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        plugin._ui_theme = "auto"
        plugin._ui_dim = False
        plugin._ui_bold = False
        plugin._ui_prefs_path = tmp_path / "ui_prefs.json"

        payloads = [{"dim": True}, {"bold": True}]
        idx = {"n": 0}
        original_request_json = webapi._request_json

        async def rotating_json():
            data = payloads[idx["n"] % len(payloads)]
            idx["n"] += 1
            await asyncio.sleep(0)  # 让另一条 POST 交错进入
            return data

        webapi._request_json = rotating_json
        try:
            results = await asyncio.gather(
                webapi._api_post_ui_theme(plugin),
                webapi._api_post_ui_theme(plugin),
            )
        finally:
            webapi._request_json = original_request_json

        assert all(r["ok"] for r in results)
        # 两条 POST 各改一个字段，最终两份偏好都保留
        assert plugin._ui_dim is True
        assert plugin._ui_bold is True
        saved = json.loads(plugin._ui_prefs_path.read_text(encoding="utf-8"))
        assert saved == {"theme": "auto", "dim": True, "bold": True}

    with_plugin(tmp_path, scenario)


def test_api_post_ui_theme_paths(tmp_path) -> None:
    async def scenario(plugin, main):
        webapi = sys.modules[f"{PACKAGE}.webapi"]
        web = sys.modules["astrbot.api.web"]
        # 关停中拒写（与 config / image-cache 端点同口径）。除 ok=False 外还断言
        # **未落盘**：teardown 之后写进 ui_prefs.json 的偏好会在下次启动被读回，
        # 用户看到的是「已被丢弃」却仍然生效的旧设置。
        plugin._stopping = True
        plugin._ui_prefs_path = tmp_path / "stopping.json"
        web.request.payload = {"theme": "dark"}
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is False
        assert not plugin._ui_prefs_path.exists(), "关停中仍写入了 UI 偏好"
        assert plugin._ui_theme != "dark"
        plugin._stopping = False
        # 请求体读不出（非法 JSON）：与「空对象」是两回事。报成「未提供任何字段」
        # 会让人去数字段名，而真正坏的是请求体本身。
        web.request.error = ValueError("invalid json body")
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is False
        assert "合法 JSON" in result["error"], result
        web.request.error = None
        # 非 dict 请求体
        web.request.payload = ["dark"]
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is False
        # 无效主题
        web.request.payload = {"theme": "neon"}
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is False
        # 父目录不存在时原子写会 mkdir，必须成功
        plugin._ui_theme = "auto"
        plugin._ui_prefs_path = tmp_path / "no_dir" / "ui_prefs.json"
        web.request.payload = {"theme": "dark"}
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is True
        assert json.loads(plugin._ui_prefs_path.read_text(encoding="utf-8")) == {
            "theme": "dark",
            "dim": False,
            "bold": False,
        }
        # 写入目标已是目录 → 失败
        plugin._ui_theme = "auto"
        blocked = tmp_path / "blocked.json"
        blocked.mkdir()
        plugin._ui_prefs_path = blocked
        web.request.payload = {"theme": "light"}
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is False
        # 成功
        plugin._ui_theme = "auto"
        plugin._ui_dim = False
        plugin._ui_bold = False
        plugin._ui_prefs_path = tmp_path / "ui_prefs.json"
        web.request.payload = {"theme": "dark"}
        result = await webapi._api_post_ui_theme(plugin)
        assert result["ok"] is True
        assert result["theme"] == "dark"
        assert plugin._ui_theme == "dark"
        assert json.loads(plugin._ui_prefs_path.read_text(encoding="utf-8")) == {
            "theme": "dark",
            "dim": False,
            "bold": False,
        }
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


def test_post_config_unreadable_body_differs_from_empty_object(tmp_path) -> None:
    """宿主 reader 吞掉坏 body 返回 default 时，POST /config 必须报错。

    缺陷形态：astrbot 的 PluginRequestProxy.json() 读不出 body 时返回
    default 而不抛（与 Quart 的 request.json() 相反）。default 写成 {} 时
    「坏请求体」与「无字段保存」无法区分，垃圾 body 会拿到 ok:true 的全量
    配置响应。哨兵 default 让两者可区分。
    """
    web = sys.modules["astrbot.api.web"]
    web.request.payload = {}
    web.request.error = ValueError("body unreadable")
    web.request.swallow_errors = True  # 宿主 PluginRequestProxy 的行为

    async def scenario(plugin, main):
        result = await webapi_module(main)._api_post_config(plugin)
        assert result == {"ok": False, "error": "请求体不是合法 JSON"}

    with_plugin(tmp_path, scenario)


def test_post_config_non_string_list_items_rejected(tmp_path) -> None:
    """list 条目必须是字符串：[1, null, true] 不得变成 ["1","None","True"]。

    缺陷形态：str(raw) 兜底把非字符串条目字符串化成活条目，持久化进白名单
    后集合比较通过、UI 无感知，磁盘却长了垃圾条目。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist_sessions": [1, None, True]}

        result = await webapi_module(main)._api_post_config(plugin)

        assert result["ok"] is False
        assert "条目必须是字符串" in result["error"]
        # 拒绝即整体不变：白名单保持加载时的值，不落任何垃圾条目
        assert plugin.settings.whitelist == {"fake:group:123"}

    with_plugin(tmp_path, scenario)


def test_post_config_splits_commas_like_the_page(tmp_path) -> None:
    """api 模式按页面同一套分隔符切分：["a,b"] 存成两条而非一条。

    缺陷形态：页面按 [\\n,，]+ 切分展示与保存，后端原样存一条；下一轮加载
    页面把它拆成两条，一个会话被无声劈成两个（裸 id 还是群通配符）。
    """

    async def scenario(plugin, main):
        utils = sys.modules[f"{PACKAGE}.utils"]
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist_sessions": ["qq:GroupMessage:1,qq:GroupMessage:2"]}

        result = await webapi_module(main)._api_post_config(plugin)

        assert result["ok"] is True
        assert plugin.settings.whitelist == {"qq:GroupMessage:1", "qq:GroupMessage:2"}
        assert len(result["config"]["whitelist_sessions"]) == 2
        assert utils.session_whitelisted("qq:GroupMessage:1", plugin.settings.whitelist)

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
        result = await webapi_module(main)._api_post_config(plugin)
        assert result["ok"] is False

    with_plugin(tmp_path, scenario)


def test_api_post_config_body_read_does_not_hold_the_config_lock(tmp_path) -> None:
    """读取请求体不得持锁：等待客户端上传的时长由客户端决定。

    缺陷形态：``await _request_json()`` 跑在 ``async with plugin._config_lock``
    里面，一个慢/挂死的上传就连坐 ``terminate()`` 与一切 ``/selfreply`` 写指令
    （它们同一把 ``_config_lock``）。§7 的「在 ``_config_lock`` 内比较 revision」
    不受影响——比较仍在锁内、且在任何变更之前。

    变异锚定：把 ``await _request_json()`` 挪回 ``async with
    plugin._config_lock`` 内，本用例红（terminate 被连坐阻塞）。
    """

    async def scenario(plugin, main):
        webapi = webapi_module(main)
        original = webapi._request_json
        reading = asyncio.Event()
        release = asyncio.Event()

        async def blocked_json():
            reading.set()
            await release.wait()
            return {"cooldown_sec": 111}

        webapi._request_json = blocked_json
        posting = asyncio.create_task(webapi._api_post_config(plugin))
        termination = asyncio.create_task(plugin.terminate())
        try:
            await reading.wait()
            done, _ = await asyncio.wait({termination}, timeout=5.0)
            assert termination in done, (
                "POST 读取请求体期间持锁：terminate() 被客户端上传时长连坐阻塞"
            )
        finally:
            release.set()
            webapi._request_json = original
            await asyncio.wait({posting, termination}, timeout=5.0)

        # 请求本身照常收口：放行后拿到的是关停拒绝，而不是被丢掉或挂住
        assert posting.result()["ok"] is False

    with_plugin(tmp_path, scenario)


def test_api_post_config_malformed_body_keeps_field_error(tmp_path) -> None:
    """请求体与字段校验失败的文案必须原样回显，不得被通用文案吞掉。

    前端表单依赖这串文案定位出错字段；校验移到锁外之后这条回显路径仍要走通。
    """

    async def scenario(plugin, main):
        webapi = webapi_module(main)
        web = sys.modules["astrbot.api.web"]
        original_cooldown = plugin.settings.cooldown_sec

        web.request.payload = ["not", "a", "dict"]
        result = await webapi._api_post_config(plugin)
        assert result == {"ok": False, "error": "请求体必须是 JSON 对象"}

        web.request.payload = {"cooldown_sec": "5s"}
        result = await webapi._api_post_config(plugin)
        assert result["ok"] is False
        assert result["error"] == "cooldown_sec 必须是整数"
        assert plugin.settings.cooldown_sec == original_cooldown

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

        result = await webapi_module(main)._api_post_config(plugin)

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
        assert config["whitelist_sessions"] == sorted(whitelist)[: models.MAX_WHITELIST_SIZE]
        assert config["whitelist_sessions"] == sorted(config["whitelist_sessions"])
        assert plugin.settings.to_config_dict() == config
        # 运行态必须回传：前端徽标按 result.runtime_enabled 刷新（缺失时 ?? 链会
        # 静默回退到保存前的旧值，切换总开关后徽标不更新）。
        assert "runtime_enabled" in result

    with_plugin(tmp_path, scenario)


def test_cancelled_post_config_write_does_not_land_after_rollback(tmp_path) -> None:
    """被取消的 POST /config 的写盘线程，不得在回滚之后才落地。

    缺陷形态：``storage.apersist_settings_config`` 把 ``write_json_atomic`` 丢进
    ``asyncio.to_thread`` 后裸 await。任务在此被取消时 await 立刻抛
    ``CancelledError``，而**写盘线程不会被取消**，``os.replace`` 仍会在稍后落地。
    随后回滚把内存恢复成旧值并再次写盘，两次 ``os.replace`` 的先后只由线程调度
    决定：旧值先落、新值后落时，磁盘留下的是**已被驳回**的新配置，而内存已是旧值
    ——磁盘与运行期 Settings 持久不一致且不会自愈（重启加载到这份被驳回的配置，
    ``config_revision`` 由内存算也与磁盘脱节）。同一 hazard 在
    ``plugin_state.save_storage`` 有显式防护，配置写盘这条路径没有。

    变异锚定：把 ``apersist_settings_config`` 的 shield 等待改回裸
    ``await asyncio.to_thread(...)``，本用例红。
    """
    import threading
    import time

    async def scenario(plugin, main):
        storage = importlib.import_module(main.__package__ + ".storage")
        original_write = storage.write_json_atomic
        entered = threading.Event()
        landed = threading.Event()
        calls = {"n": 0}

        def held_write(path: Path, data: dict[str, Any], *, abandoned: Any = None) -> bool:
            calls["n"] += 1
            first = calls["n"] == 1
            if first:
                # 让写盘线程确实进入 os.replace 之前的临界区，调用方再取消——
                # 这正是"取消落在 to_thread 上"的真实形态
                entered.set()
                time.sleep(0.3)
            result = original_write(path, data, abandoned=abandoned)
            if first:
                landed.set()
            return result

        storage.write_json_atomic = held_write
        original_persist = plugin._persist_config
        cancelled = {"done": False}

        async def cancelling_persist() -> bool:
            if not cancelled["done"]:
                cancelled["done"] = True
                task = asyncio.ensure_future(original_persist())
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                raise await task
            return await original_persist()

        plugin._persist_config = cancelling_persist
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"cooldown_sec": 777}
        try:
            try:
                await webapi_module(main)._api_post_config(plugin)
            except asyncio.CancelledError:
                pass
            # 被取消的那次写盘必须真的落地再断言：裸 await 的旧实现里线程仍在跑，
            # 断言会在它 os.replace 之前通过，变异就守不住了。
            await until(lambda: landed.is_set())
        finally:
            plugin._persist_config = original_persist
            storage.write_json_atomic = original_write

        disk = json.loads(plugin._config_path.read_text(encoding="utf-8"))
        memory = plugin.settings.to_config_dict()
        assert disk == memory, (
            f"磁盘与内存不一致：磁盘 cooldown_sec={disk.get('cooldown_sec')} "
            f"内存={memory.get('cooldown_sec')}——被驳回的配置落到了磁盘上"
        )
        assert memory["cooldown_sec"] != 777, "回滚没把内存恢复成旧值"

    with_plugin(tmp_path, scenario)


def test_rollback_completes_before_the_config_lock_is_released(tmp_path) -> None:
    """回滚必须跑完才能放锁，否则它会覆写一次已成功的配置保存。

    缺陷形态：``_apply_config_updates`` 的取消分支把 ``_restore_plugin_state``
    ``ensure_future`` 成游离任务，只经 ``shield`` 等一次。第二次取消让
    ``await asyncio.shield(restore)`` 立刻抛错、``pass`` 后 ``raise``——
    ``_api_post_config`` 的 ``async with _config_lock`` 随之释放锁，而回滚任务
    仍在跑且**不持锁**。此时一次新的 POST /config（或 /selfreply add|remove，同一把
    锁）能拿到锁并完整落盘；回滚任务随后用它开服时取的旧快照再写一次盘，把那
    次**成功**的保存静默撤销——重启后丢失，且全程无告警。

    变异锚定：把回滚的 shield 等待改回「等一次就放弃」，本用例红。
    """

    async def scenario(plugin, main):
        webapi = webapi_module(main)
        # 构造期的启动落盘持 _config_lock 且是懒跑的：不等它放锁就发 POST-A，
        # 请求会堵在锁上被取消，根本进不到 _apply_config_updates，回滚从未发生。
        await until(lambda: not plugin._config_lock.locked(), timeout=10)
        original_cooldown = plugin.settings.cooldown_sec

        persist_entered = asyncio.Event()
        rollback_entered = asyncio.Event()
        original_persist = plugin._persist_config
        real_restore = webapi._restore_plugin_state
        calls = {"persist": 0}

        async def slow_persist() -> bool:
            calls["persist"] += 1
            if calls["persist"] == 1:
                # 只拖慢 POST-A 自己那次：取消必须落在 _apply_config_updates 内部
                persist_entered.set()
                await asyncio.sleep(0.3)
            return await original_persist()

        async def slow_restore(*args: Any, **kwargs: Any) -> None:
            rollback_entered.set()
            await asyncio.sleep(0.5)
            await real_restore(*args, **kwargs)

        plugin._persist_config = slow_persist
        webapi._restore_plugin_state = slow_restore
        web = sys.modules["astrbot.api.web"]

        try:
            web.request.payload = {"cooldown_sec": 777}
            task_a = asyncio.ensure_future(webapi._api_post_config(plugin))
            await until(lambda: persist_entered.is_set(), timeout=10)
            # 第一次取消：进入回滚分支，回滚随即进入 slow_restore 的慢窗口
            task_a.cancel()
            await until(lambda: rollback_entered.is_set(), timeout=10)

            # 第二次取消：旧实现在此放弃等待回滚、放掉 _config_lock
            task_a.cancel()
            await asyncio.sleep(0.05)
            assert plugin._config_lock.locked(), (
                "回滚还在跑而 _config_lock 已释放：后来的写操作会与它交叠，"
                "回滚随后用旧快照覆写那次已成功的保存"
            )
        finally:
            plugin._persist_config = original_persist
            webapi._restore_plugin_state = real_restore

        # 等回滚自行跑完（慢窗口 0.5s，有界）：它必须写完旧值
        try:
            await asyncio.wait_for(asyncio.shield(task_a), timeout=20)
        except (asyncio.CancelledError, TimeoutError):
            pass
        # task_a 收尾时回滚已经写完（取消分支等回滚跑完才重放），无需再轮询
        # asyncio.all_tasks()——插件常驻的图片清理任务让那个条件永远为假。

        disk = json.loads(plugin._config_path.read_text(encoding="utf-8"))
        memory = plugin.settings.to_config_dict()
        assert disk == memory, (
            f"磁盘与内存不一致：磁盘 cooldown_sec={disk.get('cooldown_sec')} "
            f"内存={memory.get('cooldown_sec')}"
        )
        assert memory["cooldown_sec"] == original_cooldown, "回滚没把内存恢复成旧值"

        # 之后一次成功的保存不得再被那个回滚改动
        web.request.payload = {"cooldown_sec": 555}
        result = await asyncio.wait_for(webapi._api_post_config(plugin), timeout=20)
        assert result.get("ok") is True, result
        disk = json.loads(plugin._config_path.read_text(encoding="utf-8"))
        memory = plugin.settings.to_config_dict()
        assert disk == memory and memory["cooldown_sec"] == 555, (
            f"成功保存被回滚覆写：磁盘={disk.get('cooldown_sec')} 内存={memory.get('cooldown_sec')}"
        )

    with_plugin(tmp_path, scenario)


def test_api_post_config_huge_int_reports_field_not_body(tmp_path) -> None:
    """超长整数字面量：必须回字段级文案，不得落进最外层兜底说成「请求体不是合法 JSON」。

    json.loads 对几百位整数产出任意精度 int，``float()`` 抛 OverflowError。
    它是 ArithmeticError 而非 ValueError，会逃到 ``_api_post_config`` 最外层
    ``except Exception``：用户看到一句与字段无关的兜底文案，而前端按「错误文案
    以键名 + 空格前缀」定位字段的规则随之失效（契约 §7.1 要字段级文案）。

    变异锚定：去掉 ``_strict_float`` 里的 ``except OverflowError``，本用例红。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"decision_temperature": 10**400}

        result = await webapi_module(main)._api_post_config(plugin)

        assert result["ok"] is False
        assert result["error"] == "decision_temperature 必须是有限数字", result
        # 配置未被改动：400 不得留下半应用态
        assert plugin.settings.decision_temperature != 10**400

    with_plugin(tmp_path, scenario)


def test_api_post_config_nel_wrapped_item_is_not_reported_adjusted(tmp_path) -> None:
    """U+0085 包裹的集合条目：页面放行、后端原样保留，不得报「已规范化」。

    缺陷形态：``_config_update_was_adjusted`` 用裸 ``str.strip()`` 判「是否被
    trim 过」，而条目规范化用的是 ``js_trim``（设置页 ``String.prototype.trim``
    的那一套，刻意不剥 NEL）。分歧在这一处的方向是误报：值一个字节都没改，
    却弹出「配置已保存，已规范化：主动回复白名单会话」（契约 §7 要求
    adjusted_fields 只列**实际被调整的**键）。

    变异锚定：把 ``js_trim(item)`` 换回 ``item.strip()``，本用例红。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        wrapped = "\x85fake:group:123\x85"
        web.request.payload = {"whitelist_sessions": [wrapped]}

        result = await webapi_module(main)._api_post_config(plugin)

        assert result["ok"] is True, result.get("error")
        assert "whitelist_sessions" not in result["adjusted_fields"], (
            f"NEL 包裹的条目未被改写却报已规范化：{result['adjusted_fields']}"
        )
        assert wrapped in result["config"]["whitelist_sessions"], "NEL 包裹的条目本该原样保留"

    with_plugin(tmp_path, scenario)


def test_api_post_config_returns_runtime_enabled_after_toggle(tmp_path) -> None:
    """POST 关闭总开关后，响应里的 runtime_enabled 必须已是新运行态。"""

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        # 持久值与运行态都先置为启用，POST 才能命中「enabled 真正变化」分支。
        plugin.settings.enabled = True
        plugin.runtime_enabled = True
        web.request.payload = {"enabled": False}

        result = await webapi_module(main)._api_post_config(plugin)

        # 断言带 error 上下文：落盘失败时 _api_post_config 只回通用文案，
        # 不带出来就只剩一句 "assert False is True"，看不出是哪条路径失败的。
        assert result["ok"] is True, result.get("error")
        assert result["runtime_enabled"] is False
        assert plugin.runtime_enabled is False

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
        view_keys = {"ok", "runtime_enabled", "config_revision", "decision_prompt_default"}
        assert set(result) == panel_keys | view_keys
        assert "patrol_inactive_after_sec" not in result
        assert result["ok"] is True
        assert result["runtime_enabled"] is plugin.runtime_enabled
        assert result["decision_prompt_default"] == models.DEFAULT_DECISION_PROMPT_TEMPLATE

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
        plugin.settings.vision_main_enabled = True
        parser_before = plugin._vision.get_image_parser()
        assert parser_before is not None
        web.request.payload = payload
        result = await webapi_module(main)._api_post_config(plugin)
        assert result["ok"] is True
        for key, value in payload.items():
            assert getattr(plugin.settings, key) == value
        assert plugin._vision.get_image_parser() is not parser_before

    with_plugin(tmp_path, scenario)


def test_rollback_reschedules_check_scheduled_inside_the_window(tmp_path) -> None:
    """apply 窗口内新排的延迟检查，回滚后必须被重排而不是静默丢掉。

    缺陷形态：快照只记 ``delay_umos``（快照**时刻**的集合）。消息若落在 apply
    窗口内（``await _persist_config()`` / ``_save_storage()``）会排出一条**新的**
    延迟检查；保存随后失败，禁用路径把它取消，而回滚只重排快照集合——那条检查
    既不复活也不重排，其代次又被 ``SessionGate.restore`` 退回，最终在
    ``_wait_initial_delay_and_validate`` 静默出局：无日志、无裁决，只能等该会话
    下一条消息或巡检轮次才恢复。

    变异锚定：去掉 ``_apply_config_updates`` 里 ``window_delay_umos`` 的采集或
    ``extra_delay_umos`` 的传递，本用例红。
    """

    async def scenario(plugin, main):
        # 1) 先让延迟表空着：快照的 delay_umos 因此不含本会话
        await plugin.on_message(make_event(umo=UMO, message_str="先来一条"))
        await until(lambda: not plugin._delay_tasks, timeout=10)
        decisions_before = dict(plugin._last_decisions)

        # 2) 让应用在拓扑切换**之后**失败：禁用路径已经取消过延迟检查
        original_stop = plugin._scheduler.stop_patrol

        async def failing_stop():
            await original_stop()
            raise OSError("stop patrol failed")

        plugin._scheduler.stop_patrol = failing_stop

        # 3) 在 apply 窗口内投递一条消息 → 排出一条快照里没有的延迟检查
        original_save = plugin._save_storage
        delivered = asyncio.Event()

        async def window_save():
            if not delivered.is_set():
                await plugin.on_message(make_event(umo=UMO, message_str="窗口内的消息"))
                delivered.set()
            await original_save()

        plugin._save_storage = window_save

        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"enabled": False}
        try:
            result = await webapi_module(main)._api_post_config(plugin)
        finally:
            plugin._save_storage = original_save
            plugin._scheduler.stop_patrol = original_stop

        assert result.get("ok") is False, "场景没走到回滚路径"
        assert delivered.is_set(), "窗口内的消息没投递出去"
        assert plugin.runtime_enabled is True, "回滚没把运行态恢复成启用"

        # 4) 那条检查必须仍在（被回滚重排），并最终产出一次新裁决
        await until(lambda: not plugin._delay_tasks, timeout=10)
        new_decision = plugin._last_decisions.get(UMO)
        assert new_decision is not None and new_decision != decisions_before.get(UMO), (
            "窗口内消息的延迟检查被回滚静默丢掉：没有新裁决、也没有日志"
        )

    with_plugin(tmp_path, scenario, message_delay_sec=0)


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
        result = await webapi_module(main)._api_post_config(plugin)
        assert result.get("ok") is False
        # umo2 不在 sessions：回滚不为其重建延迟检查（原任务已被移除且无新任务）
        assert umo2 not in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def test_rollback_reschedule_failure_is_logged(tmp_path) -> None:
    """回滚重调度抛异常时只记录 debug 日志，不中断回滚。

    变异锚定：本用例同时钉住「回滚的后续步骤真的被执行」只断言
    ``ok is False`` 是假绿（重调度异常被 re-raise 中断回滚时，外层同样返回
    False），而 ``_restore_plugin_state`` 里 ``clear_parsers`` 之后的
    ``ensure_patrol`` / ``ensure_image_cleanup`` 全部被跳过。故这里加 spy，
    断言回滚收尾的三个步骤各发生一次。
    """

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

        # spy：回滚的收尾步骤必须仍然跑到（重调度异常只记 debug，不中断回滚）
        calls = {"clear_parsers": 0, "ensure_patrol": 0, "ensure_image_cleanup": 0}
        real_clear_parsers = plugin._vision.clear_parsers
        real_ensure_patrol = plugin._scheduler.ensure_patrol
        real_ensure_image_cleanup = plugin._scheduler.ensure_image_cleanup

        def spy_clear_parsers() -> None:
            calls["clear_parsers"] += 1
            real_clear_parsers()

        def spy_ensure_patrol() -> None:
            calls["ensure_patrol"] += 1
            real_ensure_patrol()

        def spy_ensure_image_cleanup() -> None:
            calls["ensure_image_cleanup"] += 1
            real_ensure_image_cleanup()

        plugin._vision.clear_parsers = spy_clear_parsers
        plugin._scheduler.ensure_patrol = spy_ensure_patrol
        plugin._scheduler.ensure_image_cleanup = spy_ensure_image_cleanup
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"whitelist_sessions": []}
            result = await webapi_module(main)._api_post_config(plugin)
        finally:
            plugin._vision.clear_parsers = real_clear_parsers
            plugin._scheduler.ensure_patrol = real_ensure_patrol
            plugin._scheduler.ensure_image_cleanup = real_ensure_image_cleanup

        assert result.get("ok") is False
        assert calls["clear_parsers"] == 1, "回滚未丢弃 parser 缓存"
        assert calls["ensure_patrol"] == 1, "重调度异常后回滚被中断，巡检拓扑未恢复"
        assert calls["ensure_image_cleanup"] == 1, "重调度异常后回滚被中断，图片清理拓扑未恢复"

    with_plugin(tmp_path, scenario)


def test_cancelled_config_apply_rolls_back(tmp_path) -> None:
    """配置应用被取消时必须回滚，不得留下半应用态（白名单已清空 / 磁盘已改）。

    缺陷形态：``_apply_config_updates`` 只有 ``except Exception``，而
    ``CancelledError`` 继承 ``BaseException``，在 ``_persist_config`` 或
    ``_save_storage`` 处被取消时，此前步骤（settings.apply、
    whitelist.replace 的 prune）已经生效，回滚却不会执行：内存里白名单被清空、
    会话状态被回收，磁盘配置可能已写新值，重启后同样错。
    """

    async def scenario(plugin, main):
        umo = UMO
        plugin.settings.whitelist = {umo}
        plugin.settings.cooldown_sec = 111
        state = plugin._state_for(umo)
        state.daily_count = 3
        original_state = plugin.sessions[umo]

        # 只在配置应用路径上取消：其余调用（如 terminate 的最终落盘）恢复原实现，
        # 否则测试收尾会连带抛 CancelledError。
        real_save = plugin._save_storage
        calls = {"count": 0}

        async def cancelled_save():
            calls["count"] += 1
            if calls["count"] == 1:
                raise asyncio.CancelledError()
            await real_save()

        plugin._save_storage = cancelled_save
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"whitelist_sessions": [], "cooldown_sec": 909}
            with pytest.raises(asyncio.CancelledError):
                await webapi_module(main)._api_post_config(plugin)
        finally:
            plugin._save_storage = real_save

        # 取消必须触发同一套回滚
        assert plugin.settings.cooldown_sec == 111, "取消后配置未回滚"
        assert umo in plugin.settings.whitelist, "取消后白名单未回滚"
        assert umo in plugin.sessions, "取消后被 prune 的会话状态未复活"
        assert plugin.sessions[umo] is original_state, "取消回滚丢了对象身份"
        assert plugin.sessions[umo].daily_count == 3, "取消回滚丢了日配额"

    with_plugin(tmp_path, scenario)


def test_unknown_config_key_error_is_sanitized() -> None:
    """未知键名进错误文本前必须去掉控制字符。

    缺陷形态：键名是客户端可控输入，原样拼进「未知配置键: ...」后既进响应也进
    服务端日志。带换行的键名能伪造日志行（含 ANSI 转义），键多/键长时一行能写出
    数 KB 的假日志。

    变异锚定：去掉 shown 的字符过滤，本用例红。
    """
    webapi = _webapi()
    with pytest.raises(ValueError) as excinfo:
        webapi._parse_config_updates({"evil\n[astrbot_plugin] fake: enabled=False": 1})
    message = str(excinfo.value)
    assert "\n" not in message, f"未知键名把换行带进了错误文本：{message!r}"
    assert "[astrbot_plugin]" in message, "过滤后仍要保留可见键名，便于定位"
    assert "未知配置键" in message

    # JSON 允许 \ud800 这类孤立代理项直接充当键名：它编不了 UTF-8，留着会让严格的
    # 日志 sink 丢掉整条拒绝记录、宿主在 ensure_ascii=False 下序列化响应时抛异常。
    with pytest.raises(ValueError) as surrogate:
        webapi._parse_config_updates({"evil\ud800key": 1})
    assert surrogate.value.args[0].encode("utf-8")
