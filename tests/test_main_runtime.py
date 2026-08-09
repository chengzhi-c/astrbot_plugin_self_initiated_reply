"""main.py 行为测试基线（0.7.14 P0/P1 修复）。

这些测试真实导入并实例化 SelfInitiatedReplyPlugin，验证：
- 工具 fail closed 与共享 platform_meta 零写入
- generation 全局单调（白名单 ABA）
- UNKNOWN 发送语义（不重试 / 消耗配额 / 不写历史 / 不触发 after-send hook）
- 配置态与运行态分离（临时开关不被无关保存清除）
- terminate 后 spawn barrier
- 会话锁回收
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from .host_stubs import (
    FakeToolSet,
    reset_hook_calls,
    with_plugin,
)

UMO = "fake:group:123"


@pytest.fixture(autouse=True)
def _cleanup_plugin_state():
    reset_hook_calls()
    yield
    reset_hook_calls()


def _make_event(umo: str = UMO, **kwargs):
    from .host_stubs import FakeEvent

    return FakeEvent(umo=umo, **kwargs)


# ============================================================================
# 工具边界
# ============================================================================


def test_prune_clears_last_decisions(tmp_path: Path) -> None:
    """复审 S1：会话回收必须同步清理调试面板的最近裁决，防同形态泄漏。"""

    async def scenario(plugin, main):
        umo = UMO
        plugin._last_decisions[umo] = {
            "at": 1.0,
            "trigger": "message",
            "should_reply": True,
            "reason": "x",
        }
        plugin.settings.whitelist.add(umo)
        removed = await plugin._remove_whitelist_session(umo)
        assert removed is True
        assert umo not in plugin._last_decisions

    with_plugin(tmp_path, scenario)


def test_skipped_decision_recorded_in_last_decisions(tmp_path: Path) -> None:
    """复审 P2：跳过决策（静默不足等早退原因）也进入调试面板。"""

    class _FakeDecision:
        async def decide(self, umo, state, *, trigger, force):
            return "静默时间不足：1s / 60s。"

    async def scenario(plugin, main):
        umo = UMO
        state = plugin._state_for(umo)
        plugin._decision = _FakeDecision()
        result = await plugin._decide_session_reply(
            umo,
            state,
            trigger="message",
            force=False,
            expected_generation=None,
        )
        assert result == "静默时间不足：1s / 60s。"
        recorded = plugin._last_decisions[umo]
        assert recorded["should_reply"] is False
        assert "静默时间不足" in recorded["reason"]

    with_plugin(tmp_path, scenario)


def test_install_boundary_only_touches_event_plugins_name(tmp_path: Path) -> None:
    """共享 platform_meta 不得被原地修改；只允许收紧事件自己的插件范围。"""

    async def scenario(plugin, main):
        event = _make_event()
        original_plugins_name = ["other_plugin"]
        event.platform_meta.support_proactive_message = True
        event.plugins_name = list(original_plugins_name)

        state = plugin._install_agent_tool_boundary(event, False)
        assert event.plugins_name == []
        assert event.platform_meta.support_proactive_message is True

        plugin._restore_agent_tool_boundary(event, state)
        assert event.plugins_name == original_plugins_name

    with_plugin(tmp_path, scenario)


def test_inherit_tools_mode_keeps_plugin_names_and_skips_policy(tmp_path: Path) -> None:
    """开关开启时：主动运行不清空插件工具边界，最终工具集也不清理。"""

    async def scenario(plugin, main):
        event = _make_event()
        event.plugins_name = ["stealer", "living_memory"]

        state = plugin._install_agent_tool_boundary(event, True)
        assert state == {}
        assert event.plugins_name == ["stealer", "living_memory"]

        tool_set = FakeToolSet()
        tool_set.add_tool(type("T", (), {"name": "stealer_fetch"})())
        req = type("Req", (), {"func_tool": tool_set})()
        assert plugin._enforce_final_tool_policy(req, True) is True
        assert [tool.name for tool in tool_set.tools] == ["stealer_fetch"]

    with_plugin(tmp_path, scenario, proactive_inherit_tools=True)


def test_inherit_tools_default_off_and_persisted_via_api(tmp_path: Path) -> None:
    """开关默认关闭；API 可持久化开启并回读。"""

    async def scenario(plugin, main):
        assert plugin.settings.proactive_inherit_tools is False

        await _post_config(plugin, {"proactive_inherit_tools": True})
        assert plugin.settings.proactive_inherit_tools is True

        config = await plugin._api_get_config()
        assert config.get("proactive_inherit_tools") is True

        # 非法类型必须拒绝
        await _post_config(plugin, {"proactive_inherit_tools": "yes"})
        assert plugin.settings.proactive_inherit_tools is True  # 未变化

    with_plugin(tmp_path, scenario)


def test_filter_final_tools_removes_injected_tools(tmp_path: Path) -> None:
    """宿主 build 注入的工具必须在 reset/run 前被清空。"""

    async def scenario(plugin, main):
        tool_set = FakeToolSet()
        tool_set.add_tool(type("T", (), {"name": "send_message_to_user"})())
        tool_set.add_tool(type("T", (), {"name": "web_search"})())
        tool_set.add_tool(type("T", (), {"name": "mcp_anything"})())
        req = type("Req", (), {"func_tool": tool_set})()

        ok = main._AGENT_RUNTIME.filter_final_tools(req, keep=frozenset())
        assert ok is True
        assert tool_set.tools == []

    with_plugin(tmp_path, scenario)


def test_filter_final_tools_keeps_only_allowed(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        tool_set = FakeToolSet()
        tool_set.add_tool(type("T", (), {"name": "safe_tool"})())
        tool_set.add_tool(type("T", (), {"name": "danger_tool"})())
        req = type("Req", (), {"func_tool": tool_set})()

        ok = main._AGENT_RUNTIME.filter_final_tools(req, keep=frozenset({"safe_tool"}))
        assert ok is True
        assert [tool.name for tool in tool_set.tools] == ["safe_tool"]

    with_plugin(tmp_path, scenario)


def test_filter_final_tools_fails_closed_when_unverifiable(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        req = type("Req", (), {"func_tool": type("Bad", (), {"tools": None})()})()
        assert main._AGENT_RUNTIME.filter_final_tools(req, keep=frozenset()) is False

        # 无 func_tool 视为天然空集，允许通过
        empty_req = type("Req", (), {"func_tool": None})()
        assert main._AGENT_RUNTIME.filter_final_tools(empty_req, keep=frozenset()) is True

    with_plugin(tmp_path, scenario)


def test_enforce_final_tool_policy_fail_closed_aborts_run(tmp_path: Path) -> None:
    """无法枚举工具集时，本次主动 Agent 必须拒绝运行而不是继续。"""

    async def scenario(plugin, main):
        bad_req = type("Req", (), {"func_tool": type("Bad", (), {"tools": None})()})()
        assert plugin._enforce_final_tool_policy(bad_req, False) is False

        tool_set = FakeToolSet()
        tool_set.add_tool(type("T", (), {"name": "send_message_to_user"})())
        clean_req = type("Req", (), {"func_tool": tool_set})()
        assert plugin._enforce_final_tool_policy(clean_req, False) is True
        assert tool_set.tools == []

    with_plugin(tmp_path, scenario)


class _PipelineTestAdapter:
    """Wrap the real runtime adapter; only build/run are injectable.

    ``enforce``/``final_tool_ids``/``new_tool_set`` stay real so the integration
    test exercises the actual tool-boundary logic.
    """

    def __init__(self, base, *, build_effect=None, run_effect=None):
        self._base = base
        self._build_effect = build_effect
        self._run_effect = run_effect

    async def build(self, **kwargs):
        result = await self._base.build(**kwargs)
        if self._build_effect is not None:
            result = await self._build_effect(kwargs, result)
        return result

    def run(self, agent_runner, **kwargs):
        if self._run_effect is not None:
            return self._run_effect(agent_runner, **kwargs)
        return self._base.run(agent_runner, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base, name)


def test_pipeline_injects_tools_and_enforces_policy_twice(tmp_path: Path) -> None:
    """核心管线集成：build 注入工具 → 两次 enforce 清空（含 hook 注入）→ run 期间
    tool_direct 直发被计数/抑制 → finally 恢复 event.send 与 plugins_name。"""

    async def scenario(plugin, main):
        from types import SimpleNamespace

        from .host_stubs import FakeBuildResult, _FakeMessageChain, _FakeResetCoro

        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        original_plugins_name = ["other_plugin"]
        event.plugins_name = list(original_plugins_name)

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

        async def build_effect(kwargs, result):
            # 模拟宿主 build 在返回前注入工具（web search / proactive send / MCP）
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
                # 预算内 2 次直发 + 1 次超预算（MAX_DIRECT_TOOL_SENDS = 2）
                for i in range(3):
                    await event.send(
                        _FakeMessageChain(type="tool_direct_result", chain=[f"直发{i}"])
                    )
                    yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        enforce_tool_snapshots: list[list[str]] = []
        original_enforce = plugin._enforce_final_tool_policy

        def counting_enforce(req, inherit_tools):
            ok = original_enforce(req, inherit_tools)
            enforce_tool_snapshots.append(sorted(main._AGENT_RUNTIME.final_tool_ids(req) or []))
            if len(enforce_tool_snapshots) == 1:
                # 模拟 hook 在第一次 enforce 之后向 req 注入工具
                req.func_tool.add_tool(SimpleNamespace(name="hook_injected"))
            return ok

        plugin._enforce_final_tool_policy = counting_enforce
        try:
            state = plugin._state_for(UMO)
            token = plugin._gate.advance(UMO)
            result = await plugin._generate_reply_via_pipeline(
                UMO, state, expected_generation=token, force=True
            )

            # 两次 enforce 都执行且都清空（hook 注入的工具也被第二次清掉）
            assert len(enforce_tool_snapshots) == 2
            assert enforce_tool_snapshots[0] == []
            assert enforce_tool_snapshots[1] == []
            # run 结束时 req.func_tool 保持为空
            assert main._AGENT_RUNTIME.final_tool_ids(req_holder["req"]) == []
            # 直发计数：前 2 次被接受，第 3 次超预算抑制
            assert result.direct_send_count == 2
            assert len(result.direct_texts) == 2
            assert result.text == "你好呀"
            # finally 恢复：实例 send 已清除（回到类级 send），plugins_name 复原
            assert "send" not in event.__dict__
            assert event.plugins_name == original_plugins_name
            assert event.get_extra("provider_request") is None
        finally:
            plugin._enforce_final_tool_policy = original_enforce
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


def test_pipeline_hook_early_exit_still_restores_event(tmp_path: Path) -> None:
    """OnLLMRequestEvent 早退路径：不执行 run，但 finally 仍恢复 event。"""

    async def scenario(plugin, main):
        from .host_stubs import FakeBuildResult, _FakeResetCoro

        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        original_plugins_name = ["other_plugin"]
        event.plugins_name = list(original_plugins_name)

        class Runner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return None

            def close(self):
                pass

        async def build_effect(kwargs, result):
            return FakeBuildResult(
                agent_runner=Runner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(original_runtime, build_effect=build_effect)
        original_hook = main.call_event_hook
        ran = []

        async def stop_hook(event_obj, event_type, *args, **kwargs):
            if event_type.name == "OnLLMRequestEvent":
                ran.append(True)
                return True  # 早退
            return await original_hook(event_obj, event_type, *args, **kwargs)

        main.call_event_hook = stop_hook
        try:
            state = plugin._state_for(UMO)
            token = plugin._gate.advance(UMO)
            result = await plugin._generate_reply_via_pipeline(
                UMO, state, expected_generation=token, force=True
            )
            assert ran == [True]
            assert result.text == ""
            assert result.direct_send_count == 0
            assert "send" not in event.__dict__
            assert event.plugins_name == original_plugins_name
            assert event.get_extra("provider_request") is None
        finally:
            main.call_event_hook = original_hook
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# generation 全局单调
# ============================================================================


def test_generation_is_monotonic_and_survives_whitelist_aba(tmp_path: Path) -> None:
    """移除白名单后旧任务 token 必须永远失效，重新加入不能复活旧任务。"""

    async def scenario(plugin, main):
        first = plugin._gate.advance(UMO)
        # 移除白名单：invalidate 推进代次，旧任务 token 从此失效
        plugin._replace_whitelist(set())
        # 重新加入：会话 token 继续增大
        plugin._replace_whitelist({UMO})
        after_readd = plugin._gate.advance(UMO)

        assert after_readd > first
        assert plugin._gate.is_current(UMO, first) is False
        # 旧任务在任何检查点都必须被拒绝
        assert plugin._gate.is_current(UMO, first - 1) is False

    with_plugin(tmp_path, scenario)


def test_generation_rejects_stale_expected_token(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        token = plugin._gate.advance(UMO)
        assert plugin._gate.is_current(UMO, token) is True
        plugin._gate.advance(UMO)
        assert plugin._gate.is_current(UMO, token) is False

    with_plugin(tmp_path, scenario)


# ============================================================================
# 配置态 / 运行态分离
# ============================================================================


async def _post_config(plugin, payload: dict) -> None:
    import sys

    web = sys.modules["astrbot.api.web"]
    web.request.payload = payload
    await plugin._api_post_config()


def test_runtime_override_survives_unrelated_config_post(tmp_path: Path) -> None:
    """临时 off 后保存无关配置，不得清除临时运行态。"""

    async def scenario(plugin, main):
        assert plugin.runtime_enabled is True
        assert plugin.settings.enabled is True

        # 临时关闭（/off 语义）
        plugin.runtime_enabled = False

        await _post_config(plugin, {"decision_temperature": 0.5})
        assert plugin.runtime_enabled is False
        assert plugin.settings.enabled is True

    with_plugin(tmp_path, scenario)


def test_persisted_enabled_change_resets_runtime_override(tmp_path: Path) -> None:
    """持久 enabled 真正变化时才清除临时覆盖。"""

    async def scenario(plugin, main):
        plugin.settings.enabled = False
        plugin.runtime_enabled = False  # 模拟临时 off

        await _post_config(plugin, {"enabled": True})  # 持久 false -> true，真正变化
        assert plugin.settings.enabled is True
        assert plugin.runtime_enabled is True

    with_plugin(tmp_path, scenario)


def test_repeated_same_enabled_post_keeps_runtime_override(tmp_path: Path) -> None:
    """重复提交相同 enabled 值不得影响临时运行态。"""

    async def scenario(plugin, main):
        plugin.runtime_enabled = False  # 临时 off，持久仍 true

        await _post_config(plugin, {"enabled": True})  # 持久 true -> true，无变化
        assert plugin.settings.enabled is True
        assert plugin.runtime_enabled is False  # 临时 off 保持

    with_plugin(tmp_path, scenario)


# ============================================================================
# spawn barrier 与状态回收
# ============================================================================


def test_track_background_task_barrier_after_stop(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin._stopping = True

        async def innocent():
            await asyncio.sleep(0)

        task = plugin._track_background_task(innocent())
        assert task is None
        # 延迟调度在停止后不得注册
        plugin._schedule_delayed_check(UMO, delay_sec=0, trigger="message_delay", force=False)
        assert UMO not in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def test_whitelist_remove_recycles_session_lock(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin._gate.lock_for(UMO)
        plugin._replace_whitelist(set())
        assert UMO not in plugin._session_locks

    with_plugin(tmp_path, scenario)


def test_gate_views_are_read_only_and_live(tmp_path: Path) -> None:
    """P2-24：SessionGate 只读视图——写抛错、读实时、语义不被绕过。"""

    async def scenario(plugin, main):
        # 写操作必须抛错（MappingProxyType / frozenset）
        with pytest.raises(TypeError):
            plugin._session_generation[UMO] = 1
        with pytest.raises(TypeError):
            plugin._session_locks[UMO] = asyncio.Lock()
        with pytest.raises(AttributeError):
            plugin._running_sessions.add(UMO)
        # 读正常且反映最新状态（实时视图）
        assert plugin._session_generation.get(UMO, 0) == 0
        plugin._gate.advance(UMO)
        assert plugin._session_generation[UMO] == 1
        plugin._gate.mark_running(UMO)
        assert UMO in plugin._running_sessions
        plugin._gate.unmark_running(UMO)
        assert UMO not in plugin._running_sessions
        lock = plugin._gate.lock_for(UMO)
        assert plugin._session_locks[UMO] is lock

    with_plugin(tmp_path, scenario)


def test_whitelist_remove_recycles_session_state(tmp_path: Path) -> None:
    """移出白名单后，会话状态（含 recent 历史）从内存回收，避免缓慢增长。"""

    async def scenario(plugin, main):
        state = plugin._state_for(UMO)
        state.recent.append("历史消息")
        assert UMO in plugin.sessions

        plugin._replace_whitelist(set())
        assert UMO not in plugin.sessions
        assert plugin.sessions.get(UMO) is None

    with_plugin(tmp_path, scenario)


def test_state_for_refreshes_daily_count_across_midnight(tmp_path: Path) -> None:
    """跨天后任意读取路径（status/持久化）都拿到当日计数。"""

    async def scenario(plugin, main):
        import time

        state = plugin._state_for(UMO)
        state.daily_key = "2000-01-01"  # 伪造昨天的日期
        state.daily_count = 7

        refreshed = plugin._state_for(UMO)
        assert refreshed is state
        assert state.daily_count == 0
        assert state.daily_key == time.strftime("%Y-%m-%d")

    with_plugin(tmp_path, scenario)


def test_terminate_clears_tasks_and_saves(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin._gate.advance(UMO)
        await plugin.terminate()
        assert plugin._stopping is True
        assert plugin._delay_tasks == {}
        assert plugin._last_events == {}
        assert (tmp_path / "data" / "astrbot_plugin_self_initiated_reply" / "state.json").exists()

    with_plugin(tmp_path, scenario)


def test_plugin_smoke_message_path(tmp_path: Path) -> None:
    """on_message 全流程冒烟：白名单消息进入调度，不抛异常。"""

    async def scenario(plugin, main):
        event = _make_event(message_str="今天天气不错")
        await plugin.on_message(event)
        assert UMO in plugin._last_events
        assert UMO in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def _fresh_state(plugin) -> Any:
    """构造一个闸门判定所需的干净会话状态。"""
    state = plugin._state_for(UMO)
    state.recent.clear()
    state.last_active_at = 0.0
    state.last_proactive_at = 0.0
    state.last_proactive_observed_at = 0.0
    state.daily_count = 0
    return state


def test_ask_decision_model_provider_failure_returns_clear_reason(tmp_path: Path) -> None:
    """判断模型 Provider 解析失败时返回明确 reason，不得抛异常或误导。"""

    async def scenario(plugin, main):
        class BrokenBridge:
            async def resolve_provider_id(self, _umo, _preferred):
                raise RuntimeError("provider registry down")

        plugin.settings.decision_model_enabled = True
        plugin.bridge = BrokenBridge()
        state = _fresh_state(plugin)
        result = await plugin._decision.ask_decision_model(UMO, state, trigger="message_delay")
        assert result["should_reply"] is False
        assert result["reason"] == "判断模型解析失败"

    with_plugin(tmp_path, scenario)


def test_command_group_and_help_are_admin_gated() -> None:
    """selfreply 命令组入口与全部子命令都必须带 ADMIN 权限门（源码护栏）。"""
    source = Path(__file__).resolve().parents[1] / "main.py"
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()

    group_index = next(
        i for i, line in enumerate(lines) if '@filter.command_group("selfreply")' in line
    )
    # 组入口：permission_type 必须在 command_group 内层（下方一行）。真实宿主的
    # permission_type 会访问被装饰对象的 __name__，RegisteringCommandable 没有，
    # 因此放外层（上方）会在加载时抛 AttributeError（0.7.15 线上事故）。
    assert lines[group_index + 1].strip() == "@permission_type(PermissionType.ADMIN)", (
        f"行 {group_index + 1} 的 command_group 缺少内层 ADMIN 权限门"
    )

    subcommand_indexes = [i for i, line in enumerate(lines) if ".command(" in line]
    assert len(subcommand_indexes) >= 9, "9 个子命令都应找到"
    for index in subcommand_indexes:
        # 子命令：permission_type 在命令装饰器正上方一行（函数形态，顺序安全）
        assert lines[index - 1].strip() == "@permission_type(PermissionType.ADMIN)", (
            f"行 {index + 1} 的命令缺少 ADMIN 权限门: {lines[index].strip()}"
        )


def test_whitelist_remove_recycles_legacy_group_key(tmp_path: Path) -> None:
    """移出白名单同时回收 legacy 裸群号 key 下的旧状态。"""

    async def scenario(plugin, main):
        legacy_key = main.session_group_id(UMO)
        assert legacy_key
        plugin.sessions[legacy_key] = plugin._state_for(UMO)
        plugin._replace_whitelist(set())
        assert UMO not in plugin.sessions
        assert legacy_key not in plugin.sessions

    with_plugin(tmp_path, scenario)


def test_force_check_prunes_session_state(tmp_path: Path) -> None:
    """非白名单会话手动 check 后 sessions 条目必须回收（0.8.8 单点化）。

    此前 _prune_session 只清代次/锁/运行标记与 _last_decisions，sessions 里
    的 SessionState（含 recent 历史）会随手动 check 的会话数累积。
    """

    async def scenario(plugin, main):
        original_check = plugin._check_session

        async def fake_check(*args, **kwargs):
            return "完成"

        plugin._check_session = fake_check
        try:
            other = "fake:group:999"
            plugin._state_for(other)  # 模拟 check 流程已建会话状态
            assert other in plugin.sessions
            event = _make_event(umo=other)
            await plugin._command_text(event, "check")
            assert other not in plugin.sessions
            assert plugin.sessions.get(other) is None
        finally:
            plugin._check_session = original_check

    with_plugin(tmp_path, scenario)


def test_version_consistency_across_metadata() -> None:
    """models / metadata.yaml / pyproject.toml / 双语 README 五源版本必须一致。

    0.8.8 起 pyproject.toml 纳入守卫：0.8.7 发布时 pyproject 漏在守卫之外，
    导致 wheel 文件名与 dist-info 版本停留在 0.8.3（实测实锤），面板显示
    0.8.7 而 pip 记录 0.8.3。
    """
    root = Path(__file__).resolve().parents[1]
    models = (root / "models.py").read_text(encoding="utf-8")
    metadata = (root / "metadata.yaml").read_text(encoding="utf-8")

    match = re.search(r'PLUGIN_VERSION = "([^"]+)"', models)
    assert match is not None
    version = match.group(1)
    assert f"version: {version}" in metadata
    # pyproject 版本（3.10 无 tomllib，用 tomli 兼容；dev 依赖已声明）
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py3.10 兼容分支
        import tomli as tomllib  # type: ignore[no-redef]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == version, (
        f"pyproject.toml version={pyproject['project']['version']} 与 PLUGIN_VERSION={version} 不一致，"  # noqa: E501
        "wheel 文件名与 dist-info 会停留在旧版本"
    )
    # README 版本号由 shields 徽章承载（0.7.22 起，原「当前版本」行随 README 重写移除）。
    # 0.9.3 起 README.en.md（234 行精确镜像）并入 README.md 的英文摘要节，
    # 单一 README 单一徽章，不再有双语同步义务。
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert f"-{version}-" in readme, "README.md badge 版本与 PLUGIN_VERSION 不一致"
    # 宿主下限声明保持一致
    assert '">=4.23.3,<5"' in metadata


def test_ci_docstring_gate_comment_matches_real_thresholds() -> None:
    """ci.yml 里描述 docstring 门禁的注释必须与脚本常量一致。

    0.9.3 收尾实测缺陷：注释写「CC>=16」而 `docstring_gates.py` 的
    `MIN_CC_REQUIRING_DOC` 实际是 12。这类注释漂移不会让门禁失效，但会让
    读 CI 的人以为 12~15 的函数不必写 docstring，从而在 review 时放过本该
    补文档的函数——注释是唯一的对外口径，必须锁在常量上。
    """
    root = Path(__file__).resolve().parents[1]
    gates = (root / "scripts" / "docstring_gates.py").read_text(encoding="utf-8")
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    lines_match = re.search(r"^MAX_LINES_WITHOUT_DOC\s*=\s*(\d+)", gates, re.MULTILINE)
    cc_match = re.search(r"^MIN_CC_REQUIRING_DOC\s*=\s*(\d+)", gates, re.MULTILINE)
    assert lines_match and cc_match, "docstring_gates.py 的阈值常量名已变，本守卫需同步"
    max_lines, min_cc = lines_match.group(1), cc_match.group(1)

    # 注释里同时出现行数与 CC 两个口径，二者都必须等于常量真值
    assert f">{max_lines} 行" in ci, (
        f"ci.yml 注释的行数阈值与 MAX_LINES_WITHOUT_DOC={max_lines} 不一致"
    )
    assert f"CC>={min_cc}" in ci, f"ci.yml 注释的复杂度阈值与 MIN_CC_REQUIRING_DOC={min_cc} 不一致"


# ============================================================================
# UI 主题偏好持久化（iframe 下 localStorage 不可用，走后端 ui/theme）
# ============================================================================


def test_ui_theme_defaults_to_auto(tmp_path: Path) -> None:
    """未设置时 GET ui/theme 返回 auto。"""

    async def scenario(plugin, main):
        cfg = await plugin._api_get_ui_theme()
        assert cfg == {"ok": True, "theme": "auto"}

    with_plugin(tmp_path, scenario)


def test_ui_theme_persists_across_instances(tmp_path: Path) -> None:
    """主题写入后端文件后，新实例（模拟页面刷新）仍能恢复。"""

    async def scenario(plugin, main):
        import sys

        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"theme": "light"}
        result = await plugin._api_post_ui_theme()
        assert result == {"ok": True, "theme": "light"}
        # 文件已落盘
        prefs_path = plugin._ui_prefs_path
        assert prefs_path.exists()
        assert '"theme": "light"' in prefs_path.read_text(encoding="utf-8")
        # 非法主题被拒且不改变状态
        web.request.payload = {"theme": "blue"}
        bad = await plugin._api_post_ui_theme()
        assert bad.get("ok") is False
        assert plugin._ui_theme == "light"

    with_plugin(tmp_path, scenario)

    # 同一数据目录新建实例：模拟刷新/重启后主题仍在（iframe 场景的关键）
    async def reopen(plugin, main):
        cfg = await plugin._api_get_ui_theme()
        assert cfg == {"ok": True, "theme": "light"}

    with_plugin(tmp_path, reopen)
