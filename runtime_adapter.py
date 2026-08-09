from __future__ import annotations

import importlib
import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, NamedTuple, TypeVar

from astrbot.api import logger

from .models import PLUGIN_ID
from .utils import maybe_await

_T = TypeVar("_T")


def _require(value: _T | None, name: str) -> _T:
    """探测值兜底解包：缺失即 raise，兼作 mypy 的 Optional 收窄。

    不用 assert：`python -O` 下 assert 语句被整体剥除，None 会漏进宿主
    调用并在更深处以难诊断的形态崩溃。validate() 已在硬模式首错 raise，
    本函数防御的是 -O 与「探测表新增符号但未进 _probe_problems」的漂移。
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
    # 路径函数：旧版 AstrBot 允许缺失（None = 走回退路径）
    config_path_fn: Callable[[], Any] | None = None
    plugin_data_path_fn: Callable[[], Any] | None = None


# 事件钩子实际使用的 EventType 成员：漂移（改名/移除）即契约红
EVENT_TYPE_MEMBERS = (
    "OnLLMRequestEvent",
    "OnDecoratingResultEvent",
    "OnAfterMessageSentEvent",
)

# 事件结果契约：实例必须可用且具备这两个链式方法（缺失参数即红）
_EVENT_RESULT_METHODS = ("message", "set_result_content_type")

# ProviderRequest 实例在 generation 中实际赋值的字段：缺失即红
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


class _HostEntry(NamedTuple):
    """一个宿主私有模块的符号契约条目（from_host 探测与 compat 清单单源）。"""

    module: str
    symbols: tuple[str, ...]
    core: bool  # 主 Agent API 必需项：缺失即整包 import_error


# 宿主私有符号单一来源表（复审 S4）：增删符号只需改此处。
# core 组是主 Agent API 必需项（任一缺失 → 整包不可用）；probe 组
# 单符号缺失 → 对应能力为 None（旧版宿主降级路径）。
_HOST_CONTRACT: tuple[_HostEntry, ...] = (
    _HostEntry("astrbot.core.agent.tool", ("ToolSet",), core=True),
    _HostEntry("astrbot.core.astr_agent_run_util", ("run_agent",), core=True),
    _HostEntry(
        "astrbot.core.astr_main_agent",
        ("MainAgentBuildConfig", "_get_session_conv", "build_main_agent"),
        core=True,
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
    module = importlib.import_module(entry.module)
    return {name: getattr(module, name) for name in entry.symbols}


class AstrBotRuntimeAdapter:
    """Keep private AstrBot Agent imports and compatibility checks in one place.

    Ticket 13: 宿主私有符号（astrbot.core.*）全量收敛于此——探测、调用与
    契约断言都只经本类发生；delivery/generation/main 不得再直接 import。
    """

    _BUILD_REQUIRED = frozenset({"event", "plugin_context", "config", "req", "apply_reset"})
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

    def __init__(self, capabilities: AgentRuntimeCapabilities):
        self.capabilities = capabilities
        # 契约结论缓存：capabilities 是 frozen dataclass，探测结果在实例生命周期内
        # 不会变，而 10 个 property 每次访问都会调 validate()（inspect.signature +
        # 2 次宿主类实例化）。缓存只存 problems 列表，raise 逻辑留在缓存之外，
        # 因此 hard 模式每次调用仍会抛出。
        self._validated_problems: list[str] | None = None

    @classmethod
    def host_contract(cls) -> list[tuple[str, list[str]]]:
        """compat_check 存在性检查的单一来源清单（增删符号只需改 _HOST_CONTRACT）。"""
        return [(entry.module, list(entry.symbols)) for entry in _HOST_CONTRACT]

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
            )
        )

    def validate(self, *, soft: bool = False) -> list[str]:
        """契约断言：缺失参数即红。硬模式首错 raise；软模式收集告警不阻塞。

        探测结论按实例缓存（capabilities 不可变），但 raise 不进缓存——
        硬模式重复调用仍会抛出，语义与未缓存时完全一致。
        """
        if self._validated_problems is None:
            self._validated_problems = self._probe_problems()
        problems = list(self._validated_problems)
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
        self._validate_callable(problems, "call_event_hook", caps.call_event_hook, frozenset())
        if caps.event_result_cls is None:
            problems.append("当前 AstrBot 缺少事件结果类（MessageEventResult）")
        else:
            try:
                instance = caps.event_result_cls()
            except Exception as exc:
                problems.append(f"当前 AstrBot 的 MessageEventResult 不可实例化：{exc}")
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
                problems.append(f"当前 AstrBot 的 ProviderRequest 不可实例化：{exc}")
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
            if fn is not None and not callable(fn):
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
        self.validate()
        return _require(self.capabilities.tool_set, "主 Agent ToolSet")

    @property
    def build_main_agent(self) -> Callable[..., Any]:
        self.validate()
        return _require(self.capabilities.build_main_agent, "build_main_agent")

    @property
    def get_session_conv(self) -> Callable[..., Any]:
        self.validate()
        return _require(self.capabilities.get_session_conv, "_get_session_conv")

    @property
    def run_agent(self) -> Callable[..., Any]:
        self.validate()
        return _require(self.capabilities.run_agent, "run_agent")

    def new_tool_set(self) -> Any:
        return self.tool_set()

    @property
    def event_type(self) -> Any:
        """宿主 EventType 枚举（成员访问经此唯一出口）。"""
        self.validate()
        return _require(self.capabilities.event_type, "EventType")

    @property
    def result_llm_type(self) -> Any:
        """宿主 ResultContentType.LLM_RESULT 值。"""
        self.validate()
        return _require(self.capabilities.result_content_type, "ResultContentType").LLM_RESULT

    def new_event_result(self) -> Any:
        """宿主 MessageEventResult 实例（构造经此唯一出口）。"""
        self.validate()
        return _require(self.capabilities.event_result_cls, "MessageEventResult")()

    def new_provider_request(self) -> Any:
        """宿主 ProviderRequest 实例。"""
        self.validate()
        return _require(self.capabilities.provider_request_cls, "ProviderRequest")()

    async def call_event_hook(self, event: Any, event_type: Any, req: Any = None) -> Any:
        """宿主事件钩子链调用（event/event_type 位置参数）。"""
        self.validate()
        hook = _require(self.capabilities.call_event_hook, "call_event_hook")
        if req is None:
            return await maybe_await(hook(event, event_type))
        return await maybe_await(hook(event, event_type, req))

    def config_path(self) -> str | None:
        """宿主配置目录（None = 旧版回退路径）。"""
        fn = self.capabilities.config_path_fn
        if not callable(fn):
            return None
        try:
            return str(fn())
        except Exception:
            return None

    def plugin_data_path(self) -> str | None:
        """宿主插件数据目录（None = 旧版回退路径）。"""
        fn = self.capabilities.plugin_data_path_fn
        if not callable(fn):
            return None
        try:
            return str(fn())
        except Exception:
            return None

    def final_tool_ids(self, req: Any) -> list[str] | None:
        """Enumerate the tool ids that would actually reach the provider.

        AstrBot resolves tools from ``req.func_tool`` at reset/run time, so the
        request object is the authoritative post-build snapshot. Returns
        ``None`` when the tool set cannot be enumerated (callers must fail
        closed).
        """
        tool_set = getattr(req, "func_tool", None)
        if tool_set is None:
            return []
        tools = getattr(tool_set, "tools", None)
        if tools is None:
            # DEBUG 而非 WARNING：本方法是被 filter_final_tools 复用的枚举器，
            # 决策与告警归调用方，否则单次失败会产生重复告警（实测 2 条）。
            logger.debug(
                "[%s] tool enumeration unavailable: func_tool has no 'tools' (type=%s)",
                PLUGIN_ID,
                type(tool_set).__name__,
            )
            return None
        try:
            return [str(getattr(tool, "name", "") or "").strip() for tool in tools]
        except Exception as exc:
            logger.debug(
                "[%s] tool enumeration failed: %s",
                PLUGIN_ID,
                exc,
            )
            return None

    def filter_final_tools(
        self,
        req: Any,
        *,
        keep: frozenset[str] | None = None,
        drop: frozenset[str] = frozenset(),
    ) -> bool:
        """Filter ``req.func_tool`` down to ``keep`` minus ``drop``.

        One of two modes is used per call:
        - ``keep`` is not None: allowlist mode. Removes every tool not in
          ``keep`` (the default proactive policy: empty allowlist removes all).
        - ``keep`` is None: denylist mode. Removes only tools in ``drop`` and
          leaves everything else untouched (inherit mode guard against
          host-dangerous capabilities, including tools a hook injected after
          build).

        Operates on ``req.func_tool`` directly, the same object the agent
        runner reads at reset and run time. Returns ``False`` when the tool
        set cannot be enumerated or a removal fails; callers must then abort
        the proactive run (fail closed).
        """
        tool_set = getattr(req, "func_tool", None)
        if tool_set is None:
            return True  # No tool set at all: nothing can be called.
        tools = getattr(tool_set, "tools", None)
        if tools is None:
            logger.warning(
                "[%s] tool boundary fail-closed: func_tool has no enumerable 'tools' "
                "(type=%s); aborting proactive run",
                PLUGIN_ID,
                type(tool_set).__name__,
            )
            return False
        try:
            for tool in list(tools):
                name = str(getattr(tool, "name", "") or "").strip()
                if not name:
                    continue
                if keep is not None:
                    if name not in keep:
                        tool_set.remove_tool(name)
                elif name in drop:
                    tool_set.remove_tool(name)
        except Exception as exc:
            logger.warning(
                "[%s] tool boundary fail-closed: removing tools failed (%s); "
                "aborting proactive run",
                PLUGIN_ID,
                exc,
            )
            return False
        remaining = self.final_tool_ids(req)
        if remaining is None:
            logger.warning(
                "[%s] tool boundary fail-closed: post-filter enumeration unavailable; "
                "aborting proactive run",
                PLUGIN_ID,
            )
            return False
        if keep is not None:
            return all(name in keep for name in remaining)
        return all(name not in drop for name in remaining)

    def new_build_config(self, **kwargs: Any) -> Any:
        self.validate()
        config_type = self.capabilities.build_config
        if config_type is None:
            raise RuntimeError("当前 AstrBot 缺少 MainAgentBuildConfig")
        return config_type(**kwargs)

    async def load_session_conversation(self, event: Any, context: Any) -> Any:
        return await self.get_session_conv(event, context)

    async def build(self, **kwargs: Any) -> Any:
        return await self.build_main_agent(**kwargs)

    def run(self, agent_runner: Any, **kwargs: Any) -> AsyncIterator[Any]:
        return self.run_agent(agent_runner, **kwargs)
