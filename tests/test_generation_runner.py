"""GenerationRunner 独立单测（ticket 04 验收）：注入假运行时适配器，脱离插件实例。

覆盖验收项：
- 生成入口可独立单测：工具集快照（new_tool_set）与边界安装/恢复配对
- 工具直发预算与代次闸门自洽，直发文本回传语义
- 超时/取消/失败三类出口的直发计数与文本不丢失
- 最终工具策略 fail-closed（keep/drop 两种模式）
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

from .test_vision import PACKAGE_NAME, _load_modules


def _generation_module():
    return importlib.import_module(f"{PACKAGE_NAME}.generation")


class FakeToolSet:
    def __init__(self) -> None:
        self.tools: list[SimpleNamespace] = []

    def add_tool(self, tool: SimpleNamespace) -> None:
        self.tools.append(tool)

    def ids(self) -> list[str]:
        return [str(getattr(t, "name", "")) for t in self.tools]


class FakeEvent:
    def __init__(self) -> None:
        self.plugins_name: list[str] = ["other_plugin"]
        self._extras: dict[str, object] = {}

    def set_extra(self, key: str, value: object) -> None:
        self._extras[key] = value

    def get_extra(self, key: str) -> object:
        return self._extras.get(key)

    async def send(self, message) -> None:
        return None


class FakeAgentRunner:
    def __init__(self, resp: object) -> None:
        self._resp = resp
        self.request_stop_calls = 0

    def reset(self, **_):
        return _FakeResetCoro()

    def request_stop(self) -> None:
        self.request_stop_calls += 1

    def get_final_llm_resp(self):
        return self._resp

    def close(self) -> None:
        pass


class _FakeResetCoro:
    closed = False

    def close(self) -> None:
        self.closed = True

    def __await__(self):
        async def _noop():
            return None

        return _noop().__await__()


class FakeBuildResult:
    def __init__(self, agent_runner: object, provider_request: object, reset_coro: object | None):
        self.agent_runner = agent_runner
        self.provider_request = provider_request
        self.reset_coro = reset_coro


class FakeRuntime:
    def __init__(self, *, build_results: list | None = None, hang: bool = False) -> None:
        self.tool_set_snapshots: list[list[str]] = []
        self.filter_calls: list[tuple[object, object]] = []
        self.build_kwargs: list[dict] = []
        self.built_reqs: list[object] = []
        self.build_results = build_results if build_results is not None else []
        self.hang = hang
        self.run_started = asyncio.Event()

    def new_tool_set(self):
        return FakeToolSet()

    def new_provider_request(self):
        return SimpleNamespace(
            prompt="",
            image_urls=[],
            audio_urls=[],
            func_tool=None,
            session_id="",
            conversation=None,
            contexts=[],
        )

    @property
    def event_type(self):
        return SimpleNamespace(OnLLMRequestEvent="OnLLMRequestEvent")

    def filter_final_tools(self, req, *, keep=None, drop=frozenset()):
        self.filter_calls.append((keep, drop))
        tool_set = getattr(req, "func_tool", None)
        if tool_set is None or not hasattr(tool_set, "tools"):
            return False
        if keep is not None:
            tool_set.tools = [t for t in tool_set.tools if t.name in keep]
        else:
            tool_set.tools = [t for t in tool_set.tools if t.name not in drop]
        return True

    def final_tool_ids(self, req):
        tool_set = getattr(req, "func_tool", None)
        if tool_set is None:
            return []
        return tool_set.ids()

    async def load_session_conversation(self, event, context):
        return SimpleNamespace(history="[]")

    def new_build_config(self, **kwargs):
        return SimpleNamespace(**kwargs)

    async def build(self, **kwargs):
        self.build_kwargs.append(kwargs)
        self.built_reqs.append(kwargs["req"])
        if self.build_results:
            return self.build_results.pop(0)
        return FakeBuildResult(
            agent_runner=FakeAgentRunner(SimpleNamespace(completion_text="你好呀")),
            provider_request=kwargs["req"],
            reset_coro=_FakeResetCoro(),
        )

    def run(self, agent_runner, **kwargs):
        async def gen():
            self.run_started.set()
            if self.hang:
                await asyncio.sleep(3600)
            yield None

        return gen()


async def _direct_send(event: FakeEvent, text: str) -> None:
    from .host_stubs import _FakeMessageChain

    await event.send(_FakeMessageChain(type="tool_direct_result", chain=[text]))


def _make_runner(
    tmp_path: Path,
    config: dict | None = None,
    *,
    runtime: FakeRuntime | None = None,
    enforce: object | None = None,
    hook: object | None = None,
    grace_sec: float = 0.05,
    history: list | None = None,
    image_context: str = "",
):
    from . import host_stubs

    host_stubs.install_astrbot_stubs()  # 补 vision 桩缺失的宿主符号（generation 顶层导入）
    _, _, models = _load_modules()
    generation_mod = _generation_module()
    settings = models.Settings.from_config(config or {})
    fake_runtime = runtime if runtime is not None else FakeRuntime()
    gate = SimpleNamespace(is_current=lambda umo, generation: True)
    calls = {"enforce": 0, "hook": 0, "history": 0, "image": 0}

    if enforce is None:

        def enforce(req, inherit_tools):
            calls["enforce"] += 1
            return True

    if hook is None:

        async def hook(event_obj, event_type, req):
            calls["hook"] += 1
            return False

    async def read_history(umo, limit):
        calls["history"] += 1
        return list(history or [])

    async def build_image_context(umo, enabled, provider_id):
        calls["image"] += 1
        return image_context

    background_tasks: set[asyncio.Task] = set()

    def discard(task: asyncio.Task) -> None:
        background_tasks.discard(task)

    runner = generation_mod.GenerationRunner(
        settings=settings,
        context=SimpleNamespace(astrbot_config={}, get_config=None),
        runtime=lambda: fake_runtime,
        gate=gate,
        local_gate=lambda state, force: "",
        enforce_policy=enforce,
        call_hook=hook,
        grace_stop_sec=lambda: grace_sec,
        background_tasks=background_tasks,
        discard_background=discard,
        read_history=read_history,
        build_image_context=build_image_context,
        last_events={},
    )
    return generation_mod, models, runner, fake_runtime, calls, background_tasks


def _state(models, *, recent=None):
    state = models.SessionState()
    state.last_active_at = 100.0
    if recent:
        for role, text, at in recent:
            state.recent.append(models.MessageRecord(role=role, name="u", text=text, at=at))
    return state


# ============================================================================
# 工具集快照与边界安装/恢复配对（验收项 1）
# ============================================================================


async def test_generate_installs_and_restores_tool_boundary(tmp_path: Path) -> None:
    _, models, runner, runtime, _, _ = _make_runner(tmp_path)
    event = FakeEvent()
    runner._last_events["s1"] = event
    state = _state(models)
    result = await runner.generate("s1", state, expected_generation=1, force=True)

    assert result.text == "你好呀"
    # 边界安装：build 时 plugins_name 已被清空
    assert runtime.build_kwargs[0]["req"].func_tool is not None
    assert event.plugins_name == ["other_plugin"]  # 恢复
    assert "send" not in event.__dict__  # tracker 恢复
    assert event.get_extra("provider_request") is None
    assert event.get_extra("self_initiated_reply") is True


async def test_generate_inherit_tools_skips_boundary(tmp_path: Path) -> None:
    _, models, runner, _, _, _ = _make_runner(tmp_path, {"proactive_inherit_tools": True})
    event = FakeEvent()
    runner._last_events["s1"] = event
    state = _state(models)
    result = await runner.generate("s1", state, force=True)
    assert result.text == "你好呀"
    assert event.plugins_name == ["other_plugin"]  # 未被清空


async def test_generate_no_last_event_returns_empty(tmp_path: Path) -> None:
    _, models, runner, _, _, _ = _make_runner(tmp_path)
    result = await runner.generate("s1", _state(models), force=True)
    assert result.text == ""
    assert result.direct_send_count == 0


async def test_install_boundary_raises_without_plugins_name(tmp_path: Path) -> None:
    _, _, runner, _, _, _ = _make_runner(tmp_path)
    event = SimpleNamespace()  # 无 plugins_name
    try:
        runner.install_agent_tool_boundary(event, False)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "插件工具边界" in str(exc)


# ============================================================================
# 直发预算与代次闸门（验收项 2）
# ============================================================================


async def test_generate_tracks_direct_sends_within_budget(tmp_path: Path) -> None:
    _, models, runner, runtime, _, _ = _make_runner(tmp_path)
    event = FakeEvent()
    runner._last_events["s1"] = event
    state = _state(models)

    def run_with_directs(_runner, **_kwargs):
        async def gen():
            for i in range(3):  # MAX_DIRECT_TOOL_SENDS = 2
                await _direct_send(event, f"直发{i}")
                yield None

        return gen()

    runtime.run = run_with_directs
    result = await runner.generate("s1", state, force=True)
    assert result.direct_send_count == 2
    assert result.text == "你好呀"
    assert len(result.direct_texts) == 2


async def test_generate_gate_suppresses_direct_send(tmp_path: Path) -> None:
    """代次闸门关闭时（is_current=False），工具直发被预算网关抑制。"""
    _, models, runner, runtime, _, _ = _make_runner(tmp_path)
    event = FakeEvent()
    runner._last_events["s1"] = event
    runner._gate = SimpleNamespace(is_current=lambda umo, generation: False)
    state = _state(models)

    def run_with_directs(_runner, **_kwargs):
        async def gen():
            await _direct_send(event, "被闸门拒绝")
            yield None

        return gen()

    runtime.run = run_with_directs
    result = await runner.generate("s1", state, force=True)
    assert result.direct_send_count == 0
    assert result.direct_texts == ()


# ============================================================================
# 超时/取消/失败三类出口不丢失直发（验收项 3）
# ============================================================================


async def test_generate_timeout_requests_graceful_stop_keeps_directs(tmp_path: Path) -> None:
    _, models, runner, runtime, _, _ = _make_runner(tmp_path, {"generation_timeout_sec": 0.05})
    event = FakeEvent()
    runner._last_events["s1"] = event
    state = _state(models)

    def hanging_run(_runner, **_kwargs):
        async def gen():
            await _direct_send(event, "超时前直发")
            await asyncio.sleep(3600)
            yield None

        return gen()

    runtime.run = hanging_run
    result = await runner.generate("s1", state, force=True)
    assert result.text == ""
    assert result.direct_send_count == 1  # 超时出口不丢直发
    assert result.direct_texts == ("超时前直发",)


async def test_generate_cancel_converges_no_orphan(tmp_path: Path) -> None:
    _, models, runner, runtime, _, background_tasks = _make_runner(tmp_path)
    event = FakeEvent()
    runner._last_events["s1"] = event
    state = _state(models)
    hanging = FakeRuntime()
    hanging.run_started = asyncio.Event()

    def hanging_run(_runner, **_kwargs):
        async def gen():
            hanging.run_started.set()
            await asyncio.sleep(3600)
            yield None

        return gen()

    hanging.run = hanging_run
    runner._runtime = lambda: hanging

    task = asyncio.ensure_future(runner.generate("s1", state, force=True))
    await hanging.run_started.wait()
    task.cancel()
    try:
        await task
        raise AssertionError("expected CancelledError")
    except asyncio.CancelledError:
        pass
    # 孤儿收敛：run_task 已被取消，后台登记清空
    await asyncio.sleep(0.1)
    assert all(t.done() for t in background_tasks)
    assert not background_tasks


async def test_generate_fail_closed_aborts_run_keeps_directs(tmp_path: Path) -> None:
    _, models, runner, runtime, _, _ = _make_runner(tmp_path)

    def reject_policy(req, inherit_tools):
        return False  # 工具集无法枚举 → fail closed

    runner._enforce_policy = reject_policy
    event = FakeEvent()
    runner._last_events["s1"] = event
    state = _state(models)
    result = await runner.generate("s1", state, force=True)
    assert result.text == ""
    assert result.direct_send_count == 0
    assert runtime.build_kwargs  # build 已发生，但 run 未发生
    assert runtime.run_started.is_set() is False


async def test_generate_build_none_returns_directs(tmp_path: Path) -> None:
    _, models, runner, runtime, _, _ = _make_runner(
        tmp_path, runtime=FakeRuntime(build_results=[None])
    )
    event = FakeEvent()
    runner._last_events["s1"] = event
    state = _state(models)
    result = await runner.generate("s1", state, force=True)
    assert result.text == ""
    assert result.direct_send_count == 0


async def test_generate_hook_early_exit_restores_event(tmp_path: Path) -> None:
    _, models, runner, runtime, _, _ = _make_runner(tmp_path)

    async def stop_hook(event_obj, event_type, req):
        return True  # 早退

    runner._call_hook = stop_hook
    event = FakeEvent()
    runner._last_events["s1"] = event
    state = _state(models)
    result = await runner.generate("s1", state, force=True)
    assert result.text == ""
    assert event.plugins_name == ["other_plugin"]
    assert event.get_extra("provider_request") is None
    assert runtime.run_started.is_set() is False


# ============================================================================
# 最终工具策略（fail-closed；keep/drop 两种模式）
# ============================================================================


async def test_enforce_policy_keep_mode_filters(tmp_path: Path) -> None:
    _, _, runner, runtime, _, _ = _make_runner(tmp_path)
    req = SimpleNamespace(func_tool=None)
    assert runner.enforce_final_tool_policy(req, False) is False  # 无法枚举 → fail closed

    tool_set = FakeToolSet()
    tool_set.add_tool(SimpleNamespace(name="web_search"))
    tool_set.add_tool(SimpleNamespace(name="send_message_to_user"))
    req = SimpleNamespace(func_tool=tool_set)
    assert runner.enforce_final_tool_policy(req, False) is True
    assert tool_set.ids() == []  # 默认空 allowlist 全部移除


async def test_enforce_policy_drop_mode_removes_dangerous(tmp_path: Path) -> None:
    _, _, runner, _, _, _ = _make_runner(tmp_path)
    tool_set = FakeToolSet()
    tool_set.add_tool(SimpleNamespace(name="web_search"))
    tool_set.add_tool(SimpleNamespace(name="astrbot_execute_shell"))
    tool_set.add_tool(SimpleNamespace(name="future_task"))
    req = SimpleNamespace(func_tool=tool_set)
    assert runner.enforce_final_tool_policy(req, True) is True
    assert tool_set.ids() == ["web_search"]  # 危险工具被 drop


# ============================================================================
# 构建配置与上下文
# ============================================================================


async def test_main_agent_build_config_defaults(tmp_path: Path) -> None:
    _, _, runner, runtime, _, _ = _make_runner(tmp_path)

    class BuildConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    runtime.new_build_config = lambda **kwargs: BuildConfig(**kwargs)
    config = runner.main_agent_build_config("s1")
    assert config.kwargs["tool_schema_mode"] == "full"
    assert config.kwargs["kb_agentic_mode"] is False
    assert config.kwargs["file_extract_enabled"] is False
    assert config.kwargs["computer_use_runtime"] == "none"
    assert config.kwargs["add_cron_tools"] is False


async def test_build_context_text_merges_history_and_image(tmp_path: Path) -> None:
    _, models, runner, _, _, _ = _make_runner(tmp_path, image_context="[图片描述]")
    state = _state(models, recent=[("user", "新消息", 990.0)])
    text = await runner.build_context_text("s1", state)
    assert "新消息" in text
    assert "[图片描述]" in text
