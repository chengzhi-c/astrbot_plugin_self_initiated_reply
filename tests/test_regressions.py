"""历史缺陷回归测试（红灯测试合并：round3 RL-4~6 + round4~8）。

按主题组织的历史回归守卫：
- 命令入口与生命周期（round3 RL-4~6）
- r1-r20 编号回归（round4/5/7/8）：工具策略、配置回滚、并发互斥、ABA 等
- 日志级别契约（round6）：高频成功路径必须保持 DEBUG
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import logging
import os
import sys
import types
from pathlib import Path

from .host_stubs import with_plugin
from .source_contract import calls_in, defines, logger_levels_for, method_source, source_of
from .test_main_runtime import UMO, _make_event, _PipelineTestAdapter

ROOT = Path(__file__).resolve().parents[1]


# ============================================================================
# round3 RL-4~6：命令入口与生命周期（0.7.0 审查缺陷）
# ============================================================================

PACKAGE_NAME_R3 = "selfreply_regressions_package"


def _install_astrbot_stubs() -> None:
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    event = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    star = sys.modules.setdefault("astrbot.api.star", types.ModuleType("astrbot.api.star"))
    components = sys.modules.setdefault(
        "astrbot.api.message_components", types.ModuleType("astrbot.api.message_components")
    )

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class At:
        pass

    if not hasattr(api, "logger"):
        api.logger = logging.getLogger("selfreply-round3")
    if not hasattr(event, "AstrMessageEvent"):
        event.AstrMessageEvent = AstrMessageEvent
    if not hasattr(star, "Context"):
        star.Context = Context
    if not hasattr(components, "At"):
        components.At = At
    astrbot.api = api


def _load_r3_modules():
    _install_astrbot_stubs()
    package = sys.modules.get(PACKAGE_NAME_R3)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME_R3)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE_NAME_R3] = package
    models = importlib.import_module(f"{PACKAGE_NAME_R3}.models")
    utils = importlib.import_module(f"{PACKAGE_NAME_R3}.utils")
    commands = importlib.import_module(f"{PACKAGE_NAME_R3}.commands")
    image = importlib.import_module(f"{PACKAGE_NAME_R3}.image")
    recorder = importlib.import_module(f"{PACKAGE_NAME_R3}.image.recorder_bridge")
    return models, utils, commands, image, recorder


# ============================================================================
# RL-4 任意用户可触发命令并吞掉事件（中危）
# ============================================================================


def test_help_action_is_reachable_without_admin() -> None:
    """确认 help 不在管理员动作集合内（用于说明上一条的影响面）。"""
    models, _, _, _, _ = _load_r3_modules()
    assert "help" not in models.ADMIN_COMMAND_ACTIONS


def test_bare_command_word_is_parsed_as_command() -> None:
    """记录当前解析行为：裸词即命令（说明缺陷来源，非断言修复）。"""
    _, _, commands, _, _ = _load_r3_modules()
    assert commands.parse_command_text("selfreply") == ("help", "")
    assert commands.parse_command_text("selfreply add") == ("add", "")


# RL-5（Web 配置读取失败返回 None）的守卫已迁至
# test_webapi_fixes.py::test_api_get_config_error_path —— 那里是真调 API 断言
# 载荷形状，比在 except 尾段里搜 "return" 更直接，也不会因重排 except 而误红。


# ============================================================================
# RL-6 会话代次表无界增长（低危）
# ============================================================================


def test_image_cache_cleanup_has_manual_api_and_startup_sweep(tmp_path: Path) -> None:
    """插件启动即回收过期缓存，并向宿主注册手动 POST 清理入口。"""
    models, _, _, _, _ = _load_r3_modules()
    cache_dir = tmp_path / "data" / models.PLUGIN_ID / "image_cache"
    cache_dir.mkdir(parents=True)
    expired = cache_dir / "expired.png"
    expired.write_bytes(b"old")
    os.utime(expired, (1, 1))

    async def scenario(plugin, main):
        assert not expired.exists()
        assert any(
            route.endswith("/image-cache/cleanup") and "POST" in methods
            for route, _handler, methods, _description in plugin.context.register_web_api_calls
        )

    with_plugin(
        tmp_path,
        scenario,
        vision_judge_enabled=False,
        vision_main_enabled=False,
        vision_image_age_sec=60,
    )


def test_plugin_logo_is_root_square_png() -> None:
    """AstrBot 从插件根目录的 logo.png 读取插件图标。"""
    logo = ROOT / "logo.png"
    data = logo.read_bytes()
    assert logo.is_file()
    assert data[:8] == bytes.fromhex("89504e470d0a1a0a")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    assert width == height
    assert width > 0


def test_successful_image_cache_logs_are_debug_only() -> None:
    """高频成功路径不应在 INFO 级别刷屏，失败日志仍保留原级别。"""
    captured = "[%s] captured %s/%s images into local vision cache for umo=%s"
    snapshot = "[%s] host image snapshot created: %s"

    assert logger_levels_for("image/vision_runtime.py", captured) == ["logger.debug"], (
        "图片入缓存成功日志必须是 debug：每条含图消息都会打，INFO 会刷屏"
    )
    assert logger_levels_for("image/parser.py", snapshot) == ["logger.debug"], (
        "宿主图片快照成功日志必须是 debug"
    )


def test_config_mutations_share_one_lock_and_settings_normalizer() -> None:
    """白名单和 Web 配置更新不能交错覆盖，配置必须经统一入口规范化。"""
    api = method_source("webapi.py", "_api_post_config")

    assert "async with plugin._config_lock" in api
    assert "_api_post_config_locked" in api
    assert defines("whitelist.py", "WhitelistManager.add")
    assert defines("whitelist.py", "WhitelistManager.remove")
    # 规范化入口：候选配置必须经 Settings.from_config 归一，不得直接落库
    assert "Settings.from_config(candidate)" in method_source("webapi.py", "_apply_config_updates")


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])

# ============================================================================
# round4 r1-r5：工具策略与配置回滚（0.7.19 审查）
# ============================================================================


def _load_vision_image():
    """复用 test_vision 的动态包加载模式。"""
    import tests.test_vision as vision

    root = Path(vision.ROOT)
    package = types.ModuleType(vision.PACKAGE_NAME)
    package.__path__ = [str(root)]
    sys.modules[vision.PACKAGE_NAME] = package
    image = importlib.import_module(f"{vision.PACKAGE_NAME}.image")
    return image


def _install_tool_injecting_pipeline(plugin, main, *, event):
    """构造 build 注入工具 + hook 注入工具的管线脚手架，返回控制器。"""
    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    class DirectSendingRunner:
        def __init__(self, target_event):
            self._target = target_event

        def reset(self, **_):
            return _FakeResetCoro()

        def get_final_llm_resp(self):
            return SimpleNamespace(completion_text="你好呀", result_chain=None)

        def close(self):
            pass

    req_holder = {}
    enforce_snapshots: list[list[str]] = []
    reset_snapshots: list[list[str]] = []
    prompts: list[str] = []

    async def build_effect(kwargs, result):
        req_holder["req"] = kwargs["req"]
        prompts.append(str(getattr(kwargs["req"], "prompt", "") or ""))
        tool_set = kwargs["req"].func_tool
        for name in ("send_message_to_user", "web_search", "mcp_anything"):
            tool_set.add_tool(SimpleNamespace(name=name))

        async def _reset():
            reset_snapshots.append(
                sorted(main._AGENT_RUNTIME.final_tool_ids(req_holder["req"]) or [])
            )

        return FakeBuildResult(
            agent_runner=DirectSendingRunner(event),
            provider_request=kwargs["req"],
            provider=None,
            reset_coro=_reset(),
        )

    def run_effect(_runner, **_kwargs):
        async def gen():
            yield None

        return gen()

    original_runtime = main._AGENT_RUNTIME
    main._AGENT_RUNTIME = _PipelineTestAdapter(
        original_runtime, build_effect=build_effect, run_effect=run_effect
    )
    original_enforce = plugin._generation.enforce_final_tool_policy

    def counting_enforce(req, inherit_tools):
        ok = original_enforce(req, inherit_tools)
        enforce_snapshots.append(sorted(main._AGENT_RUNTIME.final_tool_ids(req) or []))
        if len(enforce_snapshots) == 1:
            # 模拟 hook 在第一次 enforce 之后向 req 注入工具
            req.func_tool.add_tool(SimpleNamespace(name="hook_injected"))
        return ok

    plugin._generation.enforce_final_tool_policy = counting_enforce
    return {
        "req_holder": req_holder,
        "enforce_snapshots": enforce_snapshots,
        "reset_snapshots": reset_snapshots,
        "prompts": prompts,
        "restore": lambda: (
            setattr(plugin._generation, "enforce_final_tool_policy", original_enforce),
            setattr(main, "_AGENT_RUNTIME", original_runtime),
        ),
    }


async def _run_pipeline(plugin):
    state = plugin._state_for(UMO)
    token = plugin._gate.advance(UMO)
    return await plugin._generation.generate(UMO, state, expected_generation=token, force=True)


def test_r1_config_change_mid_run_does_not_flip_tool_policy(tmp_path: Path) -> None:
    """入口快照：运行中把开关改为 True 不得让本次运行 fail-open。"""

    async def scenario(plugin, main):
        from types import SimpleNamespace

        from .host_stubs import FakeBuildResult, _FakeResetCoro

        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        event.plugins_name = ["other_plugin"]

        class DirectSendingRunner:
            def __init__(self, target_event):
                self._target = target_event

            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        req_holder = {}
        enforce_snapshots: list[list[str]] = []

        async def build_effect(kwargs, result):
            req_holder["req"] = kwargs["req"]
            tool_set = kwargs["req"].func_tool
            for name in ("send_message_to_user", "web_search", "mcp_anything"):
                tool_set.add_tool(SimpleNamespace(name=name))
            return FakeBuildResult(
                agent_runner=DirectSendingRunner(event),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                # 模拟用户在一次主动运行中途保存配置开启继承
                plugin.settings.proactive_inherit_tools = True
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_enforce = plugin._generation.enforce_final_tool_policy

        def counting_enforce(req, inherit_tools):
            ok = original_enforce(req, inherit_tools)
            enforce_snapshots.append(sorted(main._AGENT_RUNTIME.final_tool_ids(req) or []))
            if len(enforce_snapshots) == 1:
                req.func_tool.add_tool(SimpleNamespace(name="hook_injected"))
            return ok

        plugin._generation.enforce_final_tool_policy = counting_enforce
        try:
            result = await _run_pipeline(plugin)
            assert result.text == "你好呀"
            # 快照为 False：即使运行中 settings 变为 True，enforce 仍按 False 清理
            assert enforce_snapshots == [[], []]
            assert main._AGENT_RUNTIME.final_tool_ids(req_holder["req"]) == []
        finally:
            plugin._generation.enforce_final_tool_policy = original_enforce
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


def test_r2_second_enforce_happens_before_reset(tmp_path: Path) -> None:
    """reset 执行时工具集必须已经清理：hook 注入的工具不能进 runner。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        event.plugins_name = ["other_plugin"]

        ctrl = _install_tool_injecting_pipeline(plugin, main, event=event)
        try:
            result = await _run_pipeline(plugin)
            assert result.text == "你好呀"
            assert ctrl["enforce_snapshots"] == [[], []]
            # reset 执行时工具集为空：第二次清理在 reset 之前完成
            assert ctrl["reset_snapshots"] == [[]]
        finally:
            ctrl["restore"]()

    with_plugin(tmp_path, scenario)


def test_r3_system_hint_matches_tool_policy(tmp_path: Path) -> None:
    """继承模式提示词描述真实边界；默认模式仍写死禁用工具。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0

        ctrl = _install_tool_injecting_pipeline(plugin, main, event=event)
        try:
            await _run_pipeline(plugin)
            assert len(ctrl["prompts"]) == 1
            default_prompt = ctrl["prompts"][0]
            assert "不得执行命令或 Python" in default_prompt
        finally:
            ctrl["restore"]()

    with_plugin(tmp_path, scenario)

    async def inherit_scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0

        ctrl = _install_tool_injecting_pipeline(plugin, main, event=event)
        try:
            await _run_pipeline(plugin)
            assert len(ctrl["prompts"]) == 1
            inherit_prompt = ctrl["prompts"][0]
            assert "继承宿主完整工具链" in inherit_prompt
            assert "不得执行命令或 Python" not in inherit_prompt
        finally:
            ctrl["restore"]()

    with_plugin(tmp_path / "inherit", inherit_scenario, proactive_inherit_tools=True)


def test_r4_cache_hit_does_not_rewrite_file(tmp_path: Path) -> None:
    """内容寻址命中且未篡改时不得重写文件（digest 比较修复）。"""

    image = _load_vision_image()
    parser = image.ImageParser(object(), source_cache_dir=tmp_path / "image_cache")
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    encoded = base64.b64encode(payload).decode()
    data_url = "data:image/png;base64," + encoded
    digest = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "image_cache" / digest[:2] / f"{digest}.png"

    writes: list[bytes] = []
    original_write = Path.write_bytes

    def counting_write_bytes(self, data):
        writes.append(bytes(data))
        return original_write(self, data)

    Path.write_bytes = counting_write_bytes
    try:
        assert parser._materialize_data_url(data_url) is not None
        assert parser._materialize_data_url(data_url) is not None
        assert len(writes) == 1, f"命中缓存不得重写，实际写入 {len(writes)} 次"
        assert target.read_bytes() == payload
    finally:
        Path.write_bytes = original_write


def test_r5_config_rollback_restores_sessions_and_locks(tmp_path: Path) -> None:
    """回滚必须恢复 sessions 与 _session_locks（与 settings 同级）。"""

    async def scenario(plugin, main):
        import sys

        umo = UMO
        plugin.sessions[umo] = plugin._state_for(umo)
        plugin._gate.lock_for(umo)
        plugin.settings.whitelist = {umo}

        async def boom():
            raise OSError("disk full")

        plugin._save_storage = boom
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist_sessions": []}
        # API 层不抛异常：内部回滚后返回 ok:False
        result = await plugin._api_post_config()
        assert result.get("ok") is False
        assert umo in plugin.sessions
        assert umo in plugin._session_locks

    with_plugin(tmp_path, scenario)


# ============================================================================
# round5 r6-r12：工具策略/UNKNOWN/并发（0.7.20 审查）
# ============================================================================


def _load_vision_main():
    import tests.test_vision as vision

    root = Path(vision.ROOT)
    package = types.ModuleType(vision.PACKAGE_NAME)
    package.__path__ = [str(root)]
    sys.modules[vision.PACKAGE_NAME] = package
    return importlib.import_module(f"{vision.PACKAGE_NAME}.main")


# ============================================================================
# R6：继承模式危险工具 denylist
# ============================================================================


def test_r6_inherit_mode_denylists_host_dangerous_tools(tmp_path: Path) -> None:
    """继承模式放行普通工具，但宿主级危险工具（含 hook 注入）一律拒绝。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0

        class DirectSendingRunner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        enforce_snapshots: list[list[str]] = []

        async def build_effect(kwargs, result):
            tool_set = kwargs["req"].func_tool
            # 普通插件工具 + 宿主级危险工具（cron，4.23.3 实测名 future_task）
            for name in ("send_image", "future_task"):
                tool_set.add_tool(SimpleNamespace(name=name))
            return FakeBuildResult(
                agent_runner=DirectSendingRunner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_enforce = plugin._generation.enforce_final_tool_policy

        def counting_enforce(req, inherit_tools):
            ok = original_enforce(req, inherit_tools)
            enforce_snapshots.append(sorted(main._AGENT_RUNTIME.final_tool_ids(req) or []))
            if len(enforce_snapshots) == 1:
                # 模拟 hook 在第一次 enforce 后注入危险工具（kb agentic）与普通工具
                req.func_tool.add_tool(SimpleNamespace(name="astr_kb_search"))
                req.func_tool.add_tool(SimpleNamespace(name="third_party_weather"))
            return ok

        plugin._generation.enforce_final_tool_policy = counting_enforce
        try:
            result = await plugin._generation.generate(
                UMO, plugin._state_for(UMO), expected_generation=1, force=True
            )
            assert result.text == "你好呀"
            # 修复前：继承分支直接 return True → 危险工具残留（红灯）
            assert enforce_snapshots[0] == ["send_image"]
            assert enforce_snapshots[1] == ["send_image", "third_party_weather"]
        finally:
            plugin._generation.enforce_final_tool_policy = original_enforce
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario, proactive_inherit_tools=True)


# ============================================================================
# R7：UNKNOWN 发送 + 工具直发时仍记录状态
# ============================================================================


def test_r7_unknown_send_records_state_even_with_direct_sends(tmp_path: Path) -> None:
    """工具已直发后最终文本提交 UNKNOWN：状态必须记录，观察窗口必须推进。"""

    from types import SimpleNamespace

    from .host_stubs import _FakeMessageChain

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        class Runner:
            def __init__(self, target):
                self._target = target

            def reset(self, **_):
                from .host_stubs import _FakeResetCoro

                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        async def build_effect(kwargs, result):
            from .host_stubs import FakeBuildResult

            kwargs["req"].func_tool.add_tool(SimpleNamespace(name="send_image"))
            return FakeBuildResult(
                agent_runner=Runner(event),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=Runner(event).reset(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                # 模拟工具直发（tool_direct_result）
                await _runner._target.send(
                    _FakeMessageChain(type="tool_direct_result", chain=["图"])
                )
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_send_reply = plugin._delivery.send_reply

        async def unknown_send_reply(umo, reply, expected_generation=None):
            models = importlib.import_module(f"{main.__package__}.models")
            return models.SendOutcome(models.SendStatus.UNKNOWN, "adapter raised after submit")

        plugin._delivery.send_reply = unknown_send_reply
        try:
            result = await plugin._pipeline.check_session(UMO, trigger="patrol", force=True)
            assert "未自动重试" in result
            # 修复前：direct_send_count>0 时跳过记录 → daily_count 不增（红灯）
            assert state.daily_count >= 1
            assert state.last_proactive_observed_at >= state.last_active_at
            assert state.last_proactive_at >= state.last_active_at
        finally:
            plugin._delivery.send_reply = original_send_reply
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# R8：配置回滚后延迟检查重新调度
# ============================================================================


def test_r8_rollback_reschedules_delayed_check(tmp_path: Path) -> None:
    """回滚恢复会话后，被白名单变更取消的延迟检查必须重新调度。"""

    import sys

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._state_for(UMO)
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        old_task = plugin._delay_tasks.get(UMO)
        assert old_task is not None and not old_task.cancelled()

        async def boom():
            raise OSError("disk full")

        plugin._save_storage = boom
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist_sessions": []}
        result = await plugin._api_post_config()
        assert result.get("ok") is False
        # 修复前：回滚不恢复延迟任务 → UMO 不在 _delay_tasks（红灯）
        new_task = plugin._delay_tasks.get(UMO)
        assert new_task is not None
        assert new_task is not old_task
        assert not new_task.cancelled()

    with_plugin(tmp_path, scenario)


# ============================================================================
# R9：生成超时先优雅停止
# ============================================================================


def test_r9_timeout_requests_graceful_stop(tmp_path: Path) -> None:
    """超时时先调 request_stop 让 run_agent 走正常清理，而不是硬取消。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    stop_called: list[bool] = []

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0

        class HangingRunner:
            def reset(self, **_):
                return _FakeResetCoro()

            def request_stop(self):
                stop_called.append(True)

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="", result_chain=None)

            def close(self):
                pass

        async def build_effect(kwargs, result):
            return FakeBuildResult(
                agent_runner=HangingRunner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                await asyncio.sleep(3600)  # 永不结束
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_grace = main.GRACEFUL_STOP_GRACE_SEC
        main.GRACEFUL_STOP_GRACE_SEC = 0.05
        try:
            result = await plugin._generation.generate(
                UMO, plugin._state_for(UMO), expected_generation=1, force=True
            )
            # 修复前：wait_for 直接取消 run_agent → request_stop 从未被调（红灯）
            assert stop_called == [True]
            assert result.text == ""
        finally:
            main.GRACEFUL_STOP_GRACE_SEC = original_grace
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario, generation_timeout_sec=0.05)


# ============================================================================
# R10：GET enabled 返回持久配置
# ============================================================================


def test_r10_get_config_enabled_is_persisted_value(tmp_path: Path) -> None:
    """GET config 的 enabled 必须是持久值，runtime_enabled 单独暴露。

    0.9.4 决策 5 后 ``/off`` 会同时落盘 ``enabled``，故此处不再用 ``/off`` 举例，
    改为直接构造「两者分叉」这个状态：webapi 仍须分开暴露，否则前端全量保存会把
    运行态固化成持久配置。分叉现在由 POST config 提交相同 enabled 值时产生。
    """

    async def scenario(plugin, main):
        plugin.runtime_enabled = False  # 直接构造分叉态（不再等同于 /off）
        cfg = await plugin._api_get_config()
        # 修复前：enabled 返回 runtime_enabled=False → 前端全量保存会固化关闭（红灯）
        assert cfg["enabled"] is plugin.settings.enabled
        assert cfg["enabled"] is True
        assert cfg["runtime_enabled"] is False

    with_plugin(tmp_path, scenario)


# ============================================================================
# R11：同会话并发互斥
# ============================================================================


def test_r11_concurrent_checks_are_mutexed(tmp_path: Path) -> None:
    """同一会话并发两个 _check_session：第二个必须被拒，配额只计一次。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        class Runner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        entered = asyncio.Event()

        async def build_effect(kwargs, result):
            entered.set()
            await asyncio.sleep(0.1)  # 第一个占住 _running_sessions，让第二个进入
            return FakeBuildResult(
                agent_runner=Runner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        try:
            results = await asyncio.gather(
                plugin._pipeline.check_session(UMO, trigger="patrol", force=True),
                plugin._pipeline.check_session(UMO, trigger="patrol", force=True),
            )
            rejected = [r for r in results if "已有判断任务在运行" in r]
            accepted = [r for r in results if "已有判断任务在运行" not in r]
            assert len(rejected) == 1, f"expected one rejection, got {results}"
            assert len(accepted) == 1
            # 只有一次实际执行：配额只计一次（修复前假并发测试掩盖此语义）
            assert state.daily_count == 1
        finally:
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# R12：非 force 检查的白名单闸门
# ============================================================================


def test_r12_non_force_check_rejected_for_non_whitelisted_session(tmp_path: Path) -> None:
    """非白名单会话的非 force 检查必须被闸门拒绝，不进入决策管线。"""

    async def scenario(plugin, main):
        plugin._last_events[UMO] = _make_event()
        plugin._last_event_at[UMO] = 1.0
        plugin.settings.whitelist = set()
        result = await plugin._pipeline.check_session(UMO, trigger="patrol", force=False)
        assert result == "会话不在主动回复白名单。"

    with_plugin(tmp_path, scenario)


# ============================================================================
# round6：高频成功路径日志级别契约
# ============================================================================


# 高频成功路径的日志模板 → 期望级别。每条消息在正常运行时按会话/按消息触发，
# 升到 INFO 会刷屏（0.7.x 实测），故契约是「必须 debug」。列表长度即调用点个数：
# proactive reply sent 有两处（log_reply_content 的 if/else 双分支）。
_DEBUG_LOG_CONTRACTS = [
    ("scheduler.py", "[%s] wait for minimum silence session=", 1),
    ("session_pipeline.py", "[%s] skip session=%s trigger=", 1),
    # `[%s] decision session=` 自 0.9.5 起移出本契约、升为 INFO（用户要求）。
    # 它不违反本契约的初衷：初衷是拦「逐条消息级」的刷屏，而这一行与
    # scheduler.py 那条已是 INFO 的 `check result session=` 在常见路径上 1:1
    # 同频（都在一次 check_session 收敛点各打一次），不引入新的刷屏量级。
    # 现由 tests/test_observability.py 的 _INFO_WHITELIST 看守。
    ("delivery.py", "[%s] skip before send session=", 1),
    ("delivery.py", "[%s] proactive reply sent session=", 2),
    ("delivery.py", "[%s] event send completed session=", 1),
    ("image/parser.py", "image frozen to local cache: %s", 1),
    ("image/parser.py", "image frozen as in-memory data URL", 1),
]


def test_high_frequency_success_logs_stay_debug() -> None:
    """7 处高频成功路径必须保持 DEBUG，且调用点个数不变。

    个数一起断言，是因为只查级别时模板整体消失会静默通过——那正是日志退化的
    常见形态。
    """
    problems: list[str] = []
    for rel, template, expected_sites in _DEBUG_LOG_CONTRACTS:
        levels = logger_levels_for(rel, template)
        if len(levels) != expected_sites:
            problems.append(
                f"{rel}: {template!r} 有 {len(levels)} 个调用点，期望 {expected_sites}"
                "（模板被删除或新增了调用点）"
            )
        elif set(levels) != {"logger.debug"}:
            problems.append(f"{rel}: {template!r} 级别为 {levels}，必须全部 logger.debug")
    assert not problems, "高频成功路径日志级别契约破坏：\n" + "\n".join(problems)


# ============================================================================
# round7 r13-r17：权限与配置键（v0.8.4 前站）
# ============================================================================


def test_r13_non_admin_write_command_does_not_cancel(tmp_path: Path) -> None:
    """非管理员发写指令：权限拒绝先行，在途延迟检查不得被取消。

    修复前 on_message 命令分支无条件 _cancel_event_session（白名单会话）
    → 任务被取消（红灯）。
    """

    async def scenario(plugin, main):
        event = _make_event(message_str="/selfreply add")
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin.settings.whitelist = {UMO}
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        task = plugin._delay_tasks.get(UMO)
        assert task is not None and not task.done()

        # FakeEvent.is_admin() 恒 False：非管理员
        await plugin.on_message(event)
        assert not task.cancelled(), "非管理员写指令不应取消在途回复"
        assert plugin._delay_tasks.get(UMO) is task

    with_plugin(tmp_path, scenario)


# ============================================================================
# R14：管理员写指令取消、只读指令不取消（MP1-3 语义）
# ============================================================================


def test_r14_admin_write_cancels_but_read_does_not(tmp_path: Path) -> None:
    """管理员视角：只读（status）不打断进行中的检查，写（add）才取消。

    修复前只读指令同样被无条件取消 → status 后任务被取消（红灯）。
    """

    async def scenario(plugin, main):
        plugin.settings.whitelist = {UMO}
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        task = plugin._delay_tasks.get(UMO)
        assert task is not None and not task.done()

        # 只读指令不打断
        read_event = _make_event(message_str="/selfreply status")
        read_event.role = "admin"
        plugin._last_events[UMO] = read_event
        plugin._last_event_at[UMO] = 1.0
        await plugin.on_message(read_event)
        # 用任务表引用断言：cancelling 状态时 cancelled() 尚为 False，
        # 引用断言才能区分"未取消"与"正在取消"。
        assert plugin._delay_tasks.get(UMO) is task, "只读指令不应取消在途回复"

        # 写指令取消在途回复
        write_event = _make_event(message_str="/selfreply add")
        write_event.role = "admin"
        plugin._last_events[UMO] = write_event
        plugin._last_event_at[UMO] = 1.0
        await plugin.on_message(write_event)
        assert plugin._delay_tasks.get(UMO) is not task, "管理员写指令应取消在途回复"

    with_plugin(tmp_path, scenario)


# ============================================================================
# R15：webapi 新键真实解析（MP1-4 吞字段）
# ============================================================================


def test_r15_new_config_keys_take_effect(tmp_path: Path) -> None:
    """POST 13 个规范键 + decision_history_min_messages 必须真实写入 settings。

    修复前这些键无处理分支 → ok:true 但 settings 不变（虚假绿灯，红灯）。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {
            "recent_message_limit": 30,
            "reply_length_mode": "short",
            "allow_multiline_reply": False,
            "max_reply_chars": 300,
            "log_reply_content": True,
            "bot_aliases": ["小c", "阿c"],
            "ignored_sender_ids": ["u9"],
            "check_interval_sec": 600,
            "max_daily_replies_per_session": 7,
            "quiet_hours": ["22:00-23:00"],
            "enabled_message_trigger": False,
            "enabled_patrol_trigger": True,
            "generation_timeout_sec": 90,
            "decision_history_min_messages": 8,
        }
        result = await plugin._api_post_config()
        assert result.get("ok") is True, result
        s = plugin.settings
        assert s.recent_message_limit == 30
        assert s.reply_length_mode == "short"
        assert s.allow_multiline_reply is False
        assert s.max_reply_chars == 300
        assert s.log_reply_content is True
        assert s.bot_aliases == ["小c", "阿c"]
        assert s.ignored_sender_ids == {"u9"}
        assert s.check_interval_sec == 600
        assert s.max_daily_replies_per_session == 7
        assert s.quiet_hours == ["22:00-23:00"]
        assert s.enabled_message_trigger is False
        assert s.enabled_patrol_trigger is True
        assert s.generation_timeout_sec == 90
        assert s.decision_history_min_messages == 8

    with_plugin(tmp_path, scenario)


# ============================================================================
# R16：webapi 未知键 fail loud（MP1-4）
# ============================================================================


def test_r16_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    """schema 之外的键必须被拒并列出未知键，而不是静默返回 ok:true。

    修复前未知键被忽略 → ok:true（虚假成功，红灯）。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"bogus_setting": 1}
        result = await plugin._api_post_config()
        assert result.get("ok") is False
        assert "bogus_setting" in str(result.get("error", ""))

    with_plugin(tmp_path, scenario)


# ============================================================================
# round8 r18-r20：会话状态显式化（ticket 07）
# ============================================================================


def test_r18_aba_old_task_does_not_revive_after_re_add(tmp_path: Path) -> None:
    """会话移除后立即重加：运行中的旧任务必须被代次门拦截，不发送不记录。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._gate.advance(UMO)  # 真实会话：新消息已推进过代次
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        class Runner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        entered = asyncio.Event()

        async def build_effect(kwargs, result):
            entered.set()
            await asyncio.sleep(0.2)  # 旧任务在 build 中挂起，期间发生 ABA
            return FakeBuildResult(
                agent_runner=Runner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        try:
            task = asyncio.create_task(
                plugin._pipeline.check_session(UMO, trigger="patrol", force=True)
            )
            await entered.wait()
            # 会话运行中：白名单移除（级联失效+prune）→ 立即重加（新代次）
            await plugin._remove_whitelist_session(UMO)
            assert UMO not in plugin._last_events
            await plugin._add_whitelist_session(UMO)

            result = await task

            # 旧任务被代次门拦截：不发送、不记录任何状态
            assert "会话已经更新" in result or "放弃旧任务" in result, result
            assert state.last_proactive_at == 0.0
            assert state.daily_count == 0
        finally:
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# R19：main 不得散落事件表清理（失效级联单点）
# ============================================================================


def test_r19_no_scattered_event_table_mutation_in_main() -> None:
    """事件/时间/图片三表的清理必须由 SessionCoordinator 单点拥有。"""
    main_source = source_of("main.py")

    for frag in [
        "_last_events.pop",
        "_last_event_at.pop",
        "_recent_image_events.pop",
        "_last_events.clear",
        "_last_event_at.clear",
        "_recent_image_events.clear",
    ]:
        assert frag not in main_source, f"main 不应散落清理 {frag}（收敛到 SessionCoordinator）"

    # 级联单点存在：invalidate 必须推进代次 + 取消延迟 + 清三表
    invalidate_calls = calls_in("session_coordinator.py", "SessionCoordinator.invalidate")
    for callee in ("self._gate.advance", "self._cancel_delay", "self.clear"):
        assert callee in invalidate_calls, f"invalidate 未级联 {callee}"

    clear = method_source("session_coordinator.py", "SessionCoordinator.clear")
    for frag in ["_events.pop", "_event_at.pop", "_images.pop"]:
        assert frag in clear, f"clear 必须级联清理 {frag}"


# ============================================================================
# R20：会话失效清空观察素材
# ============================================================================


def test_r20_invalidate_clears_observation_material(tmp_path: Path) -> None:
    """记录事件后会话持有观察素材；invalidate 必须清空事件表并推进代次。

    「持有观察素材」以事件表为准（_last_events/_last_event_at），不经任何
    阶段投影——残留一条就足以让下一轮决策拿到已失效会话的旧消息。
    """

    async def scenario(plugin, main):
        event = _make_event()
        plugin._coordinator.record_event(UMO, event, 1.0)
        assert UMO in plugin._last_events

        before = plugin._gate.current(UMO)
        plugin._coordinator.invalidate(UMO)

        assert UMO not in plugin._last_events
        assert UMO not in plugin._last_event_at
        assert plugin._gate.current(UMO) > before

    with_plugin(tmp_path, scenario)
