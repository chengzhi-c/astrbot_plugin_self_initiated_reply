"""宿主私有层（``astrbot.core.*``）的隔离墙。

拥有：``_HOST_CONTRACT`` 单源符号表、加载期一次性探测与契约校验、以及一组窄方法：
事件结果、provider 请求与事件类型经这里出去；钩子与路径函数由 main.py 在 import 期
从 capabilities 直接绑成模块级名字（理由见 ``AstrBotRuntimeAdapter`` 内注释）。

隔离的价值在于宿主升级时的失败位置：符号缺失在加载期即报（core 组缺失拒绝
加载，probe 组缺失降级为 None），而不是在某次发送的半路。增删宿主符号只改
``_HOST_CONTRACT``，``compat_check`` 与 ``host_contract()`` 都从它取清单。

不含业务逻辑，也不管公开层，``astrbot.api.*`` 由各模块直接 import。
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import AsyncIterator, Callable
from copy import copy
from dataclasses import dataclass
from typing import Any, NamedTuple

from astrbot.api import logger

from .models import PLUGIN_ID, proactive_build_config_kwargs
from .utils import redact_exc_text


def snapshot_reply_event(event: Any) -> Any:
    """隔离主动回复的消息身份与运行槽，保留平台客户端等发送资源。"""
    snapshot = copy(event)
    snapshot.message_obj = copy(event.message_obj)
    snapshot.message_obj.sender = copy(event.message_obj.sender)
    snapshot.message_obj.message = list(event.message_obj.message)
    snapshot.session = copy(event.session)
    snapshot.plugins_name = copy(event.plugins_name)
    snapshot._extras = event.get_extra().copy()
    snapshot._temporary_local_files = []
    snapshot.clear_result()
    return snapshot


def _require[T](value: T | None, name: str) -> T:
    """探测值兜底解包：缺失即 raise，兼作 mypy 的 Optional 收窄。

    不用 assert：`python -O` 下 assert 被剥除，None 会漏进宿主调用并在更深处
    以难诊断的形态崩溃。``validate()`` 只在加载期跑一次，本函数是运行期
    唯一的 None 兜底。
    """
    if value is None:
        raise RuntimeError(f"当前 AstrBot 缺少主动回复所需的 {name}")
    return value


@dataclass(frozen=True)
class AgentRuntimeCapabilities:
    """The private AstrBot capabilities required by the proactive pipeline."""

    import_error: Exception | None
    tool_set: type[Any] | None
    build_config: type[Any] | None
    build_main_agent: Callable[..., Any] | None
    get_session_conv: Callable[..., Any] | None
    run_agent: Callable[..., Any] | None
    # 事件结果与钩子链（delivery/generation 经适配层窄方法访问）
    event_result_cls: type[Any] | None = None
    result_content_type: Any | None = None
    event_type: Any | None = None
    call_event_hook: Callable[..., Any] | None = None
    provider_request_cls: type[Any] | None = None
    # 宿主配置与插件数据目录。
    config_path_fn: Callable[[], Any] | None = None
    plugin_data_path_fn: Callable[[], Any] | None = None
    function_tool: type[Any] | None = None
    handoff_tool: type[Any] | None = None


# 事件钩子实际使用的 EventType 成员：漂移（改名/移除）即契约红
EVENT_TYPE_MEMBERS = (
    "OnLLMRequestEvent",
    "OnDecoratingResultEvent",
    "OnAfterMessageSentEvent",
)

# 事件结果契约：实例必须可用且具备这两个链式方法（缺失参数即红）
_EVENT_RESULT_METHODS = ("message", "set_result_content_type")

# ProviderRequest 实例在 generation 中实际赋值的字段：缺失即红。
_PROVIDER_REQUEST_FIELDS = frozenset(
    {
        "prompt",
        "image_urls",
        "audio_urls",
        "func_tool",
        "session_id",
        "conversation",
        "contexts",
    }
)

# 4.29 将回退名单、重试次数提升为独立字段；旧版回退能力从 provider_settings 读取。
_OPTIONAL_BUILD_CONFIG_KWARGS = frozenset({"fallback_provider_ids", "request_max_retries"})


class _HostEntry(NamedTuple):
    """一个宿主私有模块的符号契约条目（from_host 探测与 compat 清单单源）。"""

    module: str
    symbols: tuple[str, ...]
    core: bool  # 主 Agent API 必需项：缺失即整包 import_error
    fallback_module: str | None = None


# 宿主私有符号单一来源表：增删符号只需改此处。
# core 组是主 Agent API 必需项（任一缺失 → 整包不可用）；probe 组
# 单符号缺失 → 对应能力为 None（旧版宿主降级路径）。
_HOST_CONTRACT: tuple[_HostEntry, ...] = (
    _HostEntry("astrbot.core.agent.tool", ("ToolSet",), core=True),
    _HostEntry("astrbot.core.agent.tool", ("FunctionTool",), core=False),
    _HostEntry("astrbot.core.agent.handoff", ("HandoffTool",), core=False),
    _HostEntry(
        "astrbot.core.astr_agent_run_util",
        ("run_agent",),
        core=True,
        fallback_module="astrbot.core.astr_agent_runtime.runner",
    ),
    _HostEntry(
        "astrbot.core.astr_main_agent",
        ("MainAgentBuildConfig",),
        core=True,
        fallback_module="astrbot.core.astr_agent_runtime.main_agent.config",
    ),
    _HostEntry(
        "astrbot.core.astr_main_agent",
        ("_get_session_conv",),
        core=True,
        fallback_module="astrbot.core.astr_agent_runtime.main_agent.request",
    ),
    _HostEntry(
        "astrbot.core.astr_main_agent",
        ("build_main_agent",),
        core=True,
        fallback_module="astrbot.core.astr_agent_runtime.main_agent.builder",
    ),
    _HostEntry(
        "astrbot.core.message.message_event_result",
        ("MessageEventResult", "ResultContentType"),
        core=False,
    ),
    _HostEntry("astrbot.core.pipeline.context", ("call_event_hook",), core=False),
    _HostEntry("astrbot.core.provider.entities", ("ProviderRequest",), core=False),
    _HostEntry("astrbot.core.star.star_handler", ("EventType",), core=False),
    _HostEntry(
        "astrbot.core.utils.astrbot_path",
        ("get_astrbot_config_path", "get_astrbot_plugin_data_path"),
        core=False,
    ),
)


def _import_symbols(entry: _HostEntry) -> dict[str, Any]:
    last_error: ImportError | AttributeError | None = None
    for module_name in (entry.module, entry.fallback_module):
        if module_name is None:
            continue
        try:
            module = importlib.import_module(module_name)
            return {name: getattr(module, name) for name in entry.symbols}
        except (ImportError, AttributeError) as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ImportError(f"missing host module for {entry.symbols}")


def _resolved_entry_module(entry: _HostEntry) -> str:
    """Return the first module exporting all symbols in an entry."""
    for module_name in (entry.module, entry.fallback_module):
        if module_name is None:
            continue
        try:
            module = importlib.import_module(module_name)
            if all(hasattr(module, name) for name in entry.symbols):
                return module_name
        except (ImportError, AttributeError):
            continue
    return entry.module


class AstrBotRuntimeAdapter:
    """Keep private AstrBot Agent imports and compatibility checks in one place.

    宿主私有符号（astrbot.core.*）全量收敛于此，探测、调用与
    契约断言都只经本类发生；delivery/generation/main 不得再直接 import。
    """

    _BUILD_REQUIRED = frozenset({"event", "plugin_context", "config", "req"})
    _OPTIONAL_BUILD_KWARGS = frozenset({"apply_reset"})
    _RUN_REQUIRED = frozenset(
        {
            "agent_runner",
            "max_step",
            "show_tool_use",
            "show_tool_call_result",
            "stream_to_general",
            "show_reasoning",
            "buffer_intermediate_messages",
        }
    )

    def __init__(self, capabilities: AgentRuntimeCapabilities) -> None:
        self.capabilities = capabilities

    @classmethod
    def host_contract(cls) -> list[tuple[str, list[str]]]:
        """compat_check 存在性检查的单一来源清单（增删符号只需改 _HOST_CONTRACT）。"""
        merged: dict[str, list[str]] = {}
        for entry in _HOST_CONTRACT:
            module = _resolved_entry_module(entry)
            merged.setdefault(module, []).extend(entry.symbols)
        return [(module, symbols) for module, symbols in merged.items()]

    @classmethod
    def from_host(cls) -> AstrBotRuntimeAdapter:
        try:
            core_symbols: dict[str, Any] = {}
            for entry in _HOST_CONTRACT:
                if entry.core:
                    core_symbols.update(_import_symbols(entry))
        except (ImportError, AttributeError) as exc:  # pragma: no cover - host dependent
            return cls(
                AgentRuntimeCapabilities(
                    import_error=exc,
                    tool_set=None,
                    build_config=None,
                    build_main_agent=None,
                    get_session_conv=None,
                    run_agent=None,
                )
            )

        probed: dict[str, Any] = {}
        for entry in _HOST_CONTRACT:
            if not entry.core:
                try:
                    probed.update(_import_symbols(entry))
                except (ImportError, AttributeError):  # pragma: no cover - host dependent
                    continue
        return cls(
            AgentRuntimeCapabilities(
                import_error=None,
                tool_set=core_symbols["ToolSet"],
                build_config=core_symbols["MainAgentBuildConfig"],
                build_main_agent=core_symbols["build_main_agent"],
                get_session_conv=core_symbols["_get_session_conv"],
                run_agent=core_symbols["run_agent"],
                event_result_cls=probed.get("MessageEventResult"),
                result_content_type=probed.get("ResultContentType"),
                event_type=probed.get("EventType"),
                call_event_hook=probed.get("call_event_hook"),
                provider_request_cls=probed.get("ProviderRequest"),
                config_path_fn=probed.get("get_astrbot_config_path"),
                plugin_data_path_fn=probed.get("get_astrbot_plugin_data_path"),
                function_tool=probed.get("FunctionTool"),
                handoff_tool=probed.get("HandoffTool"),
            )
        )

    def validate(self, *, soft: bool = False) -> list[str]:
        """契约断言：缺失参数即红。硬模式首错 raise；软模式收集告警不阻塞。

        调用时机是加载期一次（``SelfInitiatedReplyPlugin.__init__`` 首条语句，
        无条件、不被 try 包裹），宿主不兼容即拒绝加载。各入口不再逐次调用：
        ``capabilities`` 是 frozen dataclass，加载期通过后契约不会在运行期变化；
        运行期的 None 兜底由 ``_require`` 承担。不缓存探测结论：调用点只有
        加载期一次 + compat_check + 测试显式调用，缓存换不到收益。
        """
        problems = self._probe_problems()
        if problems and not soft:
            raise RuntimeError(problems[0])
        return problems

    def _probe_problems(self) -> list[str]:
        """执行一次完整契约探测，返回问题列表（无副作用，不 raise 契约错）。

        覆盖全部私有入口：构建 / 运行 / 会话装载 / 事件结果 / 事件类型 /
        钩子链 / ProviderRequest / 路径函数。
        """
        problems: list[str] = []
        caps = self.capabilities
        if caps.import_error is not None:
            problems.append(
                "当前 AstrBot 缺少主动回复所需的主 Agent API；"
                "请使用已验证的 AstrBot 版本，或先完成运行时适配。"
            )
        elif caps.tool_set is None:
            problems.append("当前 AstrBot 缺少主 Agent ToolSet，无法建立主动回复工具边界")
        else:
            self._validate_callable(
                problems, "build_main_agent", caps.build_main_agent, self._BUILD_REQUIRED
            )
            self._validate_callable(problems, "run_agent", caps.run_agent, self._RUN_REQUIRED)
            self._validate_callable(
                problems, "_get_session_conv", caps.get_session_conv, frozenset()
            )
            if caps.build_config is None:
                problems.append("当前 AstrBot 缺少 MainAgentBuildConfig")
            else:
                # 用运行期相同的构造入口核验配置键，保留旧宿主的可选参数筛选。
                try:
                    self.new_build_config(**proactive_build_config_kwargs())
                except TypeError as exc:
                    problems.append(
                        "当前 AstrBot 的 MainAgentBuildConfig 不接受主动运行的配置参数："
                        f"{redact_exc_text(exc)}"
                    )
                except Exception as exc:
                    problems.append(
                        f"当前 AstrBot 的 MainAgentBuildConfig 不可实例化：{redact_exc_text(exc)}"
                    )
        self._validate_callable(problems, "call_event_hook", caps.call_event_hook, frozenset())
        if caps.event_result_cls is None:
            problems.append("当前 AstrBot 缺少事件结果类（MessageEventResult）")
        else:
            try:
                instance = caps.event_result_cls()
            except Exception as exc:
                problems.append(
                    f"当前 AstrBot 的 MessageEventResult 不可实例化：{redact_exc_text(exc)}"
                )
            else:
                missing_methods = sorted(
                    name
                    for name in _EVENT_RESULT_METHODS
                    if not callable(getattr(instance, name, None))
                )
                if missing_methods:
                    problems.append(
                        "当前 AstrBot 的 MessageEventResult 缺少方法：" + ", ".join(missing_methods)
                    )
        if caps.result_content_type is None or not hasattr(caps.result_content_type, "LLM_RESULT"):
            problems.append("当前 AstrBot 缺少 ResultContentType.LLM_RESULT")
        if caps.event_type is None:
            problems.append("当前 AstrBot 缺少 EventType")
        else:
            missing_members = [
                name for name in EVENT_TYPE_MEMBERS if not hasattr(caps.event_type, name)
            ]
            if missing_members:
                problems.append("当前 AstrBot 的 EventType 缺少成员：" + ", ".join(missing_members))
        if caps.provider_request_cls is None:
            problems.append("当前 AstrBot 缺少 ProviderRequest")
        else:
            try:
                instance = caps.provider_request_cls()
            except Exception as exc:
                problems.append(
                    f"当前 AstrBot 的 ProviderRequest 不可实例化：{redact_exc_text(exc)}"
                )
            else:
                missing_fields = sorted(_PROVIDER_REQUEST_FIELDS - set(dir(instance)))
                if missing_fields:
                    problems.append(
                        "当前 AstrBot 的 ProviderRequest 缺少字段：" + ", ".join(missing_fields)
                    )
        for name, fn in (
            ("get_astrbot_config_path", caps.config_path_fn),
            ("get_astrbot_plugin_data_path", caps.plugin_data_path_fn),
        ):
            if not callable(fn):
                problems.append(f"当前 AstrBot 的 {name} 不可调用")
        return problems

    @staticmethod
    def _validate_callable(
        problems: list[str],
        name: str,
        func: Callable[..., Any] | None,
        required_params: frozenset[str],
    ) -> None:
        if not callable(func):
            problems.append(f"当前 AstrBot 主 Agent API 不可用：{name}")
            return
        if not required_params:
            return
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return
        if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
            return
        missing = sorted(required_params - set(params))
        if missing:
            problems.append(f"当前 AstrBot 的 {name} 签名不兼容，缺少参数：{', '.join(missing)}")

    @property
    def tool_set(self) -> type[Any]:
        return _require(self.capabilities.tool_set, "主 Agent ToolSet")

    @property
    def build_main_agent(self) -> Callable[..., Any]:
        return _require(self.capabilities.build_main_agent, "build_main_agent")

    @property
    def get_session_conv(self) -> Callable[..., Any]:
        return _require(self.capabilities.get_session_conv, "_get_session_conv")

    @property
    def run_agent(self) -> Callable[..., Any]:
        return _require(self.capabilities.run_agent, "run_agent")

    def new_tool_set(self) -> Any:
        return self.tool_set()

    @property
    def event_type(self) -> Any:
        """宿主 EventType 枚举（成员访问经此唯一出口）。"""
        return _require(self.capabilities.event_type, "EventType")

    @property
    def result_llm_type(self) -> Any:
        """宿主 ResultContentType.LLM_RESULT 值。"""
        return _require(self.capabilities.result_content_type, "ResultContentType").LLM_RESULT

    def new_event_result(self) -> Any:
        """宿主 MessageEventResult 实例（构造经此唯一出口）。"""
        return _require(self.capabilities.event_result_cls, "MessageEventResult")()

    def new_provider_request(self) -> Any:
        """宿主 ProviderRequest 实例。"""
        return _require(self.capabilities.provider_request_cls, "ProviderRequest")()

    # 事件钩子与路径函数（config_path_fn / plugin_data_path_fn）不经本类方法出口：
    # main.py 在 import 期把 capabilities 里的这几项绑成模块级名字，测试按名字替换。
    # 路径解析失败由 resolve_paths 让异常传播、加载期即崩。

    def filter_final_tools(
        self,
        req: Any,
        *,
        keep: frozenset[str] | None = None,
        drop: frozenset[str] = frozenset(),
    ) -> bool:
        """Apply the allowlist, or ``drop`` when inheriting tools, to the request's ToolSet.

        Return ``False`` if the tools cannot be read, removed or verified; the
        caller must then abort the proactive run.
        """
        try:
            tool_set = req.func_tool
            if tool_set is None:
                return True
            handoff_tool = self.capabilities.handoff_tool
            for tool in list(tool_set.tools):
                name = tool.name
                if not name:
                    continue
                if keep is not None:
                    if name not in keep:
                        tool_set.remove_tool(name)
                elif name in drop or (handoff_tool is not None and isinstance(tool, handoff_tool)):
                    tool_set.remove_tool(name)
            remaining_tools = list(tool_set.tools)
            remaining = [tool.name for tool in remaining_tools]
        except Exception as exc:
            logger.warning(
                "[%s] tool boundary fail-closed: tool set read or removal failed (%s); "
                "aborting proactive run",
                PLUGIN_ID,
                redact_exc_text(exc),
            )
            return False
        if keep is not None:
            violations = sorted({name or "<unnamed>" for name in remaining if name not in keep})
            policy = "non-allowlisted"
        else:
            violations = sorted(
                {
                    tool.name or "<unnamed>"
                    for tool in remaining_tools
                    if not tool.name
                    or tool.name in drop
                    or (handoff_tool is not None and isinstance(tool, handoff_tool))
                }
            )
            policy = "denied"
        if violations:
            logger.warning(
                "[%s] tool boundary fail-closed: %s tools remain: %s",
                PLUGIN_ID,
                policy,
                ", ".join(violations),
            )
            return False
        return True

    def guard_final_tools(
        self,
        req: Any,
        *,
        block: frozenset[str],
        agent_runner: Any = None,
    ) -> bool:
        """Keep selected schemas while replacing their tools with plain handlers."""
        try:
            tool_sets = []
            if req.func_tool is not None:
                tool_sets.append(req.func_tool)
            raw_tool_set = getattr(agent_runner, "_skill_like_raw_tool_set", None)
            if raw_tool_set is not None and raw_tool_set is not req.func_tool:
                tool_sets.append(raw_tool_set)
            if not tool_sets:
                return True
            function_tool = _require(self.capabilities.function_tool, "主 Agent FunctionTool")
            handoff_tool = self.capabilities.handoff_tool

            for tool_set in tool_sets:
                for index, tool in enumerate(tool_set.tools):
                    name = tool.name
                    if not name:
                        raise ValueError("tool without a name cannot be verified")
                    is_handoff = handoff_tool is not None and isinstance(tool, handoff_tool)
                    if name not in block and not is_handoff:
                        continue

                    refusal = f"error: tool '{name}' is disabled during proactive replies"

                    async def blocked_handler(
                        _event: Any, _refusal: str = refusal, **_kwargs: Any
                    ) -> str:
                        return _refusal

                    guarded = function_tool(
                        name=name,
                        description=tool.description,
                        parameters=tool.parameters,
                        handler=blocked_handler,
                        active=bool(getattr(tool, "active", True)),
                    )
                    tool_set.tools[index] = guarded
        except Exception as exc:
            logger.warning(
                "[%s] tool schema guard failed (%s); aborting proactive run",
                PLUGIN_ID,
                redact_exc_text(exc),
            )
            return False
        return True

    @staticmethod
    def _accepts_build_config_kwarg(config_type: Any, name: str) -> bool:
        """宿主 build config 是否接受该关键字。

        判据看的是**宿主自己声明的形参**而不是版本号：4.27.2 有没有这个字段
        无需预设，有则传、无则省，两种情况下都不会 TypeError。"""
        try:
            params = inspect.signature(config_type).parameters
        except (TypeError, ValueError):
            # 签名不可检查时按接受处理：省略一个存在的键会静默丢掉回退。
            return True
        return name in params or any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
        )

    def new_build_config(self, **kwargs: Any) -> Any:
        config_type = self.capabilities.build_config
        if config_type is None:
            raise RuntimeError("当前 AstrBot 缺少 MainAgentBuildConfig")
        accepted: dict[str, Any] = {}
        dropped: list[str] = []
        for key, value in kwargs.items():
            if key in _OPTIONAL_BUILD_CONFIG_KWARGS and not self._accepts_build_config_kwarg(
                config_type, key
            ):
                dropped.append(key)
                continue
            accepted[key] = value
        if dropped:
            # 备用模型有 provider_settings.fallback_chat_models 回退通道；重试次数
            # 只有支持从 provider_settings 读取它的宿主才生效，最低支持版 4.23.3
            # 不读取。保留 DEBUG 记录，便于定位旧宿主上设置未生效的原因。
            logger.debug(
                "[%s] build config 无 %s 字段；旧版 provider_settings 回退能力视宿主版本而定",
                PLUGIN_ID,
                "/".join(dropped),
            )
        return config_type(**accepted)

    async def load_session_conversation(self, event: Any, context: Any) -> Any:
        return await self.get_session_conv(event, context)

    def legacy_conversation(self, conversation: Any, context: Any) -> Any:
        """把 4.30 的会话读结果转成宿主构建 Agent 所需的对象。

        4.30 的 ``_get_session_conv`` 返回 ``ConversationRead``：会话 id 在
        ``conversation_id``，没有 ``cid``/``history``。显式请求（插件自带
        ``req``）的路径上宿主不再自己做这层转换，而 build 的 event writer 读
        ``req.conversation.cid``，persona 读 ``.persona_id``。
        转换器由宿主提供（``to_legacy_conversation``），老宿主返回的对象本身
        已带 ``cid``，原样放行。
        """
        if conversation is None or hasattr(conversation, "cid"):
            return conversation
        manager = getattr(context, "conversation_manager", None)
        convert = getattr(manager, "to_legacy_conversation", None)
        if not callable(convert):
            return conversation
        try:
            return convert(conversation)
        except Exception as exc:
            logger.debug(
                "[%s] legacy conversation conversion failed error=%s",
                PLUGIN_ID,
                redact_exc_text(exc),
            )
            return conversation

    async def build(self, **kwargs: Any) -> Any:
        build = self.build_main_agent
        try:
            params = dict(inspect.signature(build).parameters)
        except (TypeError, ValueError):
            params = {}
        accepts_var_kwargs = any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
        )
        if not accepts_var_kwargs and params:
            if "apply_reset" not in params:
                # 4.30 assembles messages inside build, so request hooks must run there.
                kwargs["config"].call_request_hook = True
            kwargs = {
                key: value
                for key, value in kwargs.items()
                if key not in self._OPTIONAL_BUILD_KWARGS or key in params
            }
        return await build(**kwargs)

    def run(self, agent_runner: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return self.run_agent(agent_runner, **kwargs)
