"""runtime_adapter 契约与宿主私有符号收敛。

覆盖：AgentRuntimeCapabilities 校验（缺失即红、软模式告警）、窄方法出口、
call_event_hook 两分支、处理器注解运行时可解析（复现宿主 4.27.2 的
eval_str=True 加载路径）与 astrbot.core 私有层 import 泄漏守卫。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .host_stubs import (
    ROOT,
    base_runtime_capabilities,
    capture_logs,
    load_modules,
    production_py_files,
)

PACKAGE_NAME = "selfreply_runtime_test_package"

_PRIVATE_IMPORT_RE = r"(^|\n)\s*(from|import)\s+astrbot\.core"
_SYMBOL_WHITELIST = {
    "runtime_adapter.py",
    "scripts/compat_check.py",
    "tests/host_stubs.py",
}


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


def test_runtime_adapter_reports_signature_mismatch() -> None:
    runtime = _load_adapter()

    async def incompatible(*, event, plugin_context, config, req):
        return None

    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, build_main_agent=incompatible)
    )

    with pytest.raises(RuntimeError, match="apply_reset"):
        adapter.validate()


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
            self.description = ""
            self.parameters = {}
            self.handler = None

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


async def test_guard_final_tools_preserves_schema_and_blocks_handler() -> None:
    """Schema 保留模式只替换 handler，不删除工具或修改缓存字段。"""
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))

    class ToolSet:
        def __init__(self):
            self.tools = []

        def add_tool(self, tool):
            for index, existing in enumerate(self.tools):
                if existing.name == tool.name:
                    self.tools[index] = tool
                    return
            self.tools.append(tool)

    called = False

    async def original_handler(_event, **_kwargs):
        nonlocal called
        called = True
        return "executed"

    dangerous = SimpleNamespace(
        name="future_task",
        description="schedule work",
        parameters={"type": "object", "properties": {}},
        handler=original_handler,
    )
    safe = SimpleNamespace(
        name="send_message_to_user",
        description="send",
        parameters={"type": "object", "properties": {}},
        handler=original_handler,
    )
    tool_set = ToolSet()
    tool_set.add_tool(dangerous)
    tool_set.add_tool(safe)
    req = SimpleNamespace(func_tool=tool_set)

    assert adapter.guard_final_tools(req, block=frozenset({"future_task"})) is True
    assert [tool.name for tool in tool_set.tools] == ["future_task", "send_message_to_user"]
    guarded = tool_set.tools[0]
    assert guarded is not dangerous
    assert guarded.description == dangerous.description
    assert guarded.parameters == dangerous.parameters
    assert callable(guarded.handler)
    assert "disabled during proactive replies" in await guarded.handler(None)
    assert called is False
    assert tool_set.tools[1] is safe


# ============================================================================
# validate 契约：缺失即红、软模式告警
# ============================================================================


def test_validate_full_contract_green() -> None:
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))
    assert adapter.validate() == []


def test_validate_event_result_missing_method_red() -> None:
    """事件结果缺 set_result_content_type：缺失即红（硬模式 raise）。"""
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


def test_validate_provider_request_missing_field_red() -> None:
    runtime = _load_adapter()
    caps = base_runtime_capabilities(
        runtime,
        provider_request_cls=type(
            "BrokenReq", (), {"prompt": "", "image_urls": [], "func_tool": None}
        ),
    )
    adapter = runtime.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="session_id"):
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


def test_narrow_symbol_accessors() -> None:
    """适配层窄方法：事件结果/内容类型/事件类型/请求构造经适配层唯一出口。"""
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime))
    result = adapter.new_event_result()
    assert callable(result.message) and callable(result.set_result_content_type)
    assert adapter.result_llm_type == "llm"
    assert adapter.event_type.OnLLMRequestEvent == "OnLLMRequestEvent"
    req = adapter.new_provider_request()
    assert req.session_id == ""


def test_runtime_contract_checks_listed() -> None:
    """compat_check 的存在性清单与适配层契约单源（增删符号必须同步）。"""
    runtime = _load_adapter()
    contract = dict(runtime.AstrBotRuntimeAdapter.host_contract())
    for mod, attrs in contract.items():
        assert isinstance(mod, str) and isinstance(attrs, list) and attrs
    assert "astrbot.core.message.message_event_result" in contract
    assert "astrbot.core.star.star_handler" in contract


def test_command_handler_annotations_resolve_at_runtime() -> None:
    """所有宿主注册的处理器注解必须在运行时可解析。

    宿主那一步的精确位置：
    ``core/star/filter/command.py::CommandFilter.init_handler_md`` 在 4.23.3 是
    ``inspect.signature(handler)``，4.27.2 起是
    ``inspect.signature(handler, eval_str=True)``。一个参数之差，让
    ``from __future__ import annotations`` 产出的字符串注解在加载期真的被 eval，
    于是 TYPE_CHECKING-only 的名字在那里 NameError，插件整体拒绝加载。
    线上实测报错：``name 'CommandReply' is not defined``。

    4.23.3 上「宿主只读 signature.parameters」曾成立，故此前把 ``CommandReply``
    放在 TYPE_CHECKING 块里是安全的；4.27.2 起该前提失效。本测试用与宿主同一个
    调用（``eval_str=True``，非"等价物"）复现那一步，把「注解必须运行时可解析」
    钉成契约，不依赖对宿主内部实现的假设。宿主上的同源守卫是
    ``scripts/compat_check.py::_handler_signature_gaps``。

    变异验证：把 main.py 的 ``CommandReply = AsyncGenerator[Any, None]`` 移回
    ``if TYPE_CHECKING:`` 块内，本测试即红（NameError: CommandReply）。
    """
    import inspect

    main_mod = load_modules(PACKAGE_NAME, "main")[0]
    plugin_cls = main_mod.SelfInitiatedReplyPlugin

    # 宿主注册的两类处理器：event_message_type 钩子与 /selfreply 指令族。
    handler_names = [
        name for name in dir(plugin_cls) if name == "on_message" or name.startswith("selfreply")
    ]
    assert "on_message" in handler_names
    # 处理器名单与 scripts/compat_check.py 的 EXPECTED_HANDLER_NAMES 同源：
    # 指令组本体（selfreply）被装饰器换成 RegisteringCommandable，不在名单里，
    # 摘掉它之后剩下的必须与名单一一对应（改名即红，脚本侧同份名单防假绿）。
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
# 宿主私有层 import 收敛
# ============================================================================


def test_private_host_symbols_confined() -> None:
    """宿主私有层 import 只出现在适配层、宿主桩与兼容检查。"""
    import re

    pattern = re.compile(_PRIVATE_IMPORT_RE)
    violations = []
    for path in production_py_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in _SYMBOL_WHITELIST:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            violations.append(rel)
    assert not violations, f"宿主私有层 import 泄漏到：{', '.join(violations)}"


# ============================================================================
# 降级宿主分支与工具过滤边界（validate 告警、路径回退、fail-closed、run 直通）
# ============================================================================
def _adapter(runtime, **overrides):
    return runtime.AstrBotRuntimeAdapter(base_runtime_capabilities(runtime, **overrides))


# ============================================================================
# validate：降级宿主告警分支（软模式收集，硬模式首错即红）
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

    变异锚定：把该 ``or`` 改成 ``and``，本用例红。
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


# ============================================================================
# 直通出口：run_agent 属性与 run 包装、build_config 缺失
# ============================================================================


def test_run_agent_property_and_run_passthrough() -> None:
    runtime = _load_adapter()
    calls: list[tuple] = []

    def fake_run_agent(agent_runner, **kwargs):
        calls.append((agent_runner, tuple(sorted(kwargs))))
        return ()

    adapter = _adapter(runtime, run_agent=fake_run_agent)
    assert adapter.run_agent is fake_run_agent
    assert adapter.run("runner", max_step=3) == ()
    assert calls == [("runner", ("max_step",))]


def test_new_build_config_missing_type_raises() -> None:
    """build_config 缺失：validate 报问题，运行期出口显式抛错（双保险）。"""
    runtime = _load_adapter()
    adapter = _adapter(runtime, build_config=None)
    assert any("缺少 MainAgentBuildConfig" in item for item in adapter.validate(soft=True))
    with pytest.raises(RuntimeError, match="MainAgentBuildConfig"):
        adapter.new_build_config(tool_schema_mode="full")


def test_validate_probes_build_config_kwargs() -> None:
    """加载期必须试实例化 MainAgentBuildConfig：键不兼容要在加载期红。

    缺陷形态：探针只核验 build/run 的签名，build config 的**键**集合只有
    实例化能验。宿主改一个键名时插件加载成功、每次主动生成在运行期
    TypeError（一条回复产不出），而 compat_check 与测试全绿——桩的 build
    config 都是 **kwargs，故障只在真实宿主可见。
    """
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
        provider_settings={"fallback_chat_models": ["fb-a"]},
        fallback_provider_ids=["fb-a"],
    )
    # 旧宿主只能靠 provider_settings 那条通道拿到回退名单
    assert config.provider_settings == {"fallback_chat_models": ["fb-a"]}


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
# _tool_list / filter_final_tools：fail-closed 边界
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


def test_tool_list_degraded_shapes() -> None:
    runtime = _load_adapter()
    adapter = _adapter(runtime)
    # 无工具集 → 空列表（无可达工具，非枚举失败）
    assert adapter._tool_list(SimpleNamespace(func_tool=None)) == []
    # 工具集无 tools 属性 → None（fail closed 信号）
    assert adapter._tool_list(SimpleNamespace(func_tool=object())) is None

    class BoomIterable:
        def __iter__(self):
            raise RuntimeError("iterate broken")

    assert (
        adapter._tool_list(SimpleNamespace(func_tool=SimpleNamespace(tools=BoomIterable()))) is None
    )

    class BoomProperty:
        @property
        def tools(self):
            raise RuntimeError("property broken")

    # 属性读取本身抛也必须 fail closed：逃出 _tool_list 会让"无法枚举"变成
    # 一次裸异常，调用方只能按 generate 的兜底分支处理，归因更糊。
    assert adapter._tool_list(SimpleNamespace(func_tool=BoomProperty())) is None


class BoomFuncTool:
    """宿主把 func_tool 实现成会抛非 AttributeError 的 property。

    ``getattr(req, "func_tool", _MISSING)`` 的默认值只在抛 AttributeError 时
    生效，这种形态会原样逃出。加载期 ``_PROVIDER_REQUEST_FIELDS`` 断言也不覆盖：
    ``dir()`` 照样列出这个 property，``validate()`` 因此通过。
    """

    @property
    def func_tool(self):
        raise RuntimeError("host property broken")


def test_read_func_tool_converges_raising_property_to_sentinel() -> None:
    """读 func_tool 抛异常必须收敛成哨兵，不得以裸异常逃出 fail-closed 接缝。"""
    runtime = _load_adapter()
    adapter = _adapter(runtime)
    boom = BoomFuncTool()
    assert adapter._read_func_tool(boom) is runtime._MISSING
    # 哨兵化后 _tool_list 判"无法枚举"，filter_final_tools 据此 abort
    assert adapter._tool_list(boom) is None
    assert adapter.filter_final_tools(boom, keep=frozenset()) is False


def test_func_tool_turning_unreadable_after_enumeration_fails_closed(caplog: object) -> None:
    """枚举成功之后 func_tool 变读不到：不猜，直接判失败，且告警点明原因。

    形态是宿主属性在两次读取之间被置空，或开始抛异常（请求被重置、宿主重入）。
    此时 470 行的 ``req.func_tool`` 会以裸异常或 ``None.tools`` 的
    AttributeError 逃出 try 之前的代码。

    注意不能写成"第一次读就读不到"：那会被 ``_tool_list`` 按"宿主声明本次无
    工具"判成 ``[]``，走的是 466 行的平凡放行出口，够不到这条分支。

    只断言 ``is False`` 不够：去掉显式守卫后 ``None.tools`` 仍会被 except 兜住
    并返回 False（实测过），guard 的增量价值全在归因上，故一并钉住告警文案。
    第二次读用**抛异常**而不是返回 None，是为了让本用例同时抓住"三个调用点
    换回裸读取"这一变异——裸读会让 RuntimeError 直接逃出本方法。
    """
    import logging

    runtime = _load_adapter()
    adapter = _adapter(runtime)

    class VanishingReq:
        def __init__(self) -> None:
            self.tool_set = _ToolSet([_Tool("safe")])
            self.reads = 0

        @property
        def func_tool(self):
            self.reads += 1
            if self.reads == 1:
                return self.tool_set
            raise RuntimeError("host property broken")

    # 前提：第一次枚举确实成功（否则本用例走的是平凡放行，什么都验不到）
    healthy = VanishingReq()
    assert adapter._tool_list(healthy) == ["safe"]

    with capture_logs(caplog, runtime.logger, logging.DEBUG):
        assert adapter.filter_final_tools(VanishingReq(), keep=frozenset({"safe"})) is False
    rendered = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert len(rendered) == 1, f"fail-closed 打出 {len(rendered)} 条告警：{rendered}"
    assert "became unreadable" in rendered[0], f"告警未点明真实原因：{rendered}"


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

    # denylist（drop）模式同理：名字读不到就无从判断它是否在 drop 里，
    # 「无法强制」按契约 §3 终止运行。旧实现在这里放行（`if name and ...`
    # 让空名工具对核验隐形），宿主把 tools 换成名字不可读的容器时，整个
    # 危险工具集会静默穿过 inherit 模式这道最后的防线。
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


def test_filter_final_tools_violation_warning_names_offenders(caplog: object) -> None:
    """keep 核验失败必须点名违规工具；匿名工具以 <unnamed> 呈现。

    修复前该出口返回 False 却零日志（只有调用方一句泛化警告），匿名宿主
    工具会静默中止整轮主动回复，无从排查。fail-closed 行为本身不变。
    """
    import logging

    runtime = _load_adapter()
    adapter = _adapter(runtime)

    # remove_tool 成功但集合未变（宿主假移除）：具名违规 + 匿名各一
    sneaky_set = _ToolSet([_Tool("rogue"), _Tool("")], remove_noop=True)
    with capture_logs(caplog, runtime.logger, logging.WARNING):
        assert (
            adapter.filter_final_tools(
                SimpleNamespace(func_tool=sneaky_set), keep=frozenset({"safe"})
            )
            is False
        )
    messages = [record.getMessage() for record in caplog.records]
    named = [message for message in messages if "non-allowlisted tools remain" in message]
    assert len(named) == 1, f"违规出口应有且仅有一条点名告警：{messages}"
    assert "rogue" in named[0] and "<unnamed>" in named[0], named[0]


def test_filter_final_tools_drop_violation_warning_names_offenders(caplog: object) -> None:
    """drop 模式同理：危险工具移除失败也要点名。"""
    import logging

    runtime = _load_adapter()
    adapter = _adapter(runtime)
    sneaky_set = _ToolSet([_Tool("danger"), _Tool("ok")], remove_noop=True)
    with capture_logs(caplog, runtime.logger, logging.WARNING):
        assert (
            adapter.filter_final_tools(
                SimpleNamespace(func_tool=sneaky_set), drop=frozenset({"danger"})
            )
            is False
        )
    messages = [record.getMessage() for record in caplog.records]
    assert any("denied tools remain: danger" in message for message in messages), messages


def test_missing_func_tool_attribute_fails_closed_not_open(caplog: object) -> None:
    """``func_tool`` 属性缺失必须 fail closed，不能与显式 ``None`` 同一出口。

    修复前实测：``getattr(req, "func_tool", None)`` 把两种情形压成一个出口，
    「缺属性」与「显式 None」都返回 ``True``，白名单模式下等于整次放行，
    而白名单模式的默认白名单是空集（本该移除全部工具）。

    两者语义相反：显式 ``None`` 是宿主声明本次无工具（放行正确）；属性缺失是
    读不到工具边界本身，无法枚举、无法移除、无法核验，只能中止。

    末段一并锁住低噪音约定：本出口同样只许一条 WARNING。它现在天然满足
    （直接 return，不经 ``_tool_list``），但这是实现细节，若日后把它改成
    先枚举再判定，就会与 ``test_fail_closed_emits_exactly_one_warning`` 记录的
    历史缺陷同形（同源告警打两条），故在此就地钉住。
    """
    import logging

    runtime = _load_adapter()
    adapter = _adapter(runtime)

    class NoFuncTool:
        """连 func_tool 属性都没有的 req（宿主改名该字段后的形态）。"""

    # 缺属性 → fail closed（两种模式都必须拦）
    with capture_logs(caplog, runtime.logger, logging.DEBUG):
        assert adapter.filter_final_tools(NoFuncTool(), keep=frozenset()) is False
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    rendered = [record.getMessage() for record in warnings]
    assert len(warnings) == 1, f"缺属性 fail-closed 打出 {len(warnings)} 条告警：{rendered}"
    assert "func_tool" in rendered[0], f"告警未点明缺哪个字段：{rendered}"

    assert adapter.filter_final_tools(NoFuncTool(), drop=frozenset({"danger"})) is False

    # 显式 None 的既有语义不受影响：宿主声明无工具，放行
    assert adapter.filter_final_tools(SimpleNamespace(func_tool=None), keep=frozenset()) is True


def test_tool_list_separates_unreadable_from_empty() -> None:
    """枚举器把「读不到 ``func_tool``」与「查过了、是空的」分开。

    与上一个用例刻意分开：那个盯 ``filter_final_tools`` 的决策出口，这个盯
    ``_tool_list`` 的枚举出口。两处的 ``getattr`` 默认值各改一处都会被
    对应用例单独抓到（实测两次变异各只有一个用例变红），所以谁退化了、
    退化在哪一层，从失败用例名就能读出来。

    缺属性返回 ``[]`` 的危害不止于本方法：``filter_final_tools`` 末尾用它做
    移除后的复核，空列表会让 ``all(...)`` 在空集上恒真，把"查不到"谎报成
    "已确认干净"。
    """
    runtime = _load_adapter()
    adapter = _adapter(runtime)

    class NoFuncTool:
        """连 func_tool 属性都没有的 req。"""

    assert adapter._tool_list(NoFuncTool()) is None  # 查不到
    assert adapter._tool_list(SimpleNamespace(func_tool=None)) == []  # 查过了，是空的


def test_func_tool_stays_in_load_time_contract_assertion() -> None:
    """耦合哨兵：``func_tool`` 必须留在加载期断言的字段清单里。

    上一个用例的缺属性分支在生产上不可达，靠的正是这条加载期断言：宿主若改名
    ``func_tool``，``validate()`` 在 ``PluginMain.__init__`` 第一条语句就 raise，
    插件拒绝加载，运行期根本走不到 ``filter_final_tools``。

    危险在于这个依赖是隐式的。若有人把 ``func_tool`` 从 ``_PROVIDER_REQUEST_FIELDS``
    删掉（比如认为"这个字段不是我们直接赋值的"），加载期防线消失、运行期那条
    分支复活成真实 fail-open，而**没有任何现有用例会变红**。本用例就是那道红线。
    """
    runtime = _load_adapter()
    assert "func_tool" in runtime._PROVIDER_REQUEST_FIELDS, (
        "func_tool 已从加载期断言清单移除，filter_final_tools 的缺属性分支"
        "将从『不可达的纵深防御』变成『可达的 fail-open』，请先读该分支的 docstring"
    )

    # 正向确认这条断言真的会拦下改名：把 ProviderRequest 换成缺该字段的形态
    renamed = type(
        "RenamedFuncTool",
        (),
        {field: None for field in runtime._PROVIDER_REQUEST_FIELDS if field != "func_tool"},
    )
    problems = _adapter(runtime, provider_request_cls=renamed).validate(soft=True)
    assert any("func_tool" in problem for problem in problems), (
        f"改名 func_tool 未被加载期断言拦下：{problems}"
    )


def test_fail_closed_emits_exactly_one_warning(caplog: object) -> None:
    """工具边界 fail-closed 每次失败只允许一条 WARNING（低噪音日志约定）。

    历史缺陷：_tool_list 与 filter_final_tools 各自 warning，单次失败
    经 filter → _tool_list 会打出两条同源告警，叠加 generation 调用方
    的第三条。现约定：底层枚举器降 DEBUG，决策点 filter 保留 WARNING。
    """
    import logging

    runtime = _load_adapter()
    adapter = _adapter(runtime)

    # 移除后枚举失败：filter 内部会再调 _tool_list，最易产生重复告警
    vanish_set = _ToolSet([_Tool("x")], none_after=True)
    with capture_logs(caplog, runtime.logger, logging.DEBUG):
        assert (
            adapter.filter_final_tools(SimpleNamespace(func_tool=vanish_set), keep=frozenset())
            is False
        )
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    rendered = [record.getMessage() for record in warnings]
    assert len(warnings) == 1, f"单次 fail-closed 打出 {len(warnings)} 条告警：{rendered}"
    # 仍须留下可排查线索（降级为 DEBUG 的枚举细节不算噪音）
    assert "fail-closed" in rendered[0]


def test_fail_closed_warning_names_the_reason(caplog: object) -> None:
    """告警须点明失败原因，否则 fail-closed 静默等同无日志。"""
    import logging

    runtime = _load_adapter()
    adapter = _adapter(runtime)

    with capture_logs(caplog, runtime.logger, logging.WARNING):
        result = adapter.filter_final_tools(SimpleNamespace(func_tool=object()), keep=frozenset())
    assert result is False
    messages = [record.getMessage() for record in caplog.records]
    assert any("tools" in message for message in messages), f"告警未点明原因：{messages}"


# ============================================================================
# _require：各入口不再逐次 validate 之后的运行期唯一 None 兜底
# ============================================================================


def test_require_is_the_only_runtime_none_guard() -> None:
    """入口取消逐次 validate 后，``_require`` 成为运行期唯一的 None 兜底。

    加载期守卫（``SelfInitiatedReplyPlugin.__init__`` → ``validate()`` 硬模式）拒绝不兼容
    宿主，但软模式只收集问题、不阻断。此时访问入口必须仍然抛出，且文案须来自
    ``_require``：``_probe_problems`` 的消息在软模式下已被调用方吞掉，若 ``_require``
    的 raise 被改成静默返回，None 会漏进宿主调用并在更深处以难诊断的形态崩溃。

    判别串取 ``_require`` 独有的「所需的 」（后跟空格）：``_probe_problems`` 的
    tool_set 分支写「缺少主 Agent ToolSet，无法建立…」，import_error 分支写
    「所需的主 Agent API」（无空格），两者都不会误命中本断言。
    """
    runtime = _load_adapter()

    # property 入口
    adapter = _adapter(runtime, tool_set=None)
    assert any("ToolSet" in item for item in adapter.validate(soft=True))
    with pytest.raises(RuntimeError, match="缺少主动回复所需的 主 Agent ToolSet"):
        _ = adapter.tool_set

    # 方法入口
    adapter = _adapter(runtime, event_result_cls=None)
    assert any("MessageEventResult" in item for item in adapter.validate(soft=True))
    with pytest.raises(RuntimeError, match="缺少主动回复所需的 MessageEventResult"):
        adapter.new_event_result()
