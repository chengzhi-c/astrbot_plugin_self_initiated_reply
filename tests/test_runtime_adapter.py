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


async def test_call_event_hook_awaits_async_callback() -> None:
    """call_event_hook 两分支（req 缺省/显式）对异步回调都走 maybe_await 正常 await。

    utils.maybe_await 是唯一实现，本测试锁住适配层调用点
    （此前该分支零覆盖：若导入/传参错误，测试不红）。
    """
    runtime = _load_adapter()
    calls: list[str] = []

    async def async_hook(event, event_type, req=None):
        calls.append(str(req))
        return "async-result"

    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, call_event_hook=async_hook)
    )
    assert await adapter.call_event_hook("evt", "OnLLMRequestEvent") == "async-result"
    assert await adapter.call_event_hook("evt", "OnLLMRequestEvent", req="req") == "async-result"
    assert calls == ["None", "req"]


async def test_call_event_hook_passes_through_sync_callback() -> None:
    """同步回调（非可等待值）原样返回，不误 await。"""
    runtime = _load_adapter()
    adapter = runtime.AstrBotRuntimeAdapter(
        base_runtime_capabilities(runtime, call_event_hook=lambda e, t, req=None: "sync")
    )
    assert await adapter.call_event_hook("evt", "t") == "sync"


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

    宿主那一步的精确位置（真机读源码确证，不是推断）：
    ``core/star/filter/command.py::CommandFilter.init_handler_md`` 在 4.23.3 是
    ``inspect.signature(handler)``，4.27.2 起是
    ``inspect.signature(handler, eval_str=True)``。一个参数之差，让
    ``from __future__ import annotations`` 产出的字符串注解在加载期真的被 eval，
    于是 TYPE_CHECKING-only 的名字在那里 NameError，插件整体拒绝加载。
    线上实测报错：``name 'CommandReply' is not defined``。

    4.23.3 上「宿主只读 signature.parameters」曾成立，故此前把 ``CommandReply``
    放在 TYPE_CHECKING 块里是安全的；4.27.2 起该前提失效。本测试用与宿主同一个
    调用（``eval_str=True``，非"等价物"）复现那一步，把「注解必须运行时可解析」
    钉成契约，不再依赖对宿主内部实现的假设。真机宿主上的同源守卫是
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
    # 指令族数量与 scripts/compat_check.py 的 EXPECTED_HANDLER_COUNT 同源：
    # 少于该数说明漏扫了（脚本侧同名常量防「检查空转」假绿）。
    from scripts.compat_check import EXPECTED_HANDLER_COUNT

    assert len([n for n in handler_names if n.startswith("selfreply")]) == EXPECTED_HANDLER_COUNT

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
    """validate 不检查 build_config：缺失时由 new_build_config 显式抛错。"""
    runtime = _load_adapter()
    adapter = _adapter(runtime, build_config=None)
    with pytest.raises(RuntimeError, match="MainAgentBuildConfig"):
        adapter.new_build_config(tool_schema_mode="full")


# ============================================================================
# final_tool_ids / filter_final_tools：fail-closed 边界
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


def test_final_tool_ids_degraded_shapes() -> None:
    runtime = _load_adapter()
    adapter = _adapter(runtime)
    # 无工具集 → 空列表（无可达工具，非枚举失败）
    assert adapter.final_tool_ids(SimpleNamespace(func_tool=None)) == []
    # 工具集无 tools 属性 → None（fail closed 信号）
    assert adapter.final_tool_ids(SimpleNamespace(func_tool=object())) is None

    class BoomIterable:
        def __iter__(self):
            raise RuntimeError("iterate broken")

    assert (
        adapter.final_tool_ids(SimpleNamespace(func_tool=SimpleNamespace(tools=BoomIterable())))
        is None
    )


def test_filter_final_tools_skip_nameless_and_fail_closed() -> None:
    runtime = _load_adapter()
    adapter = _adapter(runtime)

    # 白名单模式：移除未列入工具，保留白名单内工具
    tool_set = _ToolSet([_Tool("danger"), _Tool("safe")])
    req = SimpleNamespace(func_tool=tool_set)
    assert adapter.filter_final_tools(req, keep=frozenset({"safe"})) is True
    assert [tool.name for tool in tool_set.tools] == ["safe"]

    # 空名工具在黑名单模式下被跳过（不移除也不阻断）
    nameless_set = _ToolSet([_Tool(""), _Tool("danger")])
    req2 = SimpleNamespace(func_tool=nameless_set)
    assert adapter.filter_final_tools(req2, drop=frozenset({"danger"})) is True
    assert [tool.name for tool in nameless_set.tools] == [""]

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
    （直接 return，不经 ``final_tool_ids``），但这是实现细节，若日后把它改成
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


def test_final_tool_ids_separates_unreadable_from_empty() -> None:
    """枚举器把「读不到 ``func_tool``」与「查过了、是空的」分开。

    与上一个用例刻意分开：那个盯 ``filter_final_tools`` 的决策出口，这个盯
    ``final_tool_ids`` 的枚举出口。两处的 ``getattr`` 默认值各改一处都会被
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

    assert adapter.final_tool_ids(NoFuncTool()) is None  # 查不到
    assert adapter.final_tool_ids(SimpleNamespace(func_tool=None)) == []  # 查过了，是空的


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

    历史缺陷：final_tool_ids 与 filter_final_tools 各自 warning，单次失败
    经 filter → final_tool_ids 会打出两条同源告警，叠加 generation 调用方
    的第三条。现约定：底层枚举器降 DEBUG，决策点 filter 保留 WARNING。
    """
    import logging

    runtime = _load_adapter()
    adapter = _adapter(runtime)

    # 移除后枚举失败：filter 内部会再调 final_tool_ids，最易产生重复告警
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
