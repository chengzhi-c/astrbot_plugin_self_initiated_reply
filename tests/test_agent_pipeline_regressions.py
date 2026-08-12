"""Agent 管线回归测试：主动回复生成与发送路径的历史缺陷固化验证

每个测试断言"期望的正确行为"，修复前应当失败（红灯），修复后转绿
（历史红灯测试阶段 0 去阶段化更名，2026-08-07）：
- run_task 孤儿泄漏：force cancel 后 run_task 被收敛、request_stop 被调
- context 兜底发送：返回 None（成功）记 DELIVERED 且写入 assistant 历史
- 只读命令：status/list/help/debug 不得失效会话（延迟任务与缓存保留）
- 配置回滚：回滚后恢复 patrol/cleanup 任务拓扑
- _call_compat：函数体内部 TypeError 不得触发 minimal 重试（防双执行）
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from .host_stubs import until, with_plugin
from .test_main_runtime import UMO, _make_event, _PipelineTestAdapter


def _load_main():
    import tests.test_vision as vision

    from .host_stubs import install_astrbot_stubs

    # 本文件可独立运行：stub 安装不能依赖 test_vision 先跑（排序依赖），
    # 加载前显式安装，幂等，重复调用无副作用。
    install_astrbot_stubs()
    root = Path(vision.ROOT)
    package = vision.PACKAGE_NAME
    if package not in sys.modules:
        module = __import__("types").ModuleType(package)
        module.__path__ = [str(root)]
        sys.modules[package] = module
    return importlib.import_module(f"{package}.main")


# ============================================================================
# 0.1 P0：run_task 孤儿泄漏
# ============================================================================


def test_force_cancel_converges_agent_run_task(tmp_path: Path) -> None:
    """运行中检查被 force cancel：run_task 必须被收敛，request_stop 必须被调。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    stop_called: list[bool] = []
    run_finished: list[bool] = []

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        entered = asyncio.Event()

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
                try:
                    entered.set()
                    await asyncio.sleep(3600)  # 永不结束的 run_agent
                    yield None
                finally:
                    run_finished.append(True)

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_grace = main.GRACEFUL_STOP_GRACE_SEC
        main.GRACEFUL_STOP_GRACE_SEC = 0.05
        try:
            task = asyncio.create_task(
                plugin._generation.generate(
                    UMO, plugin._state_for(UMO), expected_generation=1, force=True
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()  # 模拟 /off / terminate 等 force cancel
            try:
                await task
            except asyncio.CancelledError:
                pass
            # 修复前：run_task 未被 shield 收敛，成为孤儿继续运行（红灯）
            assert run_finished == [True]
            assert stop_called == [True]
        finally:
            main.GRACEFUL_STOP_GRACE_SEC = original_grace
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario, generation_timeout_sec=60)


def test_force_cancel_kills_running_check_task(tmp_path: Path) -> None:
    """scheduler.cancel_delay(force=True) 必须同时取消运行中的检查任务。

    变异锚定：cancel_delay_task 的 force 分支失效（不取消 running_task）
    后本测试必须变红。
    """

    async def scenario(plugin, main):
        started = asyncio.Event()

        async def hanging():
            started.set()
            await asyncio.sleep(3600)

        running = asyncio.create_task(hanging())
        await started.wait()
        plugin._running_check_tasks[UMO] = running
        delay_task = asyncio.create_task(asyncio.sleep(3600))
        plugin._delay_tasks[UMO] = delay_task

        plugin._scheduler.cancel_delay(UMO, force=True)
        # 事件驱动等待 cancel 生效，替代单次 sleep(0)（flaky 修复）
        await until(lambda: running.done() and delay_task.done())

        assert running.done() and running.cancelled()
        assert delay_task.done() and delay_task.cancelled()
        assert UMO not in plugin._delay_tasks
        for t in (running, delay_task):  # 变异下兜底取消，避免 gather 长挂
            if not t.done():
                t.cancel()
        await asyncio.gather(running, delay_task, return_exceptions=True)

    with_plugin(tmp_path, scenario)


def test_delayed_check_waits_for_running_session_release(tmp_path: Path) -> None:
    """延迟检查须等待前一个 check 结束（事件驱动，非轮询）。"""

    async def scenario(plugin, main):
        entered_wait = asyncio.Event()
        original_release = plugin._gate.release_event

        def patched_release(umo):
            ev = original_release(umo)
            entered_wait.set()  # 确定性锚点：B 已挂起在等待上
            return ev

        plugin._gate.release_event = patched_release
        plugin._gate.mark_running(UMO)  # 前一个 check 占住运行集
        try:
            task = asyncio.create_task(
                plugin._scheduler.delayed_check(
                    UMO,
                    delay_sec=0,
                    trigger="patrol",
                    force=True,
                    generation=plugin._gate.advance(UMO),
                )
            )
            await asyncio.wait_for(entered_wait.wait(), timeout=2)
            assert not task.done(), "运行集被占用时延迟检查不应完成"
            plugin._gate.unmark_running(UMO)  # 前一个 check 结束
            # 用 asyncio.wait（而非 wait_for）：task 吞掉取消时 wait_for 会
            # 正常返回（Python 3.12+ 行为），导致变异不被捕获。
            done, _pending = await asyncio.wait({task}, timeout=2)
            assert task in done, "释放后延迟检查应完成"
        finally:
            plugin._gate.release_event = original_release

    with_plugin(tmp_path, scenario)


def test_prune_wakes_waiting_delayed_check(tmp_path: Path) -> None:
    """移出白名单时 gate.prune 须唤醒挂起在运行释放上的延迟检查。"""

    async def scenario(plugin, main):
        entered_wait = asyncio.Event()
        original_release = plugin._gate.release_event

        def patched_release(umo):
            ev = original_release(umo)
            entered_wait.set()
            return ev

        plugin._gate.release_event = patched_release
        plugin._gate.mark_running(UMO)  # 模拟正在运行的 check 占住运行集
        try:
            task = asyncio.create_task(
                plugin._scheduler.delayed_check(
                    UMO,
                    delay_sec=0,
                    trigger="patrol",
                    force=True,
                    generation=plugin._gate.advance(UMO),
                )
            )
            await asyncio.wait_for(entered_wait.wait(), timeout=2)
            assert not task.done(), "白名单移除前延迟检查应等待"
            plugin._whitelist.replace(set())  # 移出全部会话
            done, _pending = await asyncio.wait({task}, timeout=2)
            assert task in done, "白名单移除后挂起的延迟检查应被唤醒退出"
        finally:
            plugin._gate.release_event = original_release

    with_plugin(tmp_path, scenario)


def test_stale_generation_rejected_at_session_entry(tmp_path: Path) -> None:
    """旧代次任务在会话入口即被放弃，不进入决策与发送。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        token = plugin._gate.advance(UMO)
        plugin._gate.advance(UMO)  # 抬代次使 token 过期
        result = await plugin._pipeline.check_session(
            UMO, trigger="patrol", force=True, expected_generation=token
        )
        assert result == "会话已经更新，放弃旧任务。"

    with_plugin(tmp_path, scenario)


def test_force_cancel_converges_before_grace_timeout(tmp_path: Path) -> None:
    """显式 cancel 应在 grace 超时前收敛 run_task（第一层保险的时序守卫）。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    run_finished: list[bool] = []

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        entered = asyncio.Event()

        class HangingRunner:
            def reset(self, **_):
                return _FakeResetCoro()

            def request_stop(self):
                pass

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
                try:
                    entered.set()
                    await asyncio.sleep(3600)  # 永不结束的 run_agent
                    yield None
                finally:
                    run_finished.append(True)

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_grace = main.GRACEFUL_STOP_GRACE_SEC
        # 30s 宽限期：若缺少显式 cancel（第一层保险），收敛只能等 grace 超时
        main.GRACEFUL_STOP_GRACE_SEC = 30
        try:
            task = asyncio.create_task(
                plugin._generation.generate(
                    UMO, plugin._state_for(UMO), expected_generation=1, force=True
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()  # 模拟 /off / terminate 等 force cancel
            await asyncio.sleep(0.5)  # 显式 cancel 应立即收敛，无需等待 30s grace
            assert run_finished == [True], "run_task 未在 grace 超时前收敛（显式 cancel 兜底缺失）"
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            main.GRACEFUL_STOP_GRACE_SEC = original_grace
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario, generation_timeout_sec=60)


# ============================================================================
# 0.2 P1：context 兜底发送误记 UNKNOWN
# ============================================================================


def test_context_send_none_is_delivered_and_writes_history(tmp_path: Path) -> None:
    """context 兜底发送正常完成（返回 None）：记 DELIVERED 并写入 assistant 历史。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        sent_via_context: list[tuple[str, object]] = []

        async def ctx_send(umo_, message):
            sent_via_context.append((umo_, message))
            return None  # 真实宿主 Context.send_message 正常完成返回 None

        plugin.context.send_message = ctx_send

        class Runner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        async def build_effect(kwargs, result):
            return FakeBuildResult(
                agent_runner=Runner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                # 模拟生成期间事件被清理：send_reply 走 context 兜底路径
                plugin._coordinator.clear(UMO)
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        try:
            result = await plugin._pipeline.check_session(UMO, trigger="patrol", force=True)
            # 修复前：None 被记 UNKNOWN → "主动发送状态未知，未自动重试。"（红灯）
            assert "已主动回复" in result
            assert sent_via_context
            assert state.last_proactive_text == "你好呀"
            assert state.recent[-1].role == "assistant"
            assert state.daily_count == 1
        finally:
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# 0.3 P1：只读命令误失效会话
# ============================================================================


def test_readonly_commands_do_not_invalidate_session(tmp_path: Path) -> None:
    """status 只读查询不得取消待执行的延迟检查，也不得清空事件缓存。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._state_for(UMO)
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        task = plugin._delay_tasks.get(UMO)
        assert task is not None and not task.done()

        await plugin._command_text(event, "status")
        # 修复前：status 也 invalidate → 延迟任务被取消移除、缓存被清（红灯）
        assert plugin._delay_tasks.get(UMO) is task
        assert not task.done()
        assert plugin._last_events.get(UMO) is event
        assert plugin._last_event_at.get(UMO) == 1.0

    with_plugin(tmp_path, scenario)


# ============================================================================
# 0.4 P2：配置回滚不恢复任务拓扑
# ============================================================================


def test_config_rollback_restores_task_topology(tmp_path: Path) -> None:
    """禁用路径 stop_patrol 失败回滚后，patrol 任务必须恢复运行。"""

    async def scenario(plugin, main):
        plugin._scheduler.ensure_patrol()
        assert plugin._scheduler.patrol_task is not None
        assert not plugin._scheduler.patrol_task.done()

        original_stop = plugin._scheduler.stop_patrol

        async def failing_stop():
            await original_stop()
            raise OSError("stop patrol failed")

        plugin._scheduler.stop_patrol = failing_stop
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"enabled": False}
            result = await plugin._api_post_config()
            assert result.get("ok") is False
            # 修复前：回滚只恢复 settings/runtime_enabled，不重启 patrol（红灯）
            assert plugin.runtime_enabled is True
            assert plugin._scheduler.patrol_task is not None
            assert not plugin._scheduler.patrol_task.done()
        finally:
            plugin._scheduler.stop_patrol = original_stop

    with_plugin(tmp_path, scenario, enabled_patrol_trigger=True)


def test_config_rollback_reschedules_cancelled_delayed_checks(tmp_path: Path) -> None:
    """回滚后按快照重建被取消的延迟检查（message_delay 语义）。"""

    async def scenario(plugin, main):
        plugin._state_for(UMO)
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        original_task = plugin._delay_tasks.get(UMO)
        assert original_task is not None and not original_task.done()

        original_stop = plugin._scheduler.stop_patrol

        async def failing_stop():
            await original_stop()
            raise OSError("stop patrol failed")

        plugin._scheduler.stop_patrol = failing_stop
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"enabled": False}
            result = await plugin._api_post_config()
            assert result.get("ok") is False
            new_task = plugin._delay_tasks.get(UMO)
            assert new_task is not None and not new_task.done(), "回滚后延迟检查未重建"
        finally:
            plugin._scheduler.stop_patrol = original_stop

    with_plugin(tmp_path, scenario)

    with_plugin(tmp_path, scenario, enabled_patrol_trigger=True)


def test_gate_restore_recovers_running_set(tmp_path: Path) -> None:
    """restore 必须恢复运行集快照，否则回滚后运行标记漂移。

    变异锚定：session_gate.restore 删除 ``self._running_sessions = snap["running"]``
    后本测试必须变红（该变异曾在 0.8.2 三方审查中实测存活）。
    """

    async def scenario(plugin, main):
        gate = plugin._gate
        gate.mark_running(UMO)
        snap = gate.snapshot()
        gate.unmark_running(UMO)
        gate.mark_running("other:session")
        gate.restore(snap)
        assert gate.is_running(UMO) is True
        assert gate.is_running("other:session") is False

    with_plugin(tmp_path, scenario)


def test_gate_restore_clears_stale_release_for_still_running(tmp_path: Path) -> None:
    """回滚后仍标记运行中的会话：陈旧的 release set 必须清掉，事件身份必须不变。

    ``unmark_running`` 只 set 不 pop，所以回滚把运行标记恢复成快照态后，表里
    那个事件仍是已 set 的。此时 scheduler 的 ``while is_running: await
    release_event(umo).wait()`` 每轮立即返回——紧密空转独占事件循环，整个 bot
    卡死（0.9.4 阶段 1.1）。

    变异锚定：删除 ``restore`` 中的 ``release.clear()`` 分支后本测试必须变红。
    """

    async def scenario(plugin, main):
        gate = plugin._gate
        gate.mark_running(UMO)
        waiter_event = gate.release_event(UMO)  # 等待者持有此对象
        snap = gate.snapshot()
        gate.unmark_running(UMO)  # set 但不 pop
        assert waiter_event.is_set()

        gate.restore(snap)

        assert gate.is_running(UMO) is True
        assert gate.release_event(UMO) is waiter_event, "等待者持有的事件被换掉（孤儿事件）"
        assert not waiter_event.is_set(), "陈旧 set 未清除：scheduler 将空转饿死事件循环"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(waiter_event.wait(), timeout=0.02)

    with_plugin(tmp_path, scenario)


def test_gate_restore_wakes_waiter_for_no_longer_running(tmp_path: Path) -> None:
    """回滚后不再运行的会话：等待者必须被唤醒，否则它等一个不会到来的信号。

    变异锚定：删除 ``restore`` 中的 ``release.set()`` 分支后本测试必须变红。
    """

    async def scenario(plugin, main):
        gate = plugin._gate
        snap = gate.snapshot()  # 快照时该会话未运行
        gate.mark_running(UMO)
        waiter_event = gate.release_event(UMO)
        assert not waiter_event.is_set()

        gate.restore(snap)

        assert gate.is_running(UMO) is False
        assert waiter_event.is_set(), "等待者未被唤醒：该会话主动回复静默死亡"
        await asyncio.wait_for(waiter_event.wait(), timeout=0.02)

    with_plugin(tmp_path, scenario)


# ============================================================================
# 0.5 P2：_call_compat TypeError 重试双执行
# ============================================================================


def test_call_compat_does_not_retry_body_type_error() -> None:
    """函数体内部抛 TypeError（与签名无关）：只调用一次，不触发 minimal 重试。"""

    main = _load_main()
    adapters = importlib.import_module(f"{main.__package__}.adapters")
    calls: list[str] = []

    def func(prompt, **rest):
        calls.append(prompt)
        raise TypeError("internal boom")  # 函数体内部 TypeError，非签名不匹配

    with pytest.raises(TypeError):
        asyncio.run(
            adapters.AstrBotBridge._call_compat(
                func,
                kwargs={"prompt": "x", "temperature": 0.5},
                minimal_kwargs={"prompt": "x"},
            )
        )
    # 修复前：TypeError 触发 minimal 重试 → 调用两次（对 LLM 即重复计费）（红灯）
    assert calls == ["x"]
