"""红灯测试（第五轮）：0.7.20 第二轮全面审查修复验证

每个测试断言"期望的正确行为"，修复前应当失败（红灯），修复后转绿：
- R6 继承模式危险工具 denylist：hook 注入宿主级危险工具仍被拒绝，普通工具保留
- R7 UNKNOWN 发送：工具已直发时也必须记录状态（观察窗口/配额推进）
- R8 配置回滚：被白名单变更取消的延迟检查在回滚后重新调度
- R9 生成超时：先优雅停止（request_stop），不硬取消 run_agent
- R10 API GET：enabled 返回持久配置，runtime_enabled 单独暴露
- R11 同会话并发互斥：第二个 _check_session 必须被拒绝，配额只计一次
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

from .host_stubs import with_plugin
from .test_main_runtime import UMO, _PipelineTestAdapter, _make_event


def _load_main():
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
            # 普通插件工具 + 宿主级危险工具（cron）
            for name in ("send_image", "create_future_task"):
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
            enforce_snapshots.append(
                sorted(main._AGENT_RUNTIME.final_tool_ids(req) or [])
            )
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
        original_send = event.send

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
            return main.SendOutcome(main.SendStatus.UNKNOWN, "adapter raised after submit")

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
        plugin._schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
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
