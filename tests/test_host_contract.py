"""宿主符号收敛契约（ticket 13）：私有 core 符号只允许出现在适配层与宿主桩。

验收项：
- 全仓库 grep：宿主私有符号只出现在适配层与宿主桩两处（文本收敛断言）
- 契约断言覆盖全部私有入口（构建/运行/会话装载/事件结果/请求/钩子），缺失即红
- 兼容检查软模式（最新版漂移预警）收集告警不阻塞，硬模式（锁定版）首错即红
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from .host_stubs import ROOT
from .source_contract import callers_of, defines, source_of
from .test_vision import PACKAGE_NAME

# 宿主私有层 import 语句：全仓库只允许出现在收敛点（适配层、宿主桩、兼容检查）
_PRIVATE_IMPORT_RE = r"(^|\n)\s*(from|import)\s+astrbot\.core"
# 私有符号唯一允许出现的收敛点（适配层、宿主桩、兼容检查脚本）
_SYMBOL_WHITELIST = {
    "runtime_adapter.py",
    "scripts/compat_check.py",
    "tests/host_stubs.py",
}
# 除白名单外，其余源文件一律禁止 import 宿主私有层（astrbot.core）
_CHECKED_MODULES = [
    "adapters.py",
    "commands.py",
    "decision.py",
    "delivery.py",
    "generation.py",
    "main.py",
    "outbound.py",
    "scheduler.py",
    "session_coordinator.py",
    "session_gate.py",
    "storage.py",
    "utils.py",
    "webapi.py",
    "whitelist.py",
    "image/extractor.py",
    "image/models.py",
    "image/parser.py",
    "image/recorder_bridge.py",
]


def test_private_host_symbols_confined() -> None:
    """宿主私有层 import 只出现在适配层与宿主桩两处（缺失即红）。"""
    import re

    pattern = re.compile(_PRIVATE_IMPORT_RE)
    violations = []
    for rel in _CHECKED_MODULES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        if pattern.search(src):
            violations.append(rel)
    assert not violations, f"宿主私有层 import 泄漏到：{', '.join(violations)}"


def test_callback_protocols_single_source() -> None:
    """复审 S2：回调 Protocol 只允许在共享模块 models 定义一份（防镜像）。"""
    import re

    def protocol_definitions(rel: str) -> list[str]:
        text = (ROOT / rel).read_text(encoding="utf-8")
        return re.findall(r"^class (ReadHistoryCallback|ImageContextCallback)\b", text, re.M)

    for rel in ("decision.py", "generation.py"):
        assert not protocol_definitions(rel), f"{rel} 仍自行定义回调 Protocol"
    assert sorted(protocol_definitions("models.py")) == [
        "ImageContextCallback",
        "ReadHistoryCallback",
    ]


def test_history_budget_single_shape() -> None:
    """复审 S2：历史补全形状收敛到 utils.build_history_text，无镜像与硬编码。"""
    assert defines("utils.py", "build_history_text")
    assert callers_of("generation.py", "build_history_text") == [
        "GenerationRunner.build_context_text"
    ]
    assert callers_of("decision.py", "build_history_text") == [
        "DecisionMaker.build_recent_messages"
    ]
    # 历史条数预算只能来自配置，不得在 generation 侧硬编码上限
    assert "min(5," not in source_of("generation.py")


def test_build_config_type_dead_property_removed() -> None:
    """复审 S3：无调用的死 property（build_config_type）应移除。"""
    adapter = _runtime_adapter()
    assert not hasattr(adapter.AstrBotRuntimeAdapter, "build_config_type")


def test_host_symbol_table_single_source() -> None:
    """复审 S4：compat 符号表单源——表定义之后的代码不得散落宿主模块字面量。"""
    import ast

    src = (ROOT / "runtime_adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    def target_id(node: ast.AST) -> str:
        if isinstance(node, ast.AnnAssign):
            return getattr(node.target, "id", "")
        if isinstance(node, ast.Assign):
            return "".join(getattr(t, "id", "") for t in node.targets)
        return ""

    assign = next(node for node in tree.body if target_id(node) == "_HOST_CONTRACT")
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            if node.lineno > assign.end_lineno:
                docstrings = {
                    id(body[0].value)
                    for body in (node.body,)
                    if body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                }
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and id(child) in docstrings:
                        continue
                    if (
                        isinstance(child, ast.Constant)
                        and isinstance(child.value, str)
                        and "astrbot.core" in child.value
                    ):
                        violations.append(child.value[:60])
    assert not violations, (
        "from_host/validate 必须由 _HOST_CONTRACT 驱动，禁止散落宿主模块字面量："
        + "; ".join(violations)
    )


def test_delivery_clear_result_single_shape() -> None:
    """复审 S4：事件结果回收统一经 _clear_result，出口处不得内联 try/except。"""
    assert defines("delivery.py", "DeliveryRunner._clear_result")
    assert callers_of("delivery.py", "last_event.clear_result") == [
        "DeliveryRunner._clear_result"
    ], "宿主 clear_result 被在 _clear_result 之外直接调用（异常兜底会散落到各出口）"


def test_generation_graceful_stop_single_shape() -> None:
    """复审 S4：超时/取消两分支收敛为 _graceful_stop，request_stop 只出现一处。"""
    assert defines("generation.py", "GenerationRunner._graceful_stop")
    assert callers_of("generation.py", "request_stop") == ["GenerationRunner._graceful_stop"], (
        "宿主 request_stop 被在 _graceful_stop 之外调用（宽限等待语义会分叉）"
    )


def test_silence_remaining_on_state() -> None:
    """复审 S4：静默/活跃时间归属 SessionState，decision/scheduler 不得内联计算形状。"""
    assert defines("models.py", "SessionState.remaining_silence_sec")
    assert defines("models.py", "SessionState.age_sec")
    # decision 必须问 state 要剩余静默，而不是自己拿时钟减
    assert callers_of("decision.py", "state.remaining_silence_sec") == ["DecisionMaker.local_gate"]
    assert "_clock() - state.last_active_at" not in source_of("decision.py")
    # scheduler 侧同理：夹取归 SessionState，这里不得再套一层 max(0.0, ...)
    assert "max(0.0, silence_left)" not in source_of("scheduler.py")


def _runtime_adapter():
    from .host_stubs import install_astrbot_stubs, load_package

    install_astrbot_stubs()  # runtime_adapter 无模块级宿主 import，装桩保证包加载路径一致
    return load_package(PACKAGE_NAME, "runtime_adapter")


def _full_capabilities(adapter, **overrides):
    base = dict(
        import_error=None,
        tool_set=type("ToolSet", (), {}),
        build_config=type("BuildConfig", (), {}),
        build_main_agent=lambda **kwargs: SimpleNamespace(
            agent_runner=SimpleNamespace(), reset_coro=SimpleNamespace(close=lambda: None)
        ),
        get_session_conv=lambda event, context: SimpleNamespace(
            history="[]", context=SimpleNamespace()
        ),
        run_agent=lambda runner, **kwargs: iter(()),
        event_result_cls=type(
            "MessageEventResult",
            (),
            {"message": lambda self, text: self, "set_result_content_type": lambda self, t: self},
        ),
        result_content_type=SimpleNamespace(LLM_RESULT="llm"),
        event_type=SimpleNamespace(
            OnLLMRequestEvent="OnLLMRequestEvent",
            OnDecoratingResultEvent="OnDecoratingResultEvent",
            OnAfterMessageSentEvent="OnAfterMessageSentEvent",
        ),
        call_event_hook=lambda event, event_type, req=None: True,
        provider_request_cls=type(
            "ProviderRequest",
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
        config_path_fn=lambda: "/tmp/config",
        plugin_data_path_fn=lambda: "/tmp/data",
    )
    base.update(overrides)
    return adapter.AgentRuntimeCapabilities(**base)


def test_validate_full_contract_green() -> None:
    adapter = _runtime_adapter()
    runtime = adapter.AstrBotRuntimeAdapter(_full_capabilities(adapter))
    assert runtime.validate() == []


def test_validate_event_result_missing_method_red() -> None:
    """事件结果缺 set_result_content_type：缺失即红（硬模式 raise）。"""
    adapter = _runtime_adapter()
    caps = _full_capabilities(
        adapter,
        event_result_cls=type("BrokenResult", (), {"message": lambda self, text: self}),
    )
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="set_result_content_type"):
        runtime.validate()


def test_validate_event_type_missing_member_red() -> None:
    adapter = _runtime_adapter()
    caps = _full_capabilities(
        adapter,
        event_type=SimpleNamespace(OnDecoratingResultEvent="x", OnAfterMessageSentEvent="x"),
    )
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="OnLLMRequestEvent"):
        runtime.validate()


def test_validate_provider_request_missing_field_red() -> None:
    adapter = _runtime_adapter()
    caps = _full_capabilities(
        adapter,
        provider_request_cls=type(
            "BrokenReq", (), {"prompt": "", "image_urls": [], "func_tool": None}
        ),
    )
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="session_id"):
        runtime.validate()


def test_validate_call_event_hook_missing_red() -> None:
    adapter = _runtime_adapter()
    caps = _full_capabilities(adapter, call_event_hook=None)
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    with pytest.raises(RuntimeError, match="call_event_hook"):
        runtime.validate()


def test_validate_soft_warns_without_raising() -> None:
    """软模式（最新版漂移预警）：收集告警不阻塞。"""
    adapter = _runtime_adapter()
    caps = _full_capabilities(
        adapter,
        event_type=SimpleNamespace(OnDecoratingResultEvent="x", OnAfterMessageSentEvent="x"),
    )
    runtime = adapter.AstrBotRuntimeAdapter(caps)
    problems = runtime.validate(soft=True)
    assert any("OnLLMRequestEvent" in p for p in problems)


def test_validate_soft_green_silent() -> None:
    adapter = _runtime_adapter()
    runtime = adapter.AstrBotRuntimeAdapter(_full_capabilities(adapter))
    assert runtime.validate(soft=True) == []


def test_narrow_symbol_accessors() -> None:
    """适配层窄方法：事件结果/请求/事件类型/钩子/路径经适配层唯一出口。"""
    adapter = _runtime_adapter()
    runtime = adapter.AstrBotRuntimeAdapter(_full_capabilities(adapter))
    result = runtime.new_event_result()
    assert callable(result.message) and callable(result.set_result_content_type)
    assert runtime.result_llm_type == "llm"
    assert runtime.event_type.OnLLMRequestEvent == "OnLLMRequestEvent"
    req = runtime.new_provider_request()
    assert req.session_id == ""
    assert runtime.config_path() == "/tmp/config"
    assert runtime.plugin_data_path() == "/tmp/data"


async def test_call_event_hook_awaits_async_callback() -> None:
    """call_event_hook 两分支（req 缺省/显式）对异步回调都走 maybe_await 正常 await。

    0.8.8 单源化后 utils.maybe_await 是唯一实现，本测试锁住适配层调用点
    （此前该分支零覆盖：若导入/传参错误，测试不红）。
    """
    adapter = _runtime_adapter()
    calls: list[str] = []

    async def async_hook(event, event_type, req=None):
        calls.append(str(req))
        return "async-result"

    runtime = adapter.AstrBotRuntimeAdapter(_full_capabilities(adapter, call_event_hook=async_hook))
    assert await runtime.call_event_hook("evt", "OnLLMRequestEvent") == "async-result"
    assert await runtime.call_event_hook("evt", "OnLLMRequestEvent", req="req") == "async-result"
    assert calls == ["None", "req"]


async def test_call_event_hook_passes_through_sync_callback() -> None:
    """同步回调（非可等待值）原样返回，不误 await。"""
    adapter = _runtime_adapter()
    runtime = adapter.AstrBotRuntimeAdapter(
        _full_capabilities(adapter, call_event_hook=lambda e, t, req=None: "sync")
    )
    assert await runtime.call_event_hook("evt", "t") == "sync"


def test_host_contract_checks_listed() -> None:
    """compat_check 的存在性清单与适配层契约单源（增删符号必须同步）。"""
    adapter = _runtime_adapter()
    contract = dict(adapter.AstrBotRuntimeAdapter.host_contract())
    for mod, attrs in contract.items():
        assert isinstance(mod, str) and isinstance(attrs, list) and attrs
    assert "astrbot.core.message.message_event_result" in contract
    assert "astrbot.core.star.star_handler" in contract


def test_main_no_direct_private_import() -> None:
    """main 只保留模块级绑定名（供测试替换），不再直接 import 私有层。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "from astrbot.core" not in src
    assert "import astrbot.core" not in src


def test_command_handler_annotations_resolve_at_runtime() -> None:
    """所有宿主注册的处理器注解必须在运行时可解析（0.9.5 线上修复）。

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
    import importlib
    import inspect

    main_mod = importlib.import_module(f"{PACKAGE_NAME}.main")
    plugin_cls = main_mod.SelfInitiatedReplyPlugin

    # 宿主注册的两类处理器：event_message_type 钩子与 /selfreply 指令族。
    handler_names = [
        name for name in dir(plugin_cls) if name == "on_message" or name.startswith("selfreply")
    ]
    assert "on_message" in handler_names
    # 指令族共 10 个（组本身 selfreply + 9 个子指令），少于此说明漏扫了。
    assert len([n for n in handler_names if n.startswith("selfreply")]) == 10

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
