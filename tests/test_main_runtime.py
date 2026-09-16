"""main.py 行为测试：插件实例上的主链与闸门。

这些测试真实导入并实例化 SelfInitiatedReplyPlugin，验证：
- 工具 fail closed 与共享 platform_meta 零写入
- generation 全局单调（白名单 ABA）
- UNKNOWN 发送语义（不重试 / 消耗配额 / 不写历史 / 不触发 after-send hook）
- 配置态与运行态分离（临时开关不被无关保存清除）
- terminate 后 spawn barrier
- 会话锁回收
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from .host_stubs import (
    FakeToolSet,
    PipelineTestAdapter,
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
    """会话回收必须同步清理调试面板的最近裁决，防同形态泄漏。"""

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
    """跳过决策（静默不足等早退原因）也进入调试面板。"""

    class _FakeDecision:
        async def decide(self, umo, state, *, trigger, force):
            return "静默时间不足：1s / 60s。"

    async def scenario(plugin, main):
        umo = UMO
        state = plugin._state_for(umo)
        plugin._decision = _FakeDecision()
        pipeline = importlib.import_module(main.__package__ + ".session_pipeline")
        result = await pipeline.decide_session_reply(
            plugin._decision,
            plugin._gate,
            plugin._last_decisions,
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

        state = plugin._generation.install_agent_tool_boundary(event, False)
        assert event.plugins_name == []
        assert event.platform_meta.support_proactive_message is True

        plugin._generation.restore_agent_tool_boundary(event, state)
        assert event.plugins_name == original_plugins_name

    with_plugin(tmp_path, scenario)


def test_inherit_tools_mode_keeps_plugin_names_and_skips_policy(tmp_path: Path) -> None:
    """开关开启时：主动运行不清空插件工具边界，最终工具集也不清理。"""

    async def scenario(plugin, main):
        event = _make_event()
        event.plugins_name = ["stealer", "living_memory"]

        state = plugin._generation.install_agent_tool_boundary(event, True)
        assert state == {}
        assert event.plugins_name == ["stealer", "living_memory"]

        tool_set = FakeToolSet()
        tool_set.add_tool(type("T", (), {"name": "stealer_fetch"})())
        req = type("Req", (), {"func_tool": tool_set})()
        assert plugin._generation.enforce_final_tool_policy(req, True) is True
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
        assert plugin._generation.enforce_final_tool_policy(bad_req, False) is False

        tool_set = FakeToolSet()
        tool_set.add_tool(type("T", (), {"name": "send_message_to_user"})())
        clean_req = type("Req", (), {"func_tool": tool_set})()
        assert plugin._generation.enforce_final_tool_policy(clean_req, False) is True
        assert tool_set.tools == []

    with_plugin(tmp_path, scenario)


def test_pipeline_injects_tools_and_enforces_policy_twice(tmp_path: Path) -> None:
    """核心管线集成：build 注入工具 → 两次 enforce 清空（含 hook 注入）→ run 期间
    tool_direct 直发被计数/抑制 → finally 恢复 event.send 与 plugins_name。"""

    async def scenario(plugin, main):
        from .host_stubs import _FakeMessageChain, install_tool_injecting_pipeline

        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        original_plugins_name = ["other_plugin"]
        event.plugins_name = list(original_plugins_name)

        def run_effect(runner, **_kwargs):
            async def gen():
                # 预算内 2 次直发 + 1 次超预算（MAX_DIRECT_TOOL_SENDS = 2）
                for i in range(3):
                    await event.send(
                        _FakeMessageChain(type="tool_direct_result", chain=[f"直发{i}"])
                    )
                    yield None

            return gen()

        ctrl = install_tool_injecting_pipeline(plugin, main, event=event, run_effect=run_effect)
        enforce_tool_snapshots = ctrl["enforce_snapshots"]
        try:
            state = plugin._state_for(UMO)
            token = plugin._gate.advance(UMO)
            result = await plugin._generation.generate(
                UMO, state, expected_generation=token, force=True
            )

            # 两次 enforce 都执行且都清空（hook 注入的工具也被第二次清掉）
            assert len(enforce_tool_snapshots) == 2
            assert enforce_tool_snapshots[0] == []
            assert enforce_tool_snapshots[1] == []
            # run 结束时 req.func_tool 保持为空
            assert main._AGENT_RUNTIME.final_tool_ids(ctrl["req_holder"]["req"]) == []
            # 直发计数：前 2 次被接受，第 3 次超预算抑制
            assert result.direct_send_count == 2
            assert len(result.direct_texts) == 2
            assert result.text == "你好呀"
            # finally 恢复：实例 send 已清除（回到类级 send），plugins_name 复原
            assert "send" not in event.__dict__
            assert event.plugins_name == original_plugins_name
            assert event.get_extra("provider_request") is None
        finally:
            ctrl["restore"]()

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
        main._AGENT_RUNTIME = PipelineTestAdapter(original_runtime, build_effect=build_effect)
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
            result = await plugin._generation.generate(
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
        plugin._whitelist.replace(set())
        # 重新加入：会话 token 继续增大
        plugin._whitelist.replace({UMO})
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


def test_off_persists_enabled_across_restart(tmp_path: Path) -> None:
    """决策 5 红线：``/off`` 必须落盘，重启后仍是关闭。

    改持久前 ``/off`` 只改内存 ``runtime_enabled``，宿主一重启就回落到持久
    ``enabled=True``——用户打完 ``/off`` 以为「别再主动说话了」，重启后插件又
    开始发言，而用户不会知道要再打一次。这是静默违背用户意图。

    变异锚定：把 ``_persist_enabled`` 里的 ``self.settings.enabled = enabled``
    删掉（退回只改 runtime），第二条断言即红（磁盘仍是 True）；再把整个
    ``_persist_enabled`` 换回 ``self.runtime_enabled = False``，第三条断言
    （重建插件后仍关闭）也红。断言磁盘原文与重建后的实例，不只断言内存字段。
    """
    import json

    async def scenario(plugin, main):
        assert plugin.settings.enabled is True

        text = await plugin._command_text(_make_event(), "off")

        assert "已暂停" in text
        assert plugin.runtime_enabled is False
        assert plugin.settings.enabled is False
        # 磁盘原文：光看内存字段无法区分「已落盘」与「只改了内存」
        on_disk = json.loads(plugin._config_path.read_text(encoding="utf-8"))
        assert on_disk["enabled"] is False
        return plugin._config_path

    config_path = with_plugin(tmp_path, scenario)

    # 模拟宿主重启：全新实例从同一配置文件读起，必须仍是关闭
    async def after_restart(plugin, main):
        assert plugin.settings.enabled is False
        assert plugin.runtime_enabled is False

    assert json.loads(config_path.read_text(encoding="utf-8"))["enabled"] is False
    with_plugin(tmp_path, after_restart)


def test_off_rolls_back_memory_when_config_write_fails(tmp_path: Path) -> None:
    """落盘失败必须内存回滚，不留「内存已关、磁盘仍开」的中间态（§6 同一纪律）。

    不回滚的话：磁盘写失败但内存已关，插件当场静默，重启后又按磁盘的 True
    复活——用户看到的是「关了一会儿自己又开了」，且没有任何错误抵达用户。
    这里断言异常上抛 + 两个内存字段都回到原值。
    """

    async def scenario(plugin, main):
        original_persist = plugin._persist_config

        async def failing_persist():
            raise OSError("配置文件写入失败（模拟）")

        plugin._persist_config = failing_persist
        try:
            with pytest.raises(OSError):
                await plugin._command_text(_make_event(), "off")
            # 回滚后内存两个字段都必须回到开启
            assert plugin.settings.enabled is True
            assert plugin.runtime_enabled is True
        finally:
            plugin._persist_config = original_persist

    with_plugin(tmp_path, scenario)


def test_on_persists_enabled_across_restart(tmp_path: Path) -> None:
    """``/on`` 同样落盘：否则 ``/off`` 持久化后就再也开不回来（跨重启）。"""
    import json

    async def scenario(plugin, main):
        await plugin._command_text(_make_event(), "off")
        text = await plugin._command_text(_make_event(), "on")

        assert "已启用" in text
        assert plugin.settings.enabled is True
        on_disk = json.loads(plugin._config_path.read_text(encoding="utf-8"))
        assert on_disk["enabled"] is True

    with_plugin(tmp_path, scenario)


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


def test_degraded_state_rejects_new_spawn_and_force_check(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin._mark_degraded("stubborn runner")
        assert plugin.lifecycle_state == "DEGRADED"

        async def innocent() -> None:
            await asyncio.sleep(0)

        assert plugin._track_background_task(innocent()) is None
        result = await plugin._pipeline.check_session(
            UMO,
            trigger="manual",
            force=True,
            expected_generation=None,
        )
        assert result == "插件未启用。"
        event = _make_event()
        plugin._last_events[UMO] = event
        # check 的拒绝文案必须点明降级原因：降级不是"未启用"，
        # 说错会让运营去改配置而不是重启插件。
        assert await plugin._command_text(event, "check") == (
            "插件已降级，无法手动检查（需重启插件恢复）。"
        )
        assert plugin._last_events[UMO] is event

    with_plugin(tmp_path, scenario)


async def _drive_decorated(plugin, event, handler: str) -> str:
    """直接驱动一条 ``@selfreply.command`` 装饰器出口，取回显文本。

    host_stubs 的 ``@selfreply.command`` 是 passthrough，故被装饰的 async
    generator 保持原样可调用。这条 helper 只为覆盖装饰器路径：内联指令走
    ``_handle_inline_command``，两条出口的文本都在 ``_command_text`` 处取出，
    漏一处不会被另一处发现。
    """
    texts = []
    async for result in getattr(plugin, handler)(event):
        texts.append(getattr(result, "text", "") or str(result))
    return "\n".join(texts)


async def _drive_decorated_status(plugin, event) -> str:
    return await _drive_decorated(plugin, event, "selfreply_status")


def _decorated_command_handlers() -> dict[str, str]:
    """``main.py`` 里 ``@selfreply.command`` 的注册表：canonical 动作 → 属性名。

    从源码取而不是猜 ``selfreply_<动作>`` 命名约定、也不是硬编码 9 个名字：
    注册表是事实，重命名后本表自动跟上。指令组本体（``selfreply``）不在表内
    也不可驱动——宿主装饰器已把该属性换成 RegisteringCommandable，真实宿主与
    host_stubs 都是如此（见 compat_check 的 EXPECTED_HANDLER_COUNT）。
    """
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                continue
            found[decorator.args[0].value] = node.name
    return found


def test_decorated_commands_delegate_to_the_shared_dispatch(tmp_path: Path) -> None:
    """9 个子指令出口一律经 ``_command_text`` 并传自己的 canonical 动作名。

    装饰器出口在生产不可达（priority=1000 的内联拦截先返回），于是它的形状漂移
    无人可见：``selfreply_list`` 曾经直连 ``list_text()`` 而不经分派，把它的参数
    写成 ``"status"`` 也不会有任何红灯。这里逐个驱动并断言分派参数与回显文本。

    用行为记录器而不是源码匹配：断言的是"它调了谁、传了什么、回显了什么"，
    与写法（直连 / 委托 / 再加一层）无关。只读动作另有出口文本等价断言。
    """
    handlers = _decorated_command_handlers()
    assert len(handlers) == 9, f"main.py 里解析出 {len(handlers)} 个子指令（期望 9）"

    async def scenario(plugin, main):
        event = _make_event(umo=UMO)
        original = plugin._command_text
        calls: list[tuple[str, str]] = []

        async def recorder(_event, action, arg=""):
            calls.append((action, arg))
            return f"<{action}>"

        plugin._command_text = recorder
        try:
            for action, handler in sorted(handlers.items()):
                text = await _drive_decorated(plugin, event, handler)
                assert calls == [(action, "")], f"{handler} 没以 {action!r} 走单源分派：{calls}"
                assert text == f"<{action}>", f"{handler} 没原样回显分派文本：{text!r}"
                calls.clear()
        finally:
            plugin._command_text = original

    with_plugin(tmp_path, scenario)


def test_decorated_readonly_commands_match_inline_dispatch_text(tmp_path: Path) -> None:
    """只读子指令的两条出口必须取出同一份文本。

    help/status/list/debug 不改状态，故同一状态下两侧文本应当逐字相等：任一侧
    少传一个参数（lifecycle / last_decision / ignored_sender）都会分叉。
    写动作（add/remove/on/off/check）不做等价断言——它们有副作用，第二次调用的
    文案必然不同（如 add 第二次说"已在白名单中"），那样的断言会因状态耦合而假绿。
    """
    handlers = _decorated_command_handlers()

    async def scenario(plugin, main):
        event = _make_event(umo=UMO)
        for action in ("help", "status", "list", "debug"):
            inline = await plugin._command_text(event, action)
            decorated = await _drive_decorated(plugin, event, handlers[action])
            assert decorated == inline, f"{action} 两条出口文本不一致：{decorated!r} != {inline!r}"

    with_plugin(tmp_path, scenario)


def test_status_debug_do_not_create_session_state(tmp_path: Path) -> None:
    """status/debug 是只读指令：非白名单会话不得隐式创建并滞留内存状态条目。

    缺陷：两条出口都用 `_state_for` 组装参数，对不存在 key 创建并滞留
    SessionState，只有 `_prune_session` 能回收。写盘侧会过滤（不会复活），
    但只读指令留下永久内存条目与该过滤心智模型不一致。
    """

    async def scenario(plugin, main):
        import sys

        utils = sys.modules[f"{main.__package__}.utils"]
        event = _make_event(umo="fake:group:999")
        key = utils.whitelist_storage_key(utils.event_umo(event))
        assert key not in plugin.sessions

        text = await plugin._command_text(event, "status")
        assert "主动回复状态" in text
        assert key not in plugin.sessions, "status 隐式创建了会话状态"

        decorated = await _drive_decorated_status(plugin, event)
        assert "主动回复状态" in decorated
        assert key not in plugin.sessions, "装饰器 status 隐式创建了会话状态"

        debug_text = await plugin._command_text(event, "debug")
        assert "主动回复调试信息" in debug_text
        assert key not in plugin.sessions, "debug 隐式创建了会话状态"

    with_plugin(tmp_path, scenario)


def test_degraded_lifecycle_is_visible_in_status_and_add_message(tmp_path: Path) -> None:
    """降级态必须能从 /status 与 /selfreply status 看出，且 add 报错文案不误导。

    回归：一次生成超时 + 宿主吞取消即永久 DEGRADED，但 status 仍显示"运行中:
    True"、GET /status 不含 lifecycle、add 报"插件正在关闭"（根本没在关闭）。
    """

    async def scenario(plugin, main):
        webapi = importlib.import_module(main.__package__ + ".webapi")

        # 正常态：lifecycle 可见且为 RUNNING。
        status = await webapi._api_status(plugin)
        assert status["lifecycle"] == "RUNNING"

        plugin._mark_degraded("stubborn runner")

        # /status 端点暴露降级态。
        status = await webapi._api_status(plugin)
        assert status["lifecycle"] == "DEGRADED"

        # /selfreply status 文本不再谎报"运行中: True"，而是点明降级。
        event = _make_event()
        text = await plugin._command_text(event, "status")
        assert "已降级" in text
        assert "运行中: True" not in text

        # 装饰器命令是另一条独立出口，必须同样可见：它曾漏传 lifecycle，
        # 靠 status_text 的默认值静默回落 RUNNING，被上面那条内联断言掩盖。
        decorated = await _drive_decorated_status(plugin, event)
        assert "已降级" in decorated, "装饰器 /selfreply status 仍谎报正常"
        assert "运行中: True" not in decorated

        # add 的拒绝文案不得说"正在关闭"（误导：根本没在关闭）。
        try:
            await plugin._add_whitelist_session("fake:group:999")
        except RuntimeError as exc:
            assert "正在关闭" not in str(exc)
            assert "已降级" in str(exc)
        else:
            raise AssertionError("降级态下 add 应被拒绝")

    with_plugin(tmp_path, scenario)


def test_status_recent_decision_line_comes_from_last_decisions(tmp_path: Path) -> None:
    """/selfreply status 的「最近裁决」行必须来自本会话的最近裁决（README 排障指引）。

    两条出口（内联 dispatch 与装饰器命令）都要显式传参：漏传一处只会让那条出口
    静默显示「暂无记录」，排障时反而以为插件从没裁决过。
    """

    async def scenario(plugin, main):
        commands = importlib.import_module(main.__package__ + ".commands")
        utils = importlib.import_module(main.__package__ + ".utils")
        event = _make_event(umo=UMO)
        umo = utils.event_umo(event)

        # 还没裁决过：必须明说「暂无记录」，而不是省略这一行。
        text = await plugin._command_text(event, "status")
        assert "最近裁决: 暂无记录" in text
        assert "最近裁决: 暂无记录" in await _drive_decorated_status(plugin, event)

        plugin._last_decisions[umo] = {
            "at": 1_700_000_000.0,
            "trigger": "manual",
            "should_reply": False,
            "reason": "判断不回复：这条消息不需要接话",
        }
        text = await plugin._command_text(event, "status")
        assert "不接话" in text
        assert "这条消息不需要接话" in text
        decorated = await _drive_decorated_status(plugin, event)
        assert "不接话" in decorated, "装饰器 /selfreply status 没传最近裁决"

        # 原因可能是多行自由文本（模型 JSON / 异常文本），回显必须折单行并截断。
        plugin._last_decisions[umo]["reason"] = "首行\n次行 " + "很长的理由" * 30
        line = commands.recent_decision_line(plugin._last_decisions[umo])
        assert line.count("\n") == 0
        assert line.endswith("…")
        assert len(line.split(" · ", 1)[1]) <= 60

    with_plugin(tmp_path, scenario)


def test_terminate_quarantines_noncooperative_runner(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        release = asyncio.Event()

        async def noncooperative() -> None:
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    continue

        task = plugin._track_background_task(noncooperative())
        assert task is not None
        await asyncio.sleep(0)
        original_timeout = getattr(main, "TERMINATE_TASK_TIMEOUT_SEC", None)
        main.TERMINATE_TASK_TIMEOUT_SEC = 0.02
        termination = asyncio.create_task(plugin.terminate())
        try:
            await asyncio.sleep(0.08)
            assert termination.done()
            assert plugin.lifecycle_state == "DEGRADED"
            assert task in plugin._quarantined_tasks
        finally:
            release.set()
            if not termination.done():
                await asyncio.wait_for(termination, timeout=1)
            await asyncio.wait_for(task, timeout=1)
            if original_timeout is None:
                del main.TERMINATE_TASK_TIMEOUT_SEC
            else:
                main.TERMINATE_TASK_TIMEOUT_SEC = original_timeout

    with_plugin(tmp_path, scenario)


def test_quarantine_capacity_closes_spawn_barrier(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        tasks = [
            asyncio.create_task(asyncio.sleep(3600)) for _ in range(main.MAX_QUARANTINED_TASKS)
        ]
        plugin._quarantined_tasks.update({task: "test capacity" for task in tasks})
        try:
            assert plugin._can_start_tasks() is False
            assert plugin._track_background_task(asyncio.sleep(0)) is None
        finally:
            plugin._quarantined_tasks.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    with_plugin(tmp_path, scenario)


def test_track_background_task_barrier_after_stop(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin._stopping = True

        async def innocent():
            await asyncio.sleep(0)

        task = plugin._track_background_task(innocent())
        assert task is None
        # 延迟调度在停止后不得注册
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=0, trigger="message_delay", force=False
        )
        assert UMO not in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def test_terminate_quarantines_stuck_final_save(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        release = asyncio.Event()
        original_save = plugin._save_storage
        original_timeout = getattr(main, "TERMINATE_TASK_TIMEOUT_SEC", None)

        async def stuck_save() -> None:
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    continue

        plugin._save_storage = stuck_save
        main.TERMINATE_TASK_TIMEOUT_SEC = 0.02
        termination = asyncio.create_task(plugin.terminate())
        try:
            await asyncio.sleep(0.08)
            assert termination.done()
            assert plugin.lifecycle_state == "DEGRADED"
            assert any(
                "final state save deadline exceeded" in reason
                for reason in plugin._quarantined_tasks.values()
            )
        finally:
            release.set()
            plugin._save_storage = original_save
            if not termination.done():
                await asyncio.wait_for(termination, timeout=1)
            for task in list(plugin._quarantined_tasks):
                await asyncio.wait_for(task, timeout=1)
            if original_timeout is None:
                del main.TERMINATE_TASK_TIMEOUT_SEC
            else:
                main.TERMINATE_TASK_TIMEOUT_SEC = original_timeout

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


def test_whitelist_remove_recycles_gate_state(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin._gate.advance(UMO)
        plugin._gate.lock_for(UMO)
        plugin._gate.mark_running(UMO)
        plugin._whitelist.replace(set())
        assert UMO not in plugin._session_generation
        assert UMO not in plugin._session_locks
        assert UMO not in plugin._running_sessions

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

        plugin._whitelist.replace(set())
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


def test_write_commands_require_admin_and_admins_can_run_them(tmp_path: Path) -> None:
    """写指令看的是管理员判定，不是装饰器写在哪一行。"""

    async def scenario(plugin, main):
        other = "fake:group:999"
        before = set(plugin.settings.whitelist)
        denied = _make_event(umo=other, message_str="/selfreply add", is_admin=False)
        await plugin._handle_inline_command(denied, ("add", ""))
        assert any("没有权限" in text for text in denied.sent_texts)
        assert set(plugin.settings.whitelist) == before
        assert other not in plugin.settings.whitelist

        allowed = _make_event(umo=other, message_str="/selfreply add", is_admin=True)
        await plugin._handle_inline_command(allowed, ("add", ""))
        assert other in plugin.settings.whitelist
        assert allowed.sent_texts
        assert all("没有权限" not in text for text in allowed.sent_texts)

    with_plugin(tmp_path, scenario)


def test_command_group_keeps_permission_type_inner() -> None:
    """command_group 必须包住内层 ADMIN 门，外层会在宿主加载时 AttributeError。"""
    import ast

    from .source_contract import module_ast

    tree = module_ast("main.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != "selfreply":
            continue
        names = [ast.unparse(decorator) for decorator in node.decorator_list]
        group_index = next(i for i, name in enumerate(names) if "command_group" in name)
        perm_index = next(i for i, name in enumerate(names) if "permission_type" in name)
        assert "PermissionType.ADMIN" in names[perm_index]
        assert group_index < perm_index, (
            "permission_type 必须写在 command_group 内层，外层会在加载时崩"
        )
        return
    raise AssertionError("未找到 selfreply 命令组")


def test_whitelist_remove_recycles_legacy_group_key(tmp_path: Path) -> None:
    """移出白名单同时回收 legacy 裸群号 key 下的旧状态。"""

    async def scenario(plugin, main):
        legacy_key = main.session_group_id(UMO)
        assert legacy_key
        plugin.sessions[legacy_key] = plugin._state_for(UMO)
        plugin._whitelist.replace(set())
        assert UMO not in plugin.sessions
        assert legacy_key not in plugin.sessions

    with_plugin(tmp_path, scenario)


def test_force_check_prunes_session_state(tmp_path: Path) -> None:
    """非白名单会话手动 check 后 sessions 条目必须回收（0.8.8 单点化）。

    此前 _prune_session 只清代次/锁/运行标记与 _last_decisions，sessions 里
    的 SessionState（含 recent 历史）会随手动 check 的会话数累积。
    """

    async def scenario(plugin, main):
        original_check = plugin._pipeline.check_session

        async def fake_check(*args, **kwargs):
            return "完成"

        plugin._pipeline.check_session = fake_check
        try:
            other = "fake:group:999"
            plugin._state_for(other)  # 模拟 check 流程已建会话状态
            assert other in plugin.sessions
            event = _make_event(umo=other)
            await plugin._command_text(event, "check")
            assert other not in plugin.sessions
            assert plugin.sessions.get(other) is None
        finally:
            plugin._pipeline.check_session = original_check

    with_plugin(tmp_path, scenario)


def test_manual_check_records_sender_id(tmp_path: Path) -> None:
    """/selfreply check 写入的历史必须带发送者，不能落成空串。"""

    async def scenario(plugin, main):
        original_check = plugin._pipeline.check_session

        async def fake_check(*args, **kwargs):
            return "完成"

        plugin._pipeline.check_session = fake_check
        plugin.settings.whitelist.add(UMO)
        try:
            event = _make_event(
                umo=UMO,
                sender_id="sender-42",
                message_str="/selfreply check 测一下",
            )
            await plugin._command_text(event, "check", "测一下")
            state = plugin._state_for(UMO)
            assert state.recent, "check 未写入历史"
            assert state.recent[-1].sender_id == "sender-42"
        finally:
            plugin._pipeline.check_session = original_check

    with_plugin(tmp_path, scenario)


def test_version_consistency_across_metadata() -> None:
    """版本号的每一处对外载体都必须与 ``PLUGIN_VERSION`` 一致。

    载体：metadata.yaml、pyproject（经 hatchling 从 models.py 取值）、
    README 徽章、CHANGELOG 最新发布行。README 自 0.9.3 起单语单徽章
    （原 README.en.md 已并入英文摘要节），不再是"双语五源"。

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
    # pyproject 版本（requires-python >= 3.12，tomllib 恒可用）
    import tomllib

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    # pyproject 不再写死版本号，改由 hatchling 从 models.py 读取。
    # 故这里不再比对两处字面量（已无第二处），改为核验**取值机制真的能取到值**：
    # 若 path 指错文件或 pattern 与常量名不再匹配，hatchling 构建期会失败——
    # 但那要等到 CI 的 build 作业，而本用例让它在 test 作业就红。
    assert "version" in pyproject["project"].get("dynamic", []), (
        "pyproject 未声明 dynamic = ['version']：若同时也没有静态 version，构建会失败"
    )
    assert "version" not in pyproject["project"], (
        "pyproject 同时存在静态 version 与 dynamic 声明——版本号又出现第二处字面量"
    )
    hatch_version = pyproject["tool"]["hatch"]["version"]
    source_file = root / hatch_version["path"]
    assert source_file.exists(), (
        f"[tool.hatch.version].path 指向不存在的文件：{hatch_version['path']}"
    )
    extracted = re.search(hatch_version["pattern"], source_file.read_text(encoding="utf-8"))
    assert extracted is not None, (
        f"[tool.hatch.version].pattern 在 {hatch_version['path']} 里匹配不到版本号，"
        f"hatchling 构建会失败（pattern={hatch_version['pattern']!r}）"
    )
    assert extracted.group("version") == version, (
        f"hatchling 会取到 {extracted.group('version')!r}，而 PLUGIN_VERSION={version!r}——"
        f"pattern 命中了错误的位置"
    )
    # README 版本号由 shields 徽章承载（0.7.22 起，原「当前版本」行随 README 重写移除）。
    # 0.9.3 起 README.en.md（234 行精确镜像）并入 README.md 的英文摘要节，
    # 单一 README 单一徽章，不再有双语同步义务。
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert f"-{version}-" in readme, "README.md badge 版本与 PLUGIN_VERSION 不一致"
    # CHANGELOG 的最新发布版本行必须与 PLUGIN_VERSION 一致：两者都是"对外宣称的
    # 当前版本"，此前无守卫，发版时漏写 changelog 会让用户看到的版本说明停留在上一版。
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    released = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
    assert released is not None, "CHANGELOG.md 里找不到形如 '## [x.y.z]' 的发布版本行"
    assert released.group(1) == version, (
        f"CHANGELOG 最新发布版本 {released.group(1)!r} 与 PLUGIN_VERSION={version!r} 不一致"
    )
    # 宿主下限声明保持一致
    assert '">=4.23.3,<5"' in metadata


# ============================================================================
# UI 主题偏好持久化（iframe 下 localStorage 不可用，走后端 ui/theme）
# ============================================================================


def test_ui_theme_defaults_to_auto(tmp_path: Path) -> None:
    """未设置时 GET ui/theme 返回 auto。"""

    async def scenario(plugin, main):
        cfg = await plugin._api_get_ui_theme()
        assert cfg == {"ok": True, "theme": "auto", "dim": False, "bold": False}

    with_plugin(tmp_path, scenario)


def test_ui_theme_loads_from_bom_file(tmp_path: Path) -> None:
    """带 BOM 的 ui_prefs.json 必须能读取（与状态文件的 utf-8-sig 口径一致）。

    缺陷：``_load_ui_prefs`` 用 utf-8 读，BOM 会让 json.loads 失败回退默认，
    历史文件/外部编辑器产物即丢用户偏好。写侧由本插件原子写（无 BOM），
    本用例守读侧容错。
    """

    async def scenario(plugin, main):
        import sys

        plugin._ui_prefs_path.write_bytes(
            b"\xef\xbb\xbf" + b'{"theme": "dark", "dim": true, "bold": true}'
        )
        webapi = sys.modules[f"{main.__package__}.webapi"]
        theme, dim, bold = webapi.load_ui_prefs(plugin)
        assert (theme, dim, bold) == ("dark", True, True)

    with_plugin(tmp_path, scenario)


def test_ui_theme_persists_across_instances(tmp_path: Path) -> None:
    """主题写入后端文件后，新实例（模拟页面刷新）仍能恢复。"""

    async def scenario(plugin, main):
        import sys

        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"theme": "light"}
        result = await plugin._api_post_ui_theme()
        assert result == {"ok": True, "theme": "light", "dim": False, "bold": False}
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
        assert cfg == {"ok": True, "theme": "light", "dim": False, "bold": False}

    with_plugin(tmp_path, reopen)


def test_ui_dim_bold_survive_theme_only_post_and_reopen(tmp_path: Path) -> None:
    """压暗/粗体写入 ui_prefs；只改主题不得抹掉它们。"""

    async def scenario(plugin, main):
        import sys

        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"dim": True, "bold": True}
        result = await plugin._api_post_ui_theme()
        assert result == {"ok": True, "theme": "auto", "dim": True, "bold": True}
        web.request.payload = {"theme": "dark"}
        result = await plugin._api_post_ui_theme()
        assert result == {"ok": True, "theme": "dark", "dim": True, "bold": True}
        saved = json.loads(plugin._ui_prefs_path.read_text(encoding="utf-8"))
        assert saved == {"theme": "dark", "dim": True, "bold": True}
        web.request.payload = {"dim": "yes"}
        bad = await plugin._api_post_ui_theme()
        assert bad.get("ok") is False
        assert "yes" not in str(bad.get("error", ""))
        assert plugin._ui_dim is True

    with_plugin(tmp_path, scenario)

    async def reopen(plugin, main):
        cfg = await plugin._api_get_ui_theme()
        assert cfg == {"ok": True, "theme": "dark", "dim": True, "bold": True}

    with_plugin(tmp_path, reopen)


PRIVATE_UMO = "qq:FriendMessage:user-1"


def test_on_message_skips_private_when_disabled(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.whitelist.add(PRIVATE_UMO)
        plugin.settings.enabled_private_sessions = False
        await plugin.on_message(_make_event(umo=PRIVATE_UMO, message_str="今天天气不错"))
        assert PRIVATE_UMO not in plugin._last_events
        assert PRIVATE_UMO not in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def test_on_message_still_schedules_group_when_private_disabled(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.enabled_private_sessions = False
        await plugin.on_message(_make_event(message_str="今天天气不错"))
        assert UMO in plugin._last_events
        assert UMO in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def test_on_message_schedules_private_when_enabled(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.whitelist.add(PRIVATE_UMO)
        plugin.settings.enabled_private_sessions = True
        await plugin.on_message(_make_event(umo=PRIVATE_UMO, message_str="今天天气不错"))
        assert PRIVATE_UMO in plugin._last_events
        assert PRIVATE_UMO in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def test_on_message_period_keeps_inflight_generation_by_default(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        assert plugin.settings.abandon_stale_on_new_message is False
        token = plugin._gate.advance(UMO)
        plugin._gate.mark_running(UMO)
        await plugin.on_message(_make_event(message_str="。"))
        assert plugin._gate.current(UMO) == token
        assert plugin._gate.is_current(UMO, token)
        plugin._gate.unmark_running(UMO)

    with_plugin(tmp_path, scenario)


def test_period_during_generation_does_not_silence_skip_when_abandon_off(
    tmp_path: Path,
) -> None:
    async def scenario(plugin, main):
        models = importlib.import_module(main.__package__ + ".models")
        plugin.settings.min_silence_sec = 25
        plugin.settings.cooldown_sec = 0
        state = plugin._state_for(UMO)
        started = main.now_ts() - 30
        state.last_active_at = started
        plugin._coordinator.record_event(UMO, _make_event(message_str="阿c回我一下"), started)
        token = plugin._gate.advance(UMO)

        async def fake_decide(*_args, **_kwargs):
            return {"should_reply": True, "reason": "点名", "elapsed_sec": 0.0}

        async def fake_generate(_umo, _state, **kwargs):
            await plugin.on_message(_make_event(message_str="。"))
            ledger = kwargs.get("ledger") or models.AttemptLedger()
            return models.PipelineReply(text="一直在呢", ledger=ledger)

        plugin._decision.decide = fake_decide
        plugin._generation.generate = fake_generate
        result = await plugin._pipeline.check_session_locked(
            UMO, trigger="message_delay", force=False, expected_generation=token
        )
        assert result == "已主动回复。"
        assert "静默时间不足" not in result

    with_plugin(tmp_path, scenario, min_silence_sec=25, cooldown_sec=0)


def _arrange_reply_flow(plugin, main, models):
    """把 check_session_locked 推到「生成成功→投递→finally 记账」路径。"""

    async def fake_decide(*_args, **_kwargs):
        return {"should_reply": True, "reason": "测试", "elapsed_sec": 0.0}

    async def fake_generate(_umo, _state, **kwargs):
        ledger = kwargs.get("ledger") or models.AttemptLedger()
        return models.PipelineReply(text="在呢", ledger=ledger)

    plugin.settings.cooldown_sec = 0
    plugin._decision.decide = fake_decide
    plugin._generation.generate = fake_generate
    return plugin._gate.current(UMO) or plugin._gate.advance(UMO)


def test_check_session_registration_rejection_keeps_result_and_unmarks(tmp_path: Path) -> None:
    """finally 注册被拒不得改写主链结果，也不得漏掉 unmark_running。"""

    async def scenario(plugin, main):
        models = importlib.import_module(main.__package__ + ".models")
        token = _arrange_reply_flow(plugin, main, models)
        plugin._pipeline._track_critical_task = lambda _coro: None

        result = await plugin._pipeline.check_session_locked(
            UMO, trigger="message_delay", force=True, expected_generation=token
        )
        assert result == "已主动回复。"
        assert not plugin._gate.is_running(UMO)

    with_plugin(tmp_path, scenario)


def test_check_session_finalizer_internal_rejection_keeps_result(tmp_path: Path) -> None:
    """外层任务注册成功、_finalize_ledger 内部注册被拒：错误只留日志，结果不变。"""

    async def scenario(plugin, main):
        models = importlib.import_module(main.__package__ + ".models")
        token = _arrange_reply_flow(plugin, main, models)
        calls: list = []

        def track(coro):
            calls.append(coro)
            if len(calls) == 1:
                return asyncio.create_task(coro)
            coro.close()
            return None

        plugin._pipeline._track_critical_task = track

        result = await plugin._pipeline.check_session_locked(
            UMO, trigger="message_delay", force=True, expected_generation=token
        )
        assert result == "已主动回复。"
        assert not plugin._gate.is_running(UMO)

    with_plugin(tmp_path, scenario)


def test_on_message_period_abandons_inflight_when_enabled(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.abandon_stale_on_new_message = True
        token = plugin._gate.advance(UMO)
        plugin._gate.mark_running(UMO)
        await plugin.on_message(_make_event(message_str="。"))
        assert plugin._gate.current(UMO) > token
        assert not plugin._gate.is_current(UMO, token)
        plugin._gate.unmark_running(UMO)

    with_plugin(tmp_path, scenario)


def test_command_list_returns_sorted_whitelist_lines(tmp_path: Path) -> None:
    """/selfreply list 输出白名单全量与空态文案。"""

    async def scenario(plugin, main):
        event = _make_event(message_str="/selfreply list")
        plugin.settings.whitelist = set()
        assert await plugin._command_text(event, "list") == "主动回复白名单为空。"

        plugin.settings.whitelist = {"qq:GroupMessage:2", "qq:GroupMessage:1", "12345"}
        assert await plugin._command_text(event, "list") == (
            "主动回复白名单：\n- 12345\n- qq:GroupMessage:1\n- qq:GroupMessage:2"
        )

    with_plugin(tmp_path, scenario)


def test_check_session_rejects_foreign_ledger(tmp_path: Path) -> None:
    """生成返回异账本必须中止：否则投递事实记到别的账本上，本次配额不落账。"""

    async def scenario(plugin, main):
        models = importlib.import_module(main.__package__ + ".models")
        token = _arrange_reply_flow(plugin, main, models)
        foreign = models.AttemptLedger()

        async def fake_generate(_umo, _state, **_kwargs):
            return models.PipelineReply(text="在呢", ledger=foreign)

        plugin._generation.generate = fake_generate
        with pytest.raises(RuntimeError, match="different attempt ledger"):
            await plugin._pipeline.check_session_locked(
                UMO, trigger="message_delay", force=True, expected_generation=token
            )

        assert foreign.phase == "open", "异账本不得被密封或记账"
        assert plugin._state_for(UMO).daily_count == 0, "无提交的真账本不得计配额"
        assert not plugin._gate.is_running(UMO), "中止后仍必须 unmark_running"

    with_plugin(tmp_path, scenario)


def test_command_check_still_runs_private_when_disabled(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.whitelist.add(PRIVATE_UMO)
        plugin.settings.enabled_private_sessions = False
        seen: dict[str, object] = {}

        async def fake_check(umo, *, trigger, force, expected_generation):
            seen["umo"] = umo
            seen["force"] = force
            return "完成"

        original_check = plugin._pipeline.check_session
        plugin._pipeline.check_session = fake_check
        try:
            event = _make_event(umo=PRIVATE_UMO, message_str="/selfreply check")
            text = await plugin._command_text(event, "check")
            assert text == "主动回复检查结果：完成"
            assert seen["umo"] == PRIVATE_UMO
            assert seen["force"] is True
        finally:
            plugin._pipeline.check_session = original_check

    with_plugin(tmp_path, scenario)


def test_command_debug_returns_diagnostic_info(tmp_path: Path) -> None:
    """/selfreply debug 指令输出诊断文本，覆盖 commands.debug_text 分支。"""

    async def scenario(plugin, main):
        event = _make_event(message_str="/selfreply debug")
        text = await plugin._command_text(event, "debug")
        assert "主动回复调试信息" in text
        assert "归一化 UMO:" in text
        assert "is_at_or_wake_command:" in text

    with_plugin(tmp_path, scenario)


def test_guard_early_exit_creates_no_lock_entry(tmp_path: Path) -> None:
    """门卫早退不得创建锁表条目（distinct UMO 不留锁）。"""

    async def scenario(plugin, main):
        umo = "fake:group:no-lock-entry"
        assert umo not in plugin._session_locks
        result = await plugin._pipeline.check_session(umo, trigger="patrol", force=False)
        assert result == "会话不在主动回复白名单。"
        assert umo not in plugin._session_locks

    with_plugin(tmp_path, scenario)


def test_cooldown_skips_decision_model_after_proactive_reply(tmp_path: Path) -> None:
    """主动回复后冷却期内再来消息，不得再打判断模型。"""

    async def scenario(plugin, main):
        calls: list[str] = []
        original = plugin._decision.ask_decision_model

        async def counting(umo, state, *, trigger):
            calls.append(trigger)
            return await original(umo, state, trigger=trigger)

        plugin._decision.ask_decision_model = counting
        plugin.settings.decision_model_enabled = True
        plugin.settings.min_silence_sec = 0
        plugin.settings.cooldown_sec = 900
        plugin.settings.message_delay_sec = 0
        state = plugin._state_for(UMO)
        now = main.now_ts()
        event = _make_event(message_str="新消息")
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = now - 30
        state.last_active_at = now - 30
        state.last_proactive_at = now - 240
        state.last_proactive_observed_at = now - 240
        token = plugin._gate.advance(UMO)

        result = await plugin._pipeline.check_session(
            UMO, trigger="message_delay", force=False, expected_generation=token
        )

        assert "冷却中" in result
        assert calls == []

    with_plugin(tmp_path, scenario, cooldown_sec=900, min_silence_sec=0)


def test_startup_persist_failure_is_logged(tmp_path: Path, monkeypatch: Any, caplog: Any) -> None:
    """``persist_settings_config`` 返回 False 时启动路径必须记 ERROR。

    失败只由 ``write_json_atomic`` 记一条 warning 的话，用户看到的是"配置正常
    加载、插件正常工作"，而磁盘上一直是旧形状、下次启动还会再迁一遍。行为断言
    （而不是只看源码里有没有 ``if``）才能覆盖 ``if False and not persist(...)``
    这类"保留了分支却不再执行"的形态。
    """
    from .host_stubs import capture_logs, load_main, messages_at_least

    main = load_main()
    calls: list[str] = []

    def failing_persist(*args: Any, **kwargs: Any) -> bool:
        calls.append("persist")
        return False

    monkeypatch.setattr(main, "persist_settings_config", failing_persist)

    async def scenario(plugin, _main):
        return None

    with capture_logs(caplog, main.logger):
        with_plugin(tmp_path, scenario)

    assert calls == ["persist"], "启动路径没有调用 persist_settings_config"
    errors = messages_at_least(caplog, logging.ERROR)
    assert any("配置规范化落盘失败" in message for message in errors), (
        f"落盘失败没有记 ERROR，实际日志：{errors}"
    )


def test_messages_during_running_check_coalesce_to_one_follow_up(tmp_path: Path) -> None:
    """检查进行中连来多条消息，只允许再跟一次判断，不得叠出多条并行模型调用。"""

    async def scenario(plugin, main):
        from .host_stubs import until

        model_calls: list[str] = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_ask(umo, state, *, trigger):
            model_calls.append(str(trigger))
            if not entered.is_set():
                entered.set()
                await release.wait()
            return {"should_reply": False, "reason": "群聊平静", "elapsed_sec": 0.0}

        plugin._decision.ask_decision_model = slow_ask
        plugin.settings.decision_model_enabled = True
        plugin.settings.min_silence_sec = 0
        plugin.settings.cooldown_sec = 0
        plugin.settings.message_delay_sec = 0
        plugin.settings.abandon_stale_on_new_message = False
        plugin._last_events[UMO] = _make_event(message_str="先来一条")
        plugin._last_event_at[UMO] = main.now_ts()
        plugin._state_for(UMO).last_active_at = main.now_ts() - 30
        token = plugin._gate.advance(UMO)

        task = asyncio.create_task(
            plugin._pipeline.check_session(
                UMO, trigger="message_delay", force=False, expected_generation=token
            )
        )
        await entered.wait()
        await plugin.on_message(_make_event(message_str="检查中 1"))
        await plugin.on_message(_make_event(message_str="检查中 2"))
        await plugin.on_message(_make_event(message_str="检查中 3"))
        pending = [
            delayed
            for delayed in plugin._delay_tasks.values()
            if delayed is not task and not delayed.done()
        ]
        assert len(pending) <= 1
        release.set()
        await task
        await until(lambda: all(item.done() for item in list(plugin._delay_tasks.values())))
        leftover = [delayed for delayed in plugin._delay_tasks.values() if not delayed.done()]
        assert leftover == []
        assert model_calls == ["message_delay", "message_delay"]

    with_plugin(
        tmp_path,
        scenario,
        cooldown_sec=0,
        min_silence_sec=0,
        message_delay_sec=0,
        decision_model_enabled=True,
    )
