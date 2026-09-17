"""可执行的 AstrBot host fixture：让测试真正导入并实例化 main.py。

安装完整的 astrbot.* stub（覆盖 main.py 导入路径所需的全部模块），并提供
FakeToolSet / FakeEvent / FakeContext / FakeBuildResult。stub 安装是幂等补全式
的：已存在的模块沿用，缺失的模块或属性补齐，因此可以与其他测试文件共存。

每个测试用独立动态包名加载插件，避免 sys.modules 污染。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

MAIN_PACKAGE_NAME = "selfreply_main_test_package"

# 单源守卫的扫描面：用 rglob 并在此单点声明排除目录，避免各守卫各写一套。
# 非递归的 ROOT.glob("*.py") 会把 image/ 子包漏在视野外。
_NON_PRODUCTION_DIRS = frozenset(
    {"tests", "scripts", ".scratch", "data", "dist", "build", "__pycache__", ".venv", ".git"}
)


def production_py_files() -> list[Path]:
    """全部生产 Python 文件（根 + image/ 子包），路径排序稳定。

    守卫类测试应一律用它而非 ``ROOT.glob("*.py")``：后者漏掉子包，
    使镜像/漂移类断言在子包内静默失效。
    """
    return sorted(
        path
        for path in ROOT.rglob("*.py")
        if not _NON_PRODUCTION_DIRS & set(path.relative_to(ROOT).parts)
    )


def _module(name: str) -> types.ModuleType:
    return sys.modules.setdefault(name, types.ModuleType(name))


def install_astrbot_stubs() -> None:
    """Install (or complete) the astrbot stub modules for main.py imports."""
    astrbot = _module("astrbot")
    api = _module("astrbot.api")
    web = _module("astrbot.api.web")
    event = _module("astrbot.api.event")
    event_filter = _module("astrbot.api.event.filter")
    star = _module("astrbot.api.star")
    components = _module("astrbot.api.message_components")
    core = _module("astrbot.core")
    msg_evt_result = _module("astrbot.core.message.message_event_result")
    provider_entities = _module("astrbot.core.provider.entities")
    star_handler = _module("astrbot.core.star.star_handler")
    pipeline_ctx = _module("astrbot.core.pipeline.context")
    astrbot_path = _module("astrbot.core.utils.astrbot_path")
    agent_tool = _module("astrbot.core.agent.tool")
    run_util = _module("astrbot.core.astr_agent_run_util")
    main_agent = _module("astrbot.core.astr_main_agent")

    if not hasattr(api, "logger"):
        api.logger = logging.getLogger("selfreply-main-test")
    if not hasattr(api, "AstrBotConfig"):
        api.AstrBotConfig = dict

    if not hasattr(event, "AstrMessageEvent"):
        event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    if not hasattr(event, "MessageChain"):
        event.MessageChain = _FakeMessageChain
    if not hasattr(event, "filter"):
        event.filter = event_filter

    if not hasattr(event_filter, "EventMessageType"):
        event_filter.EventMessageType = _FakeEnum("ALL")
    if not hasattr(event_filter, "PlatformAdapterType"):
        event_filter.PlatformAdapterType = _FakeEnum("ALL")
    if not hasattr(event_filter, "event_message_type"):
        event_filter.event_message_type = _passthrough_decorator
    if not hasattr(event_filter, "platform_adapter_type"):
        event_filter.platform_adapter_type = _passthrough_decorator
    if not hasattr(event_filter, "command_group"):
        event_filter.command_group = _command_group
    if not hasattr(event_filter, "PermissionType"):
        event_filter.PermissionType = _FakeEnum("ADMIN")
    if not hasattr(event_filter, "permission_type"):
        event_filter.permission_type = _permission_type

    if not hasattr(star, "Star"):

        class Star:
            def __init__(self, context: Any) -> None:
                self.context = context

        star.Star = Star
    if not hasattr(star, "Context"):
        star.Context = type("Context", (), {})
    if not hasattr(star, "register"):
        star.register = _passthrough_decorator

    if not hasattr(components, "At"):
        components.At = type("At", (), {})
    if not hasattr(components, "Image"):
        components.Image = type("Image", (), {})
    if not hasattr(components, "Record"):
        components.Record = type("Record", (), {})
    if not hasattr(components, "File"):
        components.File = type("File", (), {})
    if not hasattr(components, "Video"):
        components.Video = type("Video", (), {})
    if not hasattr(components, "Reply"):

        class _FakeReply:
            """引用组件桩：只承载 ``id``（production 只构造引用，不读其它字段）。"""

            def __init__(self, id: Any = "", **_: Any) -> None:
                self.id = id

            def __str__(self) -> str:
                # 宿主 MessageChain.get_plain_text 只拼接 Plain 组件，桩同样不让
                # 引用进入正文，否则 sent_texts / get_plain_text 断言会被 repr 污染。
                return ""

        components.Reply = _FakeReply

    if not hasattr(msg_evt_result, "MessageEventResult"):
        msg_evt_result.MessageEventResult = _FakeMessageEventResult
    if not hasattr(msg_evt_result, "ResultContentType"):
        msg_evt_result.ResultContentType = _FakeEnum("LLM_RESULT")

    if not hasattr(provider_entities, "ProviderRequest"):

        class ProviderRequest:
            def __init__(self) -> None:
                self.prompt = ""
                self.image_urls: list[str] = []
                self.audio_urls: list[str] = []
                self.func_tool: Any = None
                self.session_id = ""
                self.conversation: Any = None
                self.contexts: Any = None
                self.model: Any = None
                self.system_prompt = ""
                self.extra_user_content_parts: list[Any] = []

        provider_entities.ProviderRequest = ProviderRequest

    if not hasattr(star_handler, "EventType"):
        star_handler.EventType = _FakeEnum(
            "OnLLMRequestEvent", "OnDecoratingResultEvent", "OnAfterMessageSentEvent"
        )

    if not hasattr(pipeline_ctx, "call_event_hook"):
        pipeline_ctx.call_event_hook = _call_event_hook
    if not hasattr(pipeline_ctx, "_hook_calls"):
        pipeline_ctx._hook_calls = []

    if not hasattr(astrbot_path, "get_astrbot_config_path"):
        astrbot_path.get_astrbot_config_path = lambda: str(ROOT / ".tmp_astrbot" / "config")
    if not hasattr(astrbot_path, "get_astrbot_plugin_data_path"):
        astrbot_path.get_astrbot_plugin_data_path = lambda: str(ROOT / ".tmp_astrbot" / "data")

    if not hasattr(agent_tool, "ToolSet"):
        agent_tool.ToolSet = FakeToolSet
    if not hasattr(run_util, "run_agent"):
        run_util.run_agent = _fake_run_agent
    if not hasattr(main_agent, "MainAgentBuildConfig"):
        main_agent.MainAgentBuildConfig = _FakeBuildConfig
    if not hasattr(main_agent, "build_main_agent"):
        main_agent.build_main_agent = _fake_build_main_agent
    if not hasattr(main_agent, "_get_session_conv"):
        main_agent._get_session_conv = _fake_get_session_conv

    if not hasattr(web, "request"):
        web.request = FakeRequest()
    if not hasattr(astrbot, "api"):
        astrbot.api = api
    if not hasattr(core, "message"):
        core.message = sys.modules["astrbot.core.message"] = _module("astrbot.core.message")
    if not hasattr(sys.modules["astrbot.core.message"], "message_event_result"):
        sys.modules["astrbot.core.message"].message_event_result = msg_evt_result


def load_package(package_name: str, module: str) -> types.ModuleType:
    """以独立动态包名加载插件模块（sys.modules 隔离，防测试间污染）。"""
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.{module}")


def load_modules(package_name: str, *names: str) -> tuple[types.ModuleType, ...]:
    """按动态包名批量加载插件模块（各测试文件引导的单源实现）。

    "先装宿主 stub、再按包名建包加载"是所有引导的公共前奏，调用方只给模块名。
    包名仍由调用方持有：同一份源码在不同测试里用不同包名隔离 sys.modules，
    各文件的模块集合也各有不同（不按名字分派，避免伪抽象）。
    """
    install_astrbot_stubs()
    return tuple(load_package(package_name, name) for name in names)


def base_runtime_capabilities(runtime: Any, **overrides: Any) -> Any:
    """完整契约的 ``AgentRuntimeCapabilities``（validate 覆盖全部私有入口）。

    契约字段增删时只改这里，两处用例不会各持一半。
    """
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


def load_main() -> types.ModuleType:
    install_astrbot_stubs()
    return load_package(MAIN_PACKAGE_NAME, "main")


def reset_hook_calls() -> None:
    pipeline_ctx = sys.modules.get("astrbot.core.pipeline.context")
    if pipeline_ctx is not None:
        pipeline_ctx._hook_calls = []


def hook_calls() -> list[tuple[Any, Any]]:
    pipeline_ctx = sys.modules.get("astrbot.core.pipeline.context")
    if pipeline_ctx is None:
        return []
    return list(pipeline_ctx._hook_calls)


def run(coro: Any) -> Any:
    """Run an async helper without requiring pytest-asyncio."""
    return asyncio.run(coro)


async def until(predicate: Any, timeout: float = 2.0) -> None:
    """Wait until ``predicate()`` is truthy, yielding to the loop meanwhile.

    Event-driven replacement for fixed-sleep polling: the loop keeps yielding
    until the condition holds or the timeout expires, so slow CI runners no
    longer produce flaky failures (and fast runners don't waste wall time).
    """

    async def _loop() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(_loop(), timeout=timeout)
    except TimeoutError as exc:
        raise AssertionError(f"等待条件超时（{timeout}s）") from exc


@contextlib.contextmanager
def capture_logs(caplog: Any, logger: Any, level: int = logging.DEBUG) -> Any:
    """在 ``caplog`` 中捕获插件日志，无论拿到的是桩 logger 还是宿主 logger。

    宿主的 ``astrbot`` logger 带 loguru 拦截器且 ``propagate=False``，记录不流向
    caplog 挂在 root 的处理器——``caplog.at_level`` 无论带不带 ``logger=`` 参数都
    只调级别、不改传播，于是 ``caplog.records`` 恒空，日志断言变成假绿灯。本辅助
    在块内临时放行传播并复原。

    ``logger`` 传被测模块的 ``logger`` 对象（如 ``storage.logger``），不要传名字：
    模块按动态包名加载，同一份源码在不同测试里可能绑到不同 logger 实例。
    """
    previous = logger.propagate
    logger.propagate = True
    try:
        with caplog.at_level(level, logger=logger.name):
            caplog.clear()
            yield caplog
    finally:
        logger.propagate = previous


def messages_at_least(caplog: Any, level: int) -> list[str]:
    """caplog 中级别 >= ``level`` 的日志正文（已格式化）。"""
    return [record.getMessage() for record in caplog.records if record.levelno >= level]


def with_plugin(tmp_path: Path, scenario: Any, **config_overrides: Any) -> Any:
    """Instantiate the plugin inside a running loop, run a scenario, terminate.

    ``scenario(plugin, main)`` is an async callable returning the assertion
    result (or raising). The plugin is always terminated so no background task
    leaks between tests.
    """

    async def _inner() -> Any:
        plugin, main = make_plugin(tmp_path, **config_overrides)
        try:
            return await scenario(plugin, main)
        finally:
            await plugin.terminate()

    return asyncio.run(_inner())


class _FakeEnumMember:
    """Enum-like member carrying ``.name`` like a real ``enum.Enum``."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<{self.name}>"


class _FakeEnum:
    def __init__(self, *members: str) -> None:
        for member in members:
            setattr(self, member, _FakeEnumMember(member))
        self._members = members

    def __contains__(self, item: Any) -> bool:
        name = getattr(item, "name", item)
        return name in self._members


def _passthrough_decorator(*_args: Any, **_kwargs: Any) -> Any:
    def decorate(func: Any) -> Any:
        return func

    return decorate


def _permission_type(*_args: Any, **_kwargs: Any) -> Any:
    """Mirror register_permission_type with real host semantics.

    真实宿主（4.26.8/4.27.0）在装饰时会对被装饰对象调用 get_handler_full_name
    （访问 ``__name__``）。RegisteringCommandable 没有 ``__name__``，因此把
    @permission_type 叠在 @command_group 外层会在插件加载时抛 AttributeError。
    桩复刻该行为，让这种顺序错误在测试期就炸出来，而不是留到真机安装时。
    """

    def decorate(obj: Any) -> Any:
        if not hasattr(obj, "__name__"):
            raise AttributeError(
                f"'{type(obj).__name__}' object has no attribute '__name__' "
                "(permission_type must wrap a function, not a command group)"
            )
        return obj

    return decorate


def _command_group(name: str) -> Any:
    """Mirror register_command_group: decorator returning a commandable group."""

    class RegisteringCommandable:
        def __init__(self, group_name: str) -> None:
            self.group_name = group_name

        def command(self, cmd: str, **_: Any) -> Any:
            return _passthrough_decorator()

    def decorate(_obj: Any) -> Any:
        return RegisteringCommandable(name)

    return decorate


class _FakeMessageChain:
    def __init__(self, type: str = "", chain: list[Any] | None = None) -> None:
        self.type = type
        self.chain = chain if chain is not None else []

    def message(self, text: str) -> _FakeMessageChain:
        self.chain.append(text)
        return self

    def get_plain_text(self) -> str:
        return "".join(str(item) for item in self.chain)


class _FakeMessageEventResult:
    def __init__(self) -> None:
        self.text = ""
        self.chain: list[Any] = []

    def message(self, text: str) -> _FakeMessageEventResult:
        self.text = str(text or "")
        self.chain = [self.text]
        return self

    def set_result_content_type(self, _content_type: Any) -> _FakeMessageEventResult:
        return self


class FakeToolSet:
    """Mirror the ToolSet surface main.py depends on (tools/add/remove/get/names)."""

    def __init__(self) -> None:
        self.tools: list[Any] = []

    def add_tool(self, tool: Any) -> None:
        self.tools.append(tool)

    def remove_tool(self, name: str) -> None:
        self.tools = [tool for tool in self.tools if getattr(tool, "name", "") != name]

    def get_tool(self, name: str) -> Any | None:
        for tool in self.tools:
            if getattr(tool, "name", "") == name:
                return tool
        return None

    def names(self) -> list[str]:
        return [str(getattr(tool, "name", "")) for tool in self.tools]


class _FakeBuildConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.values = kwargs


def _fake_get_session_conv(event: Any, context: Any) -> Any:
    return _FakeConversation()


class _FakeConversation:
    def __init__(self) -> None:
        self.history = "[]"


async def _fake_build_main_agent(
    *,
    event: Any,
    plugin_context: Any,
    config: Any,
    provider: Any = None,
    req: Any = None,
    apply_reset: bool = True,
) -> Any | None:
    if req is None:
        return None
    return FakeBuildResult(
        agent_runner=_FakeAgentRunner(),
        provider_request=req,
        provider=provider,
        reset_coro=None if apply_reset else None,
    )


class FakeBuildResult:
    def __init__(
        self,
        *,
        agent_runner: Any = None,
        provider_request: Any = None,
        provider: Any = None,
        reset_coro: Any = None,
    ) -> None:
        self.agent_runner = agent_runner
        self.provider_request = provider_request
        self.provider = provider
        self.reset_coro = reset_coro


class DirectSendingRunner:
    """管线脚手架共用 runner：固定返回 completion_text，其余全 no-op。

    需要不同行为（挂起、request_stop 计数）的用例自建 runner，勿加参数膨胀本类。
    """

    def __init__(self, target_event: Any = None, *, completion_text: str = "你好呀") -> None:
        self._target = target_event
        self._completion_text = completion_text

    def reset(self, **_: Any) -> Any:
        return _FakeResetCoro()

    def request_stop(self) -> None:
        pass

    def get_final_llm_resp(self) -> Any:
        return SimpleNamespace(completion_text=self._completion_text, result_chain=None)

    def close(self) -> None:
        pass


def make_counting_enforce(
    original_enforce: Any,
    runtime: Any,
    snapshots: list[list[str]],
    first_enforce_tools: tuple[str, ...] = ("hook_injected",),
):
    """包装 enforce_final_tool_policy：快照每次清理结果，第一次后注入工具。

    hook 在两次 enforce 之间注入正是被测语义，不可省。
    """

    def counting_enforce(req: Any, inherit_tools: Any) -> bool:
        ok = original_enforce(req, inherit_tools)
        snapshots.append(sorted(runtime.final_tool_ids(req) or []))
        if len(snapshots) == 1:
            for name in first_enforce_tools:
                req.func_tool.add_tool(SimpleNamespace(name=name))
        return ok

    return counting_enforce


class _FakeAgentRunner:
    def reset(self, **_: Any) -> Any:
        return _FakeResetCoro()

    def get_final_llm_resp(self) -> Any:
        return _FakeLLMResponse()


class _FakeResetCoro:
    def __await__(self) -> Any:
        return _noop_async().__await__()

    def close(self) -> None:
        """匹配生产 reset_coro 契约（generation.py 在 try/finally 中同步 close）。"""
        pass


async def _noop_async() -> None:
    return None


class _FakeLLMResponse:
    completion_text = ""


async def _fake_run_agent(
    agent_runner: Any,
    *,
    max_step: int,
    show_tool_use: bool = False,
    show_tool_call_result: bool = False,
    stream_to_general: bool = False,
    show_reasoning: bool = False,
    buffer_intermediate_messages: bool = True,
    **_kwargs: Any,
) -> Any:
    if False:
        yield  # pragma: no cover - marker for async generator compatibility
    return


async def _call_event_hook(event: Any, event_type: Any, *args: Any, **kwargs: Any) -> bool:
    pipeline_ctx = sys.modules["astrbot.core.pipeline.context"]
    pipeline_ctx._hook_calls.append((event, event_type))
    return False


class FakeRequest:
    """Web request stub whose JSON payload tests can swap per call."""

    def __init__(self) -> None:
        self.payload: Any = {}

    async def json(self, default: Any = None) -> Any:
        return self.payload

    async def get_json(self, **_: Any) -> Any:
        return self.payload


class FakePlatformMeta:
    def __init__(self, support_proactive_message: bool = True) -> None:
        self.support_proactive_message = support_proactive_message


class FakeEvent:
    """Minimal AstrMessageEvent behavior for proactive flows."""

    def __init__(
        self,
        *,
        umo: str = "fake:group:123",
        message_str: str = "hello",
        sender_id: str = "u1",
        self_id: str = "bot1",
        platform: str = "fake",
        is_admin: bool = False,
    ) -> None:
        self.unified_msg_origin = umo
        self.message_str = message_str
        self.plugins_name: list[str] | None = []
        self.platform_meta = FakePlatformMeta()
        self.session_id = f"{umo}!fake"
        self._sender_id = sender_id
        self._self_id = self_id
        self._platform = platform
        self._is_admin = is_admin
        self._result: Any = None
        self._extra: dict[str, Any] = {}
        self._stopped = False
        self.sent_texts: list[str] = []
        self.trace = _FakeTrace()

    # sender / platform identity
    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_id

    def get_self_id(self) -> str:
        return self._self_id

    def is_admin(self) -> bool:
        return self._is_admin

    def get_platform_name(self) -> str:
        return self._platform

    def get_platform_id(self) -> str:
        return f"{self._platform}-{self._sender_id}"

    # message lifecycle
    def is_stopped(self) -> bool:
        return self._stopped

    def stop_event(self) -> None:
        self._stopped = True

    def set_result(self, result: Any) -> None:
        self._result = result

    def get_result(self) -> Any:
        return self._result

    def clear_result(self) -> None:
        self._result = None

    def plain_result(self, text: str) -> Any:
        return _FakeMessageEventResult().message(text)

    def set_extra(self, key: str, value: Any) -> None:
        self._extra[key] = value

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)

    async def send(self, message: Any) -> Any:
        getter = getattr(message, "get_plain_text", None)
        if callable(getter):
            self.sent_texts.append(str(getter()))
        else:
            self.sent_texts.append(str(getattr(message, "text", message) or ""))
        return None


class _FakeTrace:
    def record(self, *_: Any, **__: Any) -> None:
        pass


class FakeContext:
    """Minimal Context stub for plugin construction and web API registration."""

    def __init__(self) -> None:
        self.register_web_api_calls: list[tuple[str, Any, list[str], str]] = []
        self.astrbot_config: dict[str, Any] = {}
        self.sent: list[tuple[str, Any]] = []

    def register_web_api(self, route: str, handler: Any, methods: list[str], desc: str) -> None:
        self.register_web_api_calls.append((route, handler, methods, desc))

    async def send_message(self, umo: str, message: Any) -> Any:
        self.sent.append((umo, message))
        # 真实宿主 Context.send_message 找到平台返回 True、未找到返回 False；
        # 桩默认模拟"找到平台"的正常路径，避免把 context 兜底路径的
        # UNKNOWN/FAILED_BEFORE_SUBMIT 语义掩盖掉。
        return True


def make_plugin(tmp_path: Path, **config_overrides: Any) -> tuple[Any, types.ModuleType]:
    """Instantiate the plugin against tmp paths with sane defaults."""
    main = load_main()
    context = FakeContext()

    # Route all host path lookups into the tmp tree for this test. main.py
    # binds these functions at import time, so patch the module namespace.
    main.get_astrbot_config_path = lambda: str(tmp_path / "config")
    main.get_astrbot_plugin_data_path = lambda: str(tmp_path / "data")

    defaults = {
        "enabled": True,
        "decision_model_enabled": False,
        "enabled_message_trigger": True,
        "enabled_patrol_trigger": False,
        "whitelist_sessions": ["fake:group:123"],
        "message_delay_sec": 5,
        "min_silence_sec": 0,
        "cooldown_sec": 0,
        "generation_timeout_sec": 30,
        "decision_timeout_sec": 5,
    }
    defaults.update(config_overrides)
    config = defaults

    plugin = main.SelfInitiatedReplyPlugin(context, config)
    return plugin, main


class PipelineTestAdapter:
    """Wrap the real runtime adapter; only build/run are injectable.

    ``enforce``/``final_tool_ids``/``new_tool_set`` stay real so the integration
    test exercises the actual tool-boundary logic.
    """

    def __init__(self, base: Any, *, build_effect: Any = None, run_effect: Any = None) -> None:
        self._base = base
        self._build_effect = build_effect
        self._run_effect = run_effect

    async def build(self, **kwargs: Any) -> Any:
        result = await self._base.build(**kwargs)
        if self._build_effect is not None:
            result = await self._build_effect(kwargs, result)
        return result

    def run(self, agent_runner: Any, **kwargs: Any) -> Any:
        if self._run_effect is not None:
            return self._run_effect(agent_runner, **kwargs)
        return self._base.run(agent_runner, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def install_tool_injecting_pipeline(
    plugin: Any,
    main: types.ModuleType,
    *,
    event: Any,
    build_tools: tuple[str, ...] = ("send_message_to_user", "web_search", "mcp_anything"),
    run_effect: Any = None,
    first_enforce_tools: tuple[str, ...] = ("hook_injected",),
    snapshot_reset: bool = False,
    snapshot_prompts: bool = False,
) -> dict[str, Any]:
    """装配「build 注入工具 → 两次 enforce（第一次后注入）→ run」测试管线。

    run_effect 签名 ``(runner, **kwargs)``；不传则 run 空转。快照开关控制 reset
    时工具集快照与 build prompt 快照（不用快照的用例不背这份观测开销）。返回
    控制器（快照列表 + restore），调用方须在 finally 里调 restore()。
    """
    req_holder: dict[str, Any] = {}
    enforce_snapshots: list[list[str]] = []
    reset_snapshots: list[list[str]] = []
    prompts: list[str] = []

    async def build_effect(kwargs: dict[str, Any], result: Any) -> Any:
        req_holder["req"] = kwargs["req"]
        if snapshot_prompts:
            prompts.append(str(getattr(kwargs["req"], "prompt", "") or ""))
        tool_set = kwargs["req"].func_tool
        for name in build_tools:
            tool_set.add_tool(SimpleNamespace(name=name))

        async def _reset() -> None:
            if snapshot_reset:
                reset_snapshots.append(
                    sorted(main._AGENT_RUNTIME.final_tool_ids(req_holder["req"]) or [])
                )

        return FakeBuildResult(
            agent_runner=DirectSendingRunner(event),
            provider_request=kwargs["req"],
            provider=None,
            reset_coro=_reset(),
        )

    def default_run_effect(_runner: Any, **_kwargs: Any) -> Any:
        async def gen() -> Any:
            yield None

        return gen()

    original_runtime = main._AGENT_RUNTIME
    main._AGENT_RUNTIME = PipelineTestAdapter(
        original_runtime,
        build_effect=build_effect,
        run_effect=run_effect or default_run_effect,
    )
    original_enforce = plugin._generation.enforce_final_tool_policy
    plugin._generation.enforce_final_tool_policy = make_counting_enforce(
        original_enforce, main._AGENT_RUNTIME, enforce_snapshots, first_enforce_tools
    )
    return {
        "req_holder": req_holder,
        "enforce_snapshots": enforce_snapshots,
        "reset_snapshots": reset_snapshots,
        "prompts": prompts,
        "restore": lambda: (
            setattr(plugin._generation, "enforce_final_tool_policy", original_enforce),
            setattr(main, "_AGENT_RUNTIME", original_runtime),
        ),
    }
