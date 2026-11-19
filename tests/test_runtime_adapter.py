"""运行时能力校验与宿主处理器注解解析。"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from .host_stubs import (
    base_runtime_capabilities,
    load_modules,
)

PACKAGE_NAME = "selfreply_runtime_test_package"


def _load_adapter():
    return load_modules(PACKAGE_NAME, "runtime_adapter")[0]


def test_runtime_adapter_validates_private_agent_capabilities() -> None:
    runtime = _load_adapter()

    class ToolSet:
        pass

    class BuildConfig:
        def __init__(self, **kwargs):
            self.values = kwargs

    async def build_main_agent(*, event, plugin_context, config, req, apply_reset):
        return (event, plugin_context, config, req, apply_reset)

    async def get_session_conv(event, context):
        return event, context

    async def run_agent(agent_runner, *, max_step, **kwargs):
        yield agent_runner, max_step, kwargs

    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(
            runtime,
            tool_set=ToolSet,
            build_config=BuildConfig,
            build_main_agent=build_main_agent,
            get_session_conv=get_session_conv,
            run_agent=run_agent,
        )
    )

    adapter.validate()
    assert isinstance(adapter.new_tool_set(), ToolSet)
    assert adapter.new_build_config(timeout=3).values == {"timeout": 3}


def test_runtime_adapter_accepts_astrbot_430_agent_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """AstrBot 4.30 moved the main-agent modules without changing the calls we use."""
    runtime = _load_adapter()

    async def run_agent(agent_runner, **_kwargs):
        if False:
            yield agent_runner

    async def get_session_conv(event, context):
        return event, context

    async def build_main_agent(**kwargs):
        return kwargs

    class BuildConfig:
        def __init__(self, **kwargs):
            self.values = kwargs

    old_modules = (
        "astrbot.core.astr_agent_run_util",
        "astrbot.core.astr_main_agent",
    )
    for name in old_modules:
        monkeypatch.delitem(sys.modules, name, raising=False)

    def install(name: str, **symbols: object) -> None:
        module = types.ModuleType(name)
        for symbol, value in symbols.items():
            setattr(module, symbol, value)
        monkeypatch.setitem(sys.modules, name, module)

    install("astrbot.core.astr_agent_runtime")
    install("astrbot.core.astr_agent_runtime.main_agent")
    install("astrbot.core.astr_agent_runtime.runner", run_agent=run_agent)
    install(
        "astrbot.core.astr_agent_runtime.main_agent.config",
        MainAgentBuildConfig=BuildConfig,
    )
    install(
        "astrbot.core.astr_agent_runtime.main_agent.request",
        _get_session_conv=get_session_conv,
    )
    install(
        "astrbot.core.astr_agent_runtime.main_agent.builder",
        build_main_agent=build_main_agent,
    )

    adapter = runtime.AstrBotRuntimeAdapter.from_host()
    assert adapter.capabilities.import_error is None
    assert adapter.capabilities.run_agent is run_agent
    assert adapter.capabilities.build_config is BuildConfig
    assert adapter.capabilities.get_session_conv is get_session_conv
    assert adapter.capabilities.build_main_agent is build_main_agent
    assert adapter.validate() == []

    contract = dict(adapter.host_contract())
    assert contract["astrbot.core.astr_agent_runtime.runner"] == ["run_agent"]
    assert contract["astrbot.core.astr_agent_runtime.main_agent.config"] == ["MainAgentBuildConfig"]


def test_runtime_adapter_reports_signature_mismatch() -> None:
    runtime = _load_adapter()

    async def incompatible(*, event, plugin_context, config):
        return None

    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, build_main_agent=incompatible)
    )

    with pytest.raises(RuntimeError, match="req"):
        adapter.validate()


def test_runtime_adapter_omits_apply_reset_for_new_host() -> None:
    import asyncio

    runtime = _load_adapter()
    calls: list[dict[str, object]] = []

    async def build_main_agent(*, event, plugin_context, config, req):
        calls.append(
            {"event": event, "plugin_context": plugin_context, "config": config, "req": req}
        )
        return "built"

    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, build_main_agent=build_main_agent)
    )
    config = SimpleNamespace(call_request_hook=False)

    result = asyncio.run(
        adapter.build(
            event="event",
            plugin_context="context",
            config=config,
            req="request",
            apply_reset=False,
        )
    )
    assert result == "built"
    assert calls == [
        {
            "event": "event",
            "plugin_context": "context",
            "config": config,
            "req": "request",
        }
    ]


def test_runtime_adapter_enforces_run_contract_params() -> None:
    """run_agent 缺少实际使用的运行参数时必须加载期失败。"""
    runtime = _load_adapter()

    async def run_agent(agent_runner, *, max_step):
        yield agent_runner, max_step

    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime, run_agent=run_agent))

    with pytest.raises(RuntimeError, match="show_tool_use"):
        adapter.validate()


def test_filter_final_tools_modes() -> None:
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))

    class Tool:
        def __init__(self, name: str):
            self.name = name

    class ToolSet:
        def __init__(self):
            self.tools = []

        def add_tool(self, tool):
            self.tools.append(tool)

        def remove_tool(self, name):
            self.tools = [t for t in self.tools if t.name != name]

    tool_set = ToolSet()
    tool_set.add_tool(Tool("send_message_to_user"))
    tool_set.add_tool(Tool("web_search"))
    req = type("Req", (), {"func_tool": tool_set})()

    assert adapter.filter_final_tools(req, keep=frozenset()) is True
    assert tool_set.tools == []

    # 无法枚举 -> fail closed
    bad_req = type("Req", (), {"func_tool": type("Bad", (), {"tools": None})()})()
    assert adapter.filter_final_tools(bad_req, keep=frozenset()) is False

    # 无工具集 -> 天然空，放行
    empty_req = type("Req", (), {"func_tool": None})()
    assert adapter.filter_final_tools(empty_req, keep=frozenset()) is True

    # denylist 模式：只移除指定工具，其余保留
    tool_set2 = ToolSet()
    tool_set2.add_tool(Tool("astr_kb_search"))
    tool_set2.add_tool(Tool("third_party_weather"))
    req2 = type("Req", (), {"func_tool": tool_set2})()
    assert (
        adapter.filter_final_tools(req2, drop=frozenset({"astr_kb_search", "create_future_task"}))
        is True
    )
    assert [t.name for t in tool_set2.tools] == ["third_party_weather"]

    class HandoffTool(Tool):
        pass

    handoff_adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, handoff_tool=HandoffTool)
    )
    handoff_set = ToolSet()
    handoff_set.add_tool(HandoffTool("transfer_to_other_agent"))
    handoff_req = type("Req", (), {"func_tool": handoff_set})()
    assert (
        handoff_adapter.filter_final_tools(handoff_req, drop=frozenset({"astr_kb_search"})) is True
    )
    assert handoff_set.tools == []


async def test_guard_final_tools_replaces_handoff_with_blocked_function_tool() -> None:
    runtime = _load_adapter()
    calls: list[str] = []

    class HandoffTool:
        name = "transfer_to_other_agent"
        description = "Delegate a task"
        parameters = {"type": "object", "properties": {}}
        active = True
        handler = None

        async def call(self, _context, **_kwargs):
            calls.append("handoff")
            return "executed"

    capabilities = base_runtime_capabilities(runtime, handoff_tool=HandoffTool)
    adapter = runtime.AstrBotRuntimeAdapter(capabilities)

    class ToolSet:
        def __init__(self) -> None:
            self.tools = [HandoffTool()]

    req = SimpleNamespace(func_tool=ToolSet())

    assert adapter.guard_final_tools(req, block=frozenset()) is True
    guarded = req.func_tool.tools[0]
    result = await guarded.handler(None)

    assert type(guarded) is capabilities.function_tool
    assert (guarded.name, guarded.description, guarded.parameters) == (
        "transfer_to_other_agent",
        "Delegate a task",
        {"type": "object", "properties": {}},
    )
    assert result == "error: tool 'transfer_to_other_agent' is disabled during proactive replies"
    assert calls == []


@pytest.mark.asyncio
async def test_guard_final_tools_replaces_mcp_subclass_tool() -> None:
    runtime = _load_adapter()
    called = False

    class MCPTool:
        name = "future_task"
        description = "Schedule a task"
        parameters = {"type": "object", "properties": {}}
        handler = None
        active = True

        async def call(self, _context, **_kwargs):
            nonlocal called
            called = True
            return "executed"

    capabilities = base_runtime_capabilities(runtime)
    adapter = runtime.AstrBotRuntimeAdapter(capabilities)
    req = SimpleNamespace(func_tool=SimpleNamespace(tools=[MCPTool()]))

    assert adapter.guard_final_tools(req, block=frozenset({"future_task"})) is True
    guarded = req.func_tool.tools[0]
    result = await guarded.handler(None)

    assert type(guarded) is capabilities.function_tool
    assert (guarded.name, guarded.description, guarded.parameters) == (
        "future_task",
        "Schedule a task",
        {"type": "object", "properties": {}},
    )
    assert result == "error: tool 'future_task' is disabled during proactive replies"
    assert called is False


async def test_guard_final_tools_also_replaces_skill_like_raw_tools() -> None:
    runtime = _load_adapter()
    called = False

    class MCPTool:
        name = "future_task"
        description = "Schedule a task"
        parameters = {"type": "object", "properties": {}}
        handler = None
        active = True

        async def call(self, _context, **_kwargs):
            nonlocal called
            called = True
            return "executed"

    capabilities = base_runtime_capabilities(runtime)
    adapter = runtime.AstrBotRuntimeAdapter(capabilities)
    raw_tools = SimpleNamespace(tools=[MCPTool()])
    request = SimpleNamespace(
        func_tool=SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="future_task",
                    description="Schedule a task",
                    parameters={"type": "object", "properties": {}},
                    active=True,
                )
            ]
        )
    )

    assert (
        adapter.guard_final_tools(
            request,
            block=frozenset({"future_task"}),
            agent_runner=SimpleNamespace(_skill_like_raw_tool_set=raw_tools),
        )
        is True
    )
    guarded = raw_tools.tools[0]
    assert (guarded.name, guarded.description, guarded.parameters) == (
        "future_task",
        "Schedule a task",
        {"type": "object", "properties": {}},
    )
    assert await guarded.handler(None) == (
        "error: tool 'future_task' is disabled during proactive replies"
    )
    assert called is False


# ============================================================================
# validate 契约：缺失时报错、软模式告警
# ============================================================================


def test_validate_event_result_missing_method_red() -> None:
    """缺少 set_result_content_type 时，硬模式拒绝加载。"""
    runtime = _load_adapter()
    caps = base_runtime_capabilities(
        runtime,
        event_result_cls=type("BrokenResult", (), {"message": lambda self, text: self}),
    )
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="set_result_content_type"):
        adapter.validate()


def test_validate_event_type_missing_member_red() -> None:
    runtime = _load_adapter()
    caps = base_runtime_capabilities(
        runtime,
        event_type=type("ET", (), {"OnDecoratingResultEvent": "x", "OnAfterMessageSentEvent": "x"}),
    )
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="OnLLMRequestEvent"):
        adapter.validate()


@pytest.mark.parametrize("field", ["session_id", "func_tool"])
def test_validate_provider_request_missing_field_red(field: str) -> None:
    runtime = _load_adapter()
    caps = base_runtime_capabilities(runtime)
    delattr(caps.provider_request_cls, field)
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match=field):
        adapter.validate()


def test_validate_call_event_hook_missing_red() -> None:
    runtime = _load_adapter()
    caps = base_runtime_capabilities(runtime, call_event_hook=None)
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="call_event_hook"):
        adapter.validate()


def test_validate_soft_warns_without_raising() -> None:
    """软模式（最新版漂移预警）：收集告警不阻塞。"""
    runtime = _load_adapter()
    caps = base_runtime_capabilities(
        runtime,
        event_type=type("ET", (), {"OnDecoratingResultEvent": "x", "OnAfterMessageSentEvent": "x"}),
    )
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    problems = adapter.validate(soft=True)
    assert any("OnLLMRequestEvent" in p for p in problems)


def test_validate_soft_green_silent() -> None:
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))
    assert adapter.validate(soft=True) == []


# ============================================================================
# 窄方法出口与事件钩子
# ============================================================================


def test_command_handler_annotations_resolve_at_runtime() -> None:
    """宿主以 inspect.signature(eval_str=True) 解析处理器，注解所需名称须在运行时存在。"""
    import inspect

    main_mod = load_modules(PACKAGE_NAME, "main")[0]
    plugin_cls = main_mod.SelfInitiatedReplyPlugin

    # 宿主注册的两类处理器：event_message_type 钩子与 /selfreply 指令族。
    handler_names = [
        name for name in dir(plugin_cls) if name == "on_message" or name.startswith("selfreply")
    ]
    assert "on_message" in handler_names
    # 与兼容检查共用处理器名单；指令组本体由宿主装饰器包装，单独排除。
    from scripts.compat_check import EXPECTED_HANDLER_NAMES

    assert {name for name in handler_names if name != "selfreply"} == EXPECTED_HANDLER_NAMES

    for name in handler_names:
        target = getattr(plugin_cls, name)
        func = getattr(target, "handler", target)
        if not callable(func) or not getattr(func, "__annotations__", None):
            continue
        # 与宿主加载期同一个调用；TYPE_CHECKING-only 名字在此 NameError。
        sig = inspect.signature(func, eval_str=True)
        annotation = sig.parameters["event"].annotation
        # 注解已被 eval（不再是字符串），说明这一步真的走过了解析而非原样透传。
        assert not isinstance(annotation, str), f"{name} 的 event 注解未被解析"


# ============================================================================
# 降级宿主分支与工具过滤边界（validate 告警、路径回退、fail-closed、run 直通）
# ============================================================================
def _adapter(runtime, **overrides):
    return runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime, **overrides))


# ============================================================================
# validate：降级宿主告警分支（软模式收集，硬模式首错时报错）
# ============================================================================


def test_validate_import_error_branch() -> None:
    runtime = _load_adapter()
    adapter = _adapter(runtime, import_error=RuntimeError("host missing"), tool_set=None)
    problems = adapter.validate(soft=True)
    assert any("主 Agent API" in item for item in problems)
    with pytest.raises(RuntimeError):
        adapter.validate()


def test_validate_missing_capability_branches() -> None:
    """tool_set/event_result_cls/result_content_type/event_type/provider_request 缺失分支。"""
    runtime = _load_adapter()
    adapter = _adapter(
        runtime,
        tool_set=None,
        event_result_cls=None,
        result_content_type=None,
        event_type=None,
        provider_request_cls=None,
    )
    problems = adapter.validate(soft=True)
    joined = " | ".join(problems)
    assert "ToolSet" in joined
    assert "MessageEventResult" in joined
    assert "ResultContentType" in joined
    assert "EventType" in joined
    assert "ProviderRequest" in joined


def test_validate_result_content_type_without_llm_result() -> None:
    """ResultContentType 存在但缺 ``LLM_RESULT`` 成员也必须报契约红。

    缺陷形态：``if caps.result_content_type is None or not hasattr(...,
    "LLM_RESULT")``。把 ``or`` 改成 ``and`` 后，``None is None`` 与
    ``hasattr`` 永不同真，这一类缺失不再上报——插件正常加载，第一次主动生成时
    ``result_llm_type`` 属性抛 RuntimeError，加载期「缺符号即报」的隔离设计被
    绕开，故障点从加载期挪到运行期深处，且没有任何一条主动回复能产出。
    """
    runtime = _load_adapter()

    class _NoLLMResult:
        pass

    adapter = _adapter(runtime, result_content_type=_NoLLMResult)
    problems = adapter.validate(soft=True)
    assert any("ResultContentType" in item for item in problems), (
        f"ResultContentType 缺 LLM_RESULT 未上报：{problems}"
    )
    with pytest.raises(RuntimeError):
        adapter.validate()

    # 对照：成员齐备时不得误报
    class _WithLLMResult:
        LLM_RESULT = "llm_result"

    ok = _adapter(runtime, result_content_type=_WithLLMResult)
    assert not any("ResultContentType" in item for item in ok.validate(soft=True))


def test_validate_uninstantiable_result_and_request_classes() -> None:
    """事件结果类/请求类不可实例化分支。"""
    runtime = _load_adapter()

    class BoomResult:
        def __init__(self) -> None:
            raise RuntimeError("result broken")

    class BoomRequest:
        def __init__(self) -> None:
            raise RuntimeError("request broken")

    adapter = _adapter(runtime, event_result_cls=BoomResult, provider_request_cls=BoomRequest)
    problems = adapter.validate(soft=True)
    joined = " | ".join(problems)
    assert "MessageEventResult 不可实例化" in joined
    assert "ProviderRequest 不可实例化" in joined


def test_validate_non_callable_path_fn_and_signature_probe_error() -> None:
    """路径函数不可调用分支；签名探测 ValueError 静默放行分支。"""
    runtime = _load_adapter()
    adapter = _adapter(
        runtime,
        config_path_fn=42,
        plugin_data_path_fn=42,
        # staticmethod 实例可调用但 inspect.signature 抛 ValueError → 静默放行
        # （3.14 下 len 等常见 C 函数已有文本签名，不再触发该分支）
        build_main_agent=staticmethod(int),
    )
    problems = adapter.validate(soft=True)
    joined = " | ".join(problems)
    assert "get_astrbot_config_path 不可调用" in joined
    assert "get_astrbot_plugin_data_path 不可调用" in joined
    assert "build_main_agent" not in joined  # 签名探测失败不记问题


def test_validate_probes_build_config_kwargs() -> None:
    """加载时实例化构建配置以核验字段名，避免签名兼容但运行时构造失败。"""
    runtime = _load_adapter()

    class StrictBuildConfig:
        def __init__(self, *, tool_call_timeout, tool_schema_mode):
            self.values = {
                "tool_call_timeout": tool_call_timeout,
                "tool_schema_mode": tool_schema_mode,
            }

    adapter = _adapter(runtime, build_config=StrictBuildConfig)
    problems = adapter.validate(soft=True)
    assert any("MainAgentBuildConfig" in item for item in problems), (
        f"build config 键不兼容未在加载期暴露：{problems}"
    )


def test_optional_build_config_key_is_omitted_on_legacy_host() -> None:
    """``fallback_provider_ids`` 是 4.29 才有的字段：旧宿主没声明就不得硬传。

    4.23.3 / 4.27.2 的 ``MainAgentBuildConfig`` 是 ``@dataclass``，多传一个未知
    关键字就是加载期 RuntimeError——插件在 metadata 声明的最低版本上直接装不上。
    旧宿主的回退走 ``provider_settings.fallback_chat_models``（已随整棵
    provider_settings 透传），所以省略该键不会丢掉回退能力。

    桩必须是**显式形参**：``**kwargs`` 的桩会让这条完全测不出来，正是上面
    ``test_validate_probes_build_config_kwargs`` docstring 说的那个盲区。
    """
    runtime = _load_adapter()

    class LegacyBuildConfig:
        """4.23.3 / 4.27.2 形状：12 个显式形参，没有 ``fallback_provider_ids``。"""

        def __init__(
            self,
            *,
            tool_call_timeout: int,
            tool_schema_mode: str,
            provider_wake_prefix: str,
            streaming_response: bool,
            sanitize_context_by_modalities: bool,
            kb_agentic_mode: bool,
            file_extract_enabled: bool,
            llm_safety_mode: bool,
            safety_mode_strategy: str,
            computer_use_runtime: str,
            add_cron_tools: bool,
            provider_settings: dict,
        ) -> None:
            self.provider_settings = provider_settings

    adapter = _adapter(runtime, build_config=LegacyBuildConfig)

    assert adapter.validate(soft=True) == [], "旧宿主形状不得被可选键误判为不兼容"
    config = adapter.new_build_config(
        tool_call_timeout=60,
        tool_schema_mode="full",
        provider_wake_prefix="",
        streaming_response=False,
        sanitize_context_by_modalities=False,
        kb_agentic_mode=False,
        file_extract_enabled=False,
        llm_safety_mode=True,
        safety_mode_strategy="system_prompt",
        computer_use_runtime="none",
        add_cron_tools=False,
        provider_settings={"fallback_chat_models": ["fb-a"], "request_max_retries": 1},
        fallback_provider_ids=["fb-a"],
        request_max_retries=1,
    )
    # 旧宿主只能靠 provider_settings 那条通道拿到回退名单
    assert config.provider_settings == {"fallback_chat_models": ["fb-a"], "request_max_retries": 1}


def test_optional_build_config_key_is_passed_on_new_host() -> None:
    """新宿主声明了该字段就必须真的传进去，否则回退链静默失效。"""
    runtime = _load_adapter()

    class NewBuildConfig:
        def __init__(self, *, tool_call_timeout, fallback_provider_ids):
            self.fallback_provider_ids = fallback_provider_ids

    adapter = _adapter(runtime, build_config=NewBuildConfig)
    config = adapter.new_build_config(tool_call_timeout=60, fallback_provider_ids=["fb-a", "fb-b"])
    assert config.fallback_provider_ids == ["fb-a", "fb-b"]


# ============================================================================
# 工具边界
# ============================================================================


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class _ToolSet:
    def __init__(
        self,
        tools,
        *,
        remove_boom: bool = False,
        none_after: bool = False,
        remove_noop: bool = False,
    ) -> None:
        self.tools = tools
        self._remove_boom = remove_boom
        self._none_after = none_after
        self._remove_noop = remove_noop

    def remove_tool(self, name: str) -> None:
        if self._remove_boom:
            raise RuntimeError("remove broken")
        if self._none_after:
            self.tools = None
            return
        if self._remove_noop:
            return
        self.tools = [tool for tool in self.tools if tool.name != name]


def test_filter_final_tools_skip_nameless_and_fail_closed() -> None:
    runtime = _load_adapter()
    adapter = _adapter(runtime)

    # 白名单模式：移除未列入工具，保留白名单内工具
    tool_set = _ToolSet([_Tool("danger"), _Tool("safe")])
    req = SimpleNamespace(func_tool=tool_set)
    assert adapter.filter_final_tools(req, keep=frozenset({"safe"})) is True
    assert [tool.name for tool in tool_set.tools] == ["safe"]

    # 空名工具在白名单（keep）模式下按 <unnamed> 违规 fail closed
    anon_set = _ToolSet([_Tool(""), _Tool("danger")])
    anon_req = SimpleNamespace(func_tool=anon_set)
    assert adapter.filter_final_tools(anon_req, keep=frozenset()) is False

    # 空名工具无法与拒绝名单比对，仍需中止。
    nameless_set = _ToolSet([_Tool(""), _Tool("danger")])
    req2 = SimpleNamespace(func_tool=nameless_set)
    drop = frozenset({"danger"})
    assert adapter.filter_final_tools(req2, drop=drop) is False
    # 名字可读的 drop 命中仍正常移除后放行
    named_set = _ToolSet([_Tool("ok"), _Tool("danger")])
    named_req = SimpleNamespace(func_tool=named_set)
    assert adapter.filter_final_tools(named_req, drop=drop) is True
    assert [tool.name for tool in named_set.tools] == ["ok"]

    # remove_tool 抛错 → False（fail closed）
    boom_set = _ToolSet([_Tool("x")], remove_boom=True)
    assert (
        adapter.filter_final_tools(SimpleNamespace(func_tool=boom_set), keep=frozenset()) is False
    )

    # 移除后枚举失败（tools 变 None）→ False（fail closed）
    vanish_set = _ToolSet([_Tool("x")], none_after=True)
    assert (
        adapter.filter_final_tools(SimpleNamespace(func_tool=vanish_set), keep=frozenset()) is False
    )


@pytest.mark.parametrize(
    ("keep", "drop"),
    [(frozenset({"safe"}), frozenset()), (None, frozenset({"danger"}))],
)
def test_filter_final_tools_refuses_incomplete_removal(keep, drop) -> None:
    runtime = _load_adapter()
    adapter = _adapter(runtime)
    tool_set = _ToolSet([_Tool("danger"), _Tool("safe")], remove_noop=True)
    assert (
        adapter.filter_final_tools(SimpleNamespace(func_tool=tool_set), keep=keep, drop=drop)
        is False
    )


# ============================================================================
# 4.30 会话读结果 → 宿主可解引用的会话对象
# ============================================================================


def _read_model():
    """4.30 ``ConversationRead`` 的关键字段：conversation_id / content / persona_id。"""
    return SimpleNamespace(
        conversation_id="conv-430",
        persona_id="p1",
        content=[{"role": "user", "content": "在吗"}],
        token_usage=0,
    )


def test_legacy_conversation_converts_read_model() -> None:
    """读结果缺 ``cid`` 时必须经宿主的 to_legacy_conversation 补齐。

    宿主在显式请求路径上不再自己转换：build 的 event writer 读
    ``req.conversation.cid``/``.revision``，persona 读 ``.persona_id``，
    历史回写也看 ``req.conversation`` 是否为空。
    """
    runtime = _load_adapter()
    adapter = _adapter(runtime)
    converted = SimpleNamespace(cid="conv-430", persona_id="p1", history="[]", revision=object())
    context = SimpleNamespace(
        conversation_manager=SimpleNamespace(to_legacy_conversation=lambda record: converted)
    )

    assert adapter.legacy_conversation(_read_model(), context) is converted


def test_legacy_conversation_passes_through_legacy_object() -> None:
    """老宿主的会话对象已带 cid，不经过转换器。"""
    runtime = _load_adapter()
    adapter = _adapter(runtime)
    legacy = SimpleNamespace(cid="conv-1", persona_id=None, history="[]")
    context = SimpleNamespace(
        conversation_manager=SimpleNamespace(
            to_legacy_conversation=lambda record: pytest.fail("不该转换老宿主对象")
        )
    )

    assert adapter.legacy_conversation(legacy, context) is legacy


def test_legacy_conversation_without_converter_returns_original() -> None:
    """宿主没有转换器（旧 API）时原样返回，由调用方按形状决定是否回填。"""
    runtime = _load_adapter()
    adapter = _adapter(runtime)
    record = _read_model()

    assert adapter.legacy_conversation(record, SimpleNamespace()) is record


def test_legacy_conversation_swallows_converter_failure() -> None:
    """转换器抛错只损失本次回填，不得让整轮生成失败。"""
    runtime = _load_adapter()
    adapter = _adapter(runtime)
    record = _read_model()

    def boom(_record):
        raise ValueError("bad record")

    context = SimpleNamespace(conversation_manager=SimpleNamespace(to_legacy_conversation=boom))

    assert adapter.legacy_conversation(record, context) is record
