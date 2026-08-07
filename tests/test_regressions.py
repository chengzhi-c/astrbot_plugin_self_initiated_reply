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
import inspect
import logging
import sys
import types
from pathlib import Path

from .host_stubs import with_plugin
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


def _main_source() -> str:
    return (ROOT / "main.py").read_text(encoding="utf-8")


# ============================================================================
# RL-4 任意用户可触发命令并吞掉事件（中危）
# ============================================================================


def test_inline_command_requires_slash_or_mention() -> None:
    """裸词 selfreply 不应在任意会话里被当成命令并 stop_event。"""
    source = _main_source()
    start = source.index("    async def on_message(")
    end = source.index("\n    def _should_ignore_event(", start)
    handler = source[start:end]

    gated = (
        'startswith("/")' in handler
        or "is_at_or_wake_command_event" in handler
        or "_looks_like_command_entry" in handler
    )

    assert gated, (
        "on_message 未校验命令前缀：任意群成员发送裸词 selfreply 就能让 Bot "
        "回复整段帮助文本并 stop_event()，从而吞掉该消息、阻断其他插件"
    )


def test_help_action_is_reachable_without_admin() -> None:
    """确认 help 不在管理员动作集合内（用于说明上一条的影响面）。"""
    models, _, _, _, _ = _load_r3_modules()
    source = inspect.getsource(models)
    line = next(item for item in source.splitlines() if item.startswith("ADMIN_COMMAND_ACTIONS"))
    assert '"help"' not in line, "help 不受管理员限制，配合缺失的前缀校验构成无门槛触发面"


def test_bare_command_word_is_parsed_as_command() -> None:
    """记录当前解析行为：裸词即命令（说明缺陷来源，非断言修复）。"""
    _, _, commands, _, _ = _load_r3_modules()
    assert commands.parse_command_text("selfreply") == ("help", "")
    assert commands.parse_command_text("selfreply add") == ("add", "")


# ============================================================================
# RL-5 Web 配置读取失败返回 None（低危）
# ============================================================================


def test_api_get_config_returns_payload_on_failure() -> None:
    """Quart 路由不能返回 None，否则失败时抛 TypeError 变成 500。"""
    source = (ROOT / "webapi.py").read_text(encoding="utf-8")
    start = source.index("async def _api_get_config(")
    end = source.index("\nasync def _api_providers(", start)
    method = source[start:end]

    tail = method[method.index("except Exception") :]
    stripped = [item.strip() for item in tail.splitlines()]

    assert "return" not in stripped, (
        "_api_get_config 异常分支使用裸 return（None），"
        "Quart 无法序列化 None，会把可恢复的读取失败放大成 500"
    )


# ============================================================================
# RL-6 会话代次表无界增长（低危）
# ============================================================================


def test_session_generation_map_is_pruned_on_whitelist_removal() -> None:
    """移出白名单时应清理会话代次记录，避免长期运行内存缓慢增长。"""
    whitelist_source = (ROOT / "whitelist.py").read_text(encoding="utf-8")
    start = whitelist_source.index("    def replace(")
    end = whitelist_source.index("\n    async def commit_change(", start)
    method = whitelist_source[start:end]

    assert "self._prune(umo)" in method, (
        "replace 未通过 gate.prune 清理代次/锁/运行集；"
        "该字典按 UMO 累积且从不回收，长期运行会持续增长"
    )


def test_terminate_clears_image_event_cache() -> None:
    """terminate 应清理含图事件缓存，避免插件重载时残留事件对象。

    实现随 07 迁入 SessionCoordinator：terminate 走 reset_all 级联清空
    （事件/时间/图片/阶段标记），断言锚定单点入口。
    """
    source = _main_source()
    coordinator_source = (ROOT / "session_coordinator.py").read_text(encoding="utf-8")
    method = source[source.index("    async def terminate(") :]

    assert "self._coordinator.reset_all()" in method, (
        "terminate 未经 reset_all 清空会话协作资源（含图片缓存）"
    )
    reset = coordinator_source[
        coordinator_source.index("    def reset_all(") : coordinator_source.index(
            "\n    # ---------", coordinator_source.index("    def reset_all(")
        )
    ]
    assert "_images.clear()" in reset, "reset_all 未清空图片索引"


def test_terminate_waits_for_cancelled_background_tasks() -> None:
    """取消后台任务后必须等待其收尾，避免旧任务越过终止边界。"""
    source = _main_source()
    terminate = source[source.index("    async def terminate(") :]
    wait_method_start = source.index("    async def _wait_background_tasks(")
    wait_method_end = source.index("\n    async def _stop_patrol_task(", wait_method_start)
    wait_method = source[wait_method_start:wait_method_end]

    assert "await self._wait_background_tasks()" in terminate
    assert "asyncio.gather" in wait_method
    assert "self._background_tasks" in wait_method


def test_image_cache_cleanup_has_manual_api_and_startup_sweep() -> None:
    """图片缓存既要能手动清理，也要在插件重载时立即扫一次。"""
    source = _main_source()
    webapi_source = (ROOT / "webapi.py").read_text(encoding="utf-8")
    register_start = webapi_source.index("def register_web_apis(")
    register_end = webapi_source.index("\ndef bind_api_handlers(", register_start)
    registration = webapi_source[register_start:register_end]

    assert 'f"{route}/image-cache/cleanup"' in registration
    assert '"POST"' in registration
    assert "_api_cleanup_image_cache" in registration
    assert "self._cleanup_image_sources(now=now_ts())" in source
    assert (
        "if not self.settings.vision_enabled"
        not in source[
            source.index("    def _cleanup_image_sources(") : source.index(
                "    def _cleanup_old_events_if_needed("
            )
        ]
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
    source = _main_source()
    parser_source = (ROOT / "image" / "parser.py").read_text(encoding="utf-8")

    assert (
        "logger.debug(\n"
        '                "[%s] captured %s/%s images into local vision cache for umo=%s",' in source
    )
    assert (
        "logger.info(\n"
        '                "[%s] captured %s/%s images into local vision cache for umo=%s",'
        not in source
    )
    assert (
        'logger.debug(\n                "[selfreply] host image snapshot created: %s",'
        in parser_source
    )
    assert (
        'logger.info(\n                "[selfreply] host image snapshot created: %s",'
        not in parser_source
    )


def test_config_mutations_share_one_lock_and_settings_normalizer() -> None:
    """白名单和 Web 配置更新不能交错覆盖，配置必须经统一入口规范化。"""
    webapi_source = (ROOT / "webapi.py").read_text(encoding="utf-8")
    whitelist_source = (ROOT / "whitelist.py").read_text(encoding="utf-8")
    api_start = webapi_source.index("async def _api_post_config(")
    api_end = webapi_source.index("\nasync def _api_status(", api_start)
    api = webapi_source[api_start:api_end]

    assert "async with plugin._config_lock" in api
    assert "_api_post_config_locked" in api
    assert "async def add(" in whitelist_source
    assert "async def remove(" in whitelist_source
    assert "Settings.from_config(candidate)" in api


def test_proactive_agent_starts_with_restricted_tool_scope() -> None:
    """主动 Agent 默认不得继承全局插件、跨会话消息和高危工具。

    实现随 04 拆分迁入 generation.py：锚定新文件，断言内容不变（捕获力保持）。
    """
    generation_source = (ROOT / "generation.py").read_text(encoding="utf-8")
    main_source = _main_source()
    start = generation_source.index("    async def generate(")
    end = generation_source.index("\n    def enforce_final_tool_policy(", start)
    method = generation_source[start:end]

    assert "req.func_tool = self._runtime().new_tool_set()" in method
    assert "self.install_agent_tool_boundary(last_event, inherit_tools)" in method
    assert "self._enforce_policy(req, inherit_tools)" in method
    assert "self._call_hook(" in method
    boundary_start = generation_source.index("    def install_agent_tool_boundary(")
    boundary_end = generation_source.index(
        "\n    @staticmethod\n    def restore_agent_tool_boundary", boundary_start
    )
    boundary = generation_source[boundary_start:boundary_end]
    assert "event.plugins_name = []" in boundary
    # 共享 platform_meta 是适配器单例，禁止原地修改；边界必须靠最终工具集策略。
    assert "support_proactive_message" not in boundary
    policy_start = generation_source.index("    def enforce_final_tool_policy(")
    policy_end = generation_source.index("\n    def install_agent_tool_boundary(", policy_start)
    policy = generation_source[policy_start:policy_end]
    assert "filter_final_tools(req, keep=PROACTIVE_ALLOWED_TOOL_IDS)" in policy
    assert "self.restore_agent_tool_boundary" in method
    # 主插件仍经委托壳暴露原方法名（测试与外部调用面保持）
    assert "    async def _generate_reply_via_pipeline(" in main_source
    assert "    def _enforce_final_tool_policy(" in main_source
    assert "    def _install_agent_tool_boundary(" in main_source


def test_new_message_does_not_cancel_running_decorating_hook() -> None:
    """新消息应使旧回复失效，但不能取消正在执行的装饰钩子。"""
    source = _main_source()
    scheduler_source = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    cancel_start = source.index("    def _cancel_delay_task(")
    cancel_end = source.index("\n    def _clear_cached_event(", cancel_start)
    cancel_method = source[cancel_start:cancel_end]
    invalidate_start = source.index("    def _invalidate_session(")
    invalidate_end = source.index("\n    def _cancel_event_session(", invalidate_start)
    invalidate_method = source[invalidate_start:invalidate_end]
    bulk_start = source.index("    def _cancel_delay_tasks(")
    bulk_end = source.index("\n    async def _stop_patrol_task(", bulk_start)
    bulk_method = source[bulk_start:bulk_end]

    # 实现迁入 SessionScheduler（ticket 02）：插件壳只委托，守卫在调度器内
    assert "self._scheduler.cancel_delay(umo, force=force)" in cancel_method
    assert "self._gate.is_running(umo)" in scheduler_source
    assert "force" in cancel_method
    assert "force_cancel" in invalidate_method
    assert "force_cancel=True" in bulk_method


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
    original_enforce = plugin._enforce_final_tool_policy

    def counting_enforce(req, inherit_tools):
        ok = original_enforce(req, inherit_tools)
        enforce_snapshots.append(sorted(main._AGENT_RUNTIME.final_tool_ids(req) or []))
        if len(enforce_snapshots) == 1:
            # 模拟 hook 在第一次 enforce 之后向 req 注入工具
            req.func_tool.add_tool(SimpleNamespace(name="hook_injected"))
        return ok

    plugin._enforce_final_tool_policy = counting_enforce
    return {
        "req_holder": req_holder,
        "enforce_snapshots": enforce_snapshots,
        "reset_snapshots": reset_snapshots,
        "prompts": prompts,
        "restore": lambda: (
            setattr(plugin, "_enforce_final_tool_policy", original_enforce),
            setattr(main, "_AGENT_RUNTIME", original_runtime),
        ),
    }


async def _run_pipeline(plugin):
    state = plugin._state_for(UMO)
    token = plugin._gate.advance(UMO)
    return await plugin._generate_reply_via_pipeline(
        UMO, state, expected_generation=token, force=True
    )


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
        original_enforce = plugin._enforce_final_tool_policy

        def counting_enforce(req, inherit_tools):
            ok = original_enforce(req, inherit_tools)
            enforce_snapshots.append(sorted(main._AGENT_RUNTIME.final_tool_ids(req) or []))
            if len(enforce_snapshots) == 1:
                req.func_tool.add_tool(SimpleNamespace(name="hook_injected"))
            return ok

        plugin._enforce_final_tool_policy = counting_enforce
        try:
            result = await _run_pipeline(plugin)
            assert result.text == "你好呀"
            # 快照为 False：即使运行中 settings 变为 True，enforce 仍按 False 清理
            assert enforce_snapshots == [[], []]
            assert main._AGENT_RUNTIME.final_tool_ids(req_holder["req"]) == []
        finally:
            plugin._enforce_final_tool_policy = original_enforce
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
        web.request.payload = {"whitelist": []}
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
        original_enforce = plugin._enforce_final_tool_policy

        def counting_enforce(req, inherit_tools):
            ok = original_enforce(req, inherit_tools)
            enforce_snapshots.append(sorted(main._AGENT_RUNTIME.final_tool_ids(req) or []))
            if len(enforce_snapshots) == 1:
                # 模拟 hook 在第一次 enforce 后注入危险工具（kb agentic）与普通工具
                req.func_tool.add_tool(SimpleNamespace(name="astr_kb_search"))
                req.func_tool.add_tool(SimpleNamespace(name="third_party_weather"))
            return ok

        plugin._enforce_final_tool_policy = counting_enforce
        try:
            result = await plugin._generate_reply_via_pipeline(
                UMO, plugin._state_for(UMO), expected_generation=1, force=True
            )
            assert result.text == "你好呀"
            # 修复前：继承分支直接 return True → 危险工具残留（红灯）
            assert enforce_snapshots[0] == ["send_image"]
            assert enforce_snapshots[1] == ["send_image", "third_party_weather"]
        finally:
            plugin._enforce_final_tool_policy = original_enforce
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
        original_send_reply = plugin._send_reply

        async def unknown_send_reply(umo, reply, *, expected_generation=None):
            models = importlib.import_module(f"{main.__package__}.models")
            return models.SendOutcome(models.SendStatus.UNKNOWN, "adapter raised after submit")

        plugin._send_reply = unknown_send_reply
        try:
            result = await plugin._check_session(UMO, trigger="patrol", force=True)
            assert "未自动重试" in result
            # 修复前：direct_send_count>0 时跳过记录 → daily_count 不增（红灯）
            assert state.daily_count >= 1
            assert state.last_proactive_observed_at >= state.last_active_at
            assert state.last_proactive_at >= state.last_active_at
        finally:
            plugin._send_reply = original_send_reply
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
        plugin._schedule_delayed_check(UMO, delay_sec=None, trigger="message_delay", force=False)
        old_task = plugin._delay_tasks.get(UMO)
        assert old_task is not None and not old_task.cancelled()

        async def boom():
            raise OSError("disk full")

        plugin._save_storage = boom
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist": []}
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
            result = await plugin._generate_reply_via_pipeline(
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
    """/off 临时暂停后，GET config 的 enabled 仍是持久值，runtime_enabled 单独暴露。"""

    async def scenario(plugin, main):
        plugin.runtime_enabled = False  # 模拟 /off
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
                plugin._check_session(UMO, trigger="patrol", force=True),
                plugin._check_session(UMO, trigger="patrol", force=True),
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
        result = await plugin._check_session(UMO, trigger="patrol", force=False)
        assert result == "会话不在主动回复白名单。"

    with_plugin(tmp_path, scenario)


# ============================================================================
# round6：高频成功路径日志级别契约
# ============================================================================


def _log_call_level(source: str, frag: str) -> list[str]:
    """定位 frag 消息对应的 logger 调用前缀（rfind 最近调用点，支持多处）。"""
    levels: list[str] = []
    search_from = 0
    while True:
        idx = source.find(frag, search_from)
        if idx == -1:
            break
        call = source.rfind("logger.", 0, idx)
        assert call != -1, f"no logger call before {frag!r}"
        levels.append(source[call : source.index("(", call)])
        search_from = idx + len(frag)
    return levels


def _parser_source() -> str:
    import tests.test_vision as vision

    return (Path(vision.ROOT) / "image" / "parser.py").read_text(encoding="utf-8")


def _scheduler_source() -> str:
    import tests.test_vision as vision

    return (Path(vision.ROOT) / "scheduler.py").read_text(encoding="utf-8")


def _delivery_source() -> str:
    import tests.test_vision as vision

    return (Path(vision.ROOT) / "delivery.py").read_text(encoding="utf-8")


# ============================================================================
# main.py：7 处降级点（静默等待日志随 02 拆分迁入 scheduler.py）
# ============================================================================


def test_main_logs_wait_silence_is_debug() -> None:
    source = _scheduler_source()
    assert _log_call_level(source, "[%s] wait for minimum silence session=") == ["logger.debug"]


def test_main_logs_skip_session_is_debug() -> None:
    source = _main_source()
    assert _log_call_level(source, "[%s] skip session=%s trigger=") == ["logger.debug"]


def test_main_logs_decision_is_debug() -> None:
    source = _main_source()
    assert _log_call_level(source, "[%s] decision session=%s trigger=") == ["logger.debug"]


def test_main_logs_skip_before_send_is_debug() -> None:
    source = _delivery_source()
    assert _log_call_level(source, "[%s] skip before send session=") == ["logger.debug"]


def test_main_logs_reply_sent_both_branches_are_debug() -> None:
    """log_reply_content 的 if/else 双分支都必须降级（两处调用点）。"""
    source = _delivery_source()
    levels = _log_call_level(source, "[%s] proactive reply sent session=")
    assert levels == ["logger.debug", "logger.debug"]


def test_main_logs_event_send_completed_is_debug() -> None:
    source = _delivery_source()
    assert _log_call_level(source, "[%s] event send completed session=") == ["logger.debug"]


# ============================================================================
# image/parser.py：2 处降级点
# ============================================================================


def test_parser_logs_image_frozen_to_cache_is_debug() -> None:
    source = _parser_source()
    assert _log_call_level(source, "image frozen to local cache: %s") == ["logger.debug"]


def test_parser_logs_image_frozen_data_url_is_debug() -> None:
    source = _parser_source()
    assert _log_call_level(source, "image frozen as in-memory data URL") == ["logger.debug"]


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
        plugin._schedule_delayed_check(UMO, delay_sec=None, trigger="message_delay", force=False)
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
        plugin._schedule_delayed_check(UMO, delay_sec=None, trigger="message_delay", force=False)
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
# R17：bridge 缓存复用（MP1-8）
# ============================================================================


def test_r17_recorder_bridge_is_cached(tmp_path: Path) -> None:
    """get_recorder_bridge 第二次调用必须复用首个实例。

    修复前条件 `_default_bridge is None or context is not None` 恒真
    （唯一调用点恒传 context）→ 每次新建实例（红灯）。
    """

    async def scenario(plugin, main):
        from .host_stubs import MAIN_PACKAGE_NAME

        rb = sys.modules[f"{MAIN_PACKAGE_NAME}.image.recorder_bridge"]
        rb._default_bridge = None
        first = rb.get_recorder_bridge(plugin.context)
        # 唯一调用点恒传 context：修复前每次调用都重建 → 非同一实例（红灯）
        second = rb.get_recorder_bridge(plugin.context)
        assert second is first, "bridge 必须复用缓存实例"
        rb._default_bridge = None

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
            task = asyncio.create_task(plugin._check_session(UMO, trigger="patrol", force=True))
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
    """事件/时间/图片三表的清理必须收敛经 SessionCoordinator，main 只经委托壳。"""
    import tests.test_vision as vision

    main_source = (Path(vision.ROOT) / "main.py").read_text(encoding="utf-8")
    coordinator_source = (Path(vision.ROOT) / "session_coordinator.py").read_text(encoding="utf-8")

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
    invalidate = coordinator_source[
        coordinator_source.index("    def invalidate(") : coordinator_source.index(
            "\n    def clear(", coordinator_source.index("    def invalidate(")
        )
    ]
    assert "self._gate.advance(umo)" in invalidate
    assert "self._cancel_delay(umo, force_cancel)" in invalidate
    assert "self.clear(umo)" in invalidate

    clear = coordinator_source[
        coordinator_source.index("    def clear(") : coordinator_source.index(
            "\n    def reset_all(", coordinator_source.index("    def clear(")
        )
    ]
    for frag in ["_events.pop", "_event_at.pop", "_images.pop", "_phases.pop"]:
        assert frag in clear, f"clear 必须级联清理 {frag}"


# ============================================================================
# R20：FSM 状态可观测（on_message 记录事件后处于 OBSERVING）
# ============================================================================


def test_r20_state_is_observing_after_message_recorded(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        event = _make_event()
        plugin._coordinator.record_event(UMO, event, 1.0)

        phase = plugin._coordinator.state(UMO)
        assert phase.value == "observing"

        plugin._coordinator.invalidate(UMO)
        assert plugin._coordinator.state(UMO).value == "idle"

    with_plugin(tmp_path, scenario)
