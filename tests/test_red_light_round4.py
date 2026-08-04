"""红灯测试（第四轮）：0.7.19 全面审查修复验证

每个测试断言"期望的正确行为"，修复前应当失败（红灯），修复后转绿：
- R1 开关快照：运行中改配置不影响本次运行的 install/enforce 语义
- R2 reset 时序：第二次工具清理发生在 reset 之前
- R3 system_hint：继承模式提示词描述真实边界，不再写死禁用工具
- R4 缓存命中：未篡改内容不重写文件（digest 比较修复）
- R5 配置回滚：sessions 与会话锁一并恢复
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import sys
import types
from pathlib import Path

from .host_stubs import with_plugin
from .test_main_runtime import UMO, _make_event, _PipelineTestAdapter


def _load_modules():
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
    token = plugin._advance_session_generation(UMO)
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

    image = _load_modules()
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
        plugin._session_locks[umo] = asyncio.Lock()
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
