"""runtime_adapter.py 覆盖率补盲（0.9.0 P2）：降级宿主分支与工具过滤边界。

盲区背景（补盲前全量口径 86%，32 行未覆盖）：validate 的降级宿主告警分支、
路径函数回退、final_tool_ids/filter_final_tools 的 fail-closed 边界、
run_agent 直通出口。宿主兼容层最容易在宿主升级时出问题，本文件为其
补上模块级门禁前的行为覆盖（捕获力经门禁入库后由 CI 守护）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .host_stubs import load_package

PACKAGE_NAME = "selfreply_runtime_test_package"


def _load_adapter():
    return load_package(PACKAGE_NAME, "runtime_adapter")


def _base_caps(runtime, **overrides):
    """完整契约 capabilities（与 test_runtime_adapter 同形）。"""
    base = dict(
        import_error=None,
        tool_set=object,
        build_config=object,
        build_main_agent=lambda **_k: None,
        get_session_conv=lambda *_a: None,
        run_agent=lambda *_a, **_k: (),
        event_result_cls=type(
            "Result",
            (),
            {"message": lambda self, t: self, "set_result_content_type": lambda self, t: self},
        ),
        result_content_type=type("CT", (), {"LLM_RESULT": "llm"}),
        event_type=type(
            "ET",
            (),
            {
                "OnLLMRequestEvent": "OnLLMRequestEvent",
                "OnDecoratingResultEvent": "OnDecoratingResultEvent",
                "OnAfterMessageSentEvent": "OnAfterMessageSentEvent",
            },
        ),
        call_event_hook=lambda *_a, **_k: True,
        provider_request_cls=type(
            "Req",
            (),
            {
                "prompt": "",
                "image_urls": [],
                "audio_urls": [],
                "func_tool": None,
                "session_id": "",
                "conversation": None,
                "contexts": [],
            },
        ),
    )
    base.update(overrides)
    return runtime.AgentRuntimeCapabilities(**base)


def _adapter(runtime, **overrides):
    return runtime.AstrBotRuntimeAdapter(_base_caps(runtime, **overrides))


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


def test_path_fallbacks_return_none() -> None:
    """路径函数缺失/抛错：回退 None（旧版宿主降级路径）。"""
    runtime = _load_adapter()
    adapter = _adapter(runtime, config_path_fn=None, plugin_data_path_fn=None)
    assert adapter.config_path() is None
    assert adapter.plugin_data_path() is None

    def boom() -> str:
        raise OSError("path broken")

    adapter = _adapter(runtime, config_path_fn=boom, plugin_data_path_fn=boom)
    assert adapter.config_path() is None
    assert adapter.plugin_data_path() is None


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
    def __init__(self, tools, *, remove_boom: bool = False, none_after: bool = False) -> None:
        self.tools = tools
        self._remove_boom = remove_boom
        self._none_after = none_after

    def remove_tool(self, name: str) -> None:
        if self._remove_boom:
            raise RuntimeError("remove broken")
        if self._none_after:
            self.tools = None
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
    with caplog.at_level(logging.DEBUG, logger="astrbot"):
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

    with caplog.at_level(logging.WARNING, logger="astrbot"):
        result = adapter.filter_final_tools(SimpleNamespace(func_tool=object()), keep=frozenset())
    assert result is False
    messages = [record.getMessage() for record in caplog.records]
    assert any("tools" in message for message in messages), f"告警未点明原因：{messages}"


# ============================================================================
# _require：各入口不再逐次 validate 之后的运行期唯一 None 兜底
# ============================================================================


def test_require_is_the_only_runtime_none_guard() -> None:
    """入口取消逐次 validate 后，``_require`` 成为运行期唯一的 None 兜底。

    加载期守卫（``main._validate_agent_api`` → ``validate()`` 硬模式）拒绝不兼容
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
