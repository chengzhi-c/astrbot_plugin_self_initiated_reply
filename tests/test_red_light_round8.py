"""红灯测试（第八轮）：ticket 07 会话状态显式化验证

- R18 ABA：白名单移除后重加，旧任务（持有旧代次 token）不得复活发送/记录
- R19 级联单点：main 不得散落事件表清理（收敛 SessionCoordinator）
- R20 状态投影：on_message 记录事件后会话处于 OBSERVING（FSM 可观测）
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

from .host_stubs import with_plugin
from .test_main_runtime import UMO, _make_event, _PipelineTestAdapter


def _load_main():
    import tests.test_vision as vision

    root = Path(vision.ROOT)
    package = types.ModuleType(vision.PACKAGE_NAME)
    package.__path__ = [str(root)]
    sys.modules[vision.PACKAGE_NAME] = package
    return importlib.import_module(f"{vision.PACKAGE_NAME}.main")


# ============================================================================
# R18：ABA——白名单移除后重加，旧任务不得复活
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
