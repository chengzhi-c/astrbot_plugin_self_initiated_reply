from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable


@dataclass(frozen=True)
class AgentRuntimeCapabilities:
    """The private AstrBot capabilities required by the proactive pipeline."""

    import_error: Exception | None
    tool_set: type[Any] | None
    build_config: type[Any] | None
    build_main_agent: Callable[..., Any] | None
    get_session_conv: Callable[..., Any] | None
    run_agent: Callable[..., Any] | None


class AstrBotRuntimeAdapter:
    """Keep private AstrBot Agent imports and compatibility checks in one place."""

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

    @classmethod
    def from_host(cls) -> "AstrBotRuntimeAdapter":
        try:
            from astrbot.core.agent.tool import ToolSet
            from astrbot.core.astr_agent_run_util import run_agent
            from astrbot.core.astr_main_agent import (
                MainAgentBuildConfig,
                _get_session_conv,
                build_main_agent,
            )
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
        return cls(
            AgentRuntimeCapabilities(
                import_error=None,
                tool_set=ToolSet,
                build_config=MainAgentBuildConfig,
                build_main_agent=build_main_agent,
                get_session_conv=_get_session_conv,
                run_agent=run_agent,
            )
        )

    def validate(self) -> None:
        caps = self.capabilities
        if caps.import_error is not None:
            raise RuntimeError(
                "当前 AstrBot 缺少主动回复所需的主 Agent API；"
                "请使用已验证的 AstrBot 版本，或先完成运行时适配。"
            ) from caps.import_error
        if caps.tool_set is None:
            raise RuntimeError("当前 AstrBot 缺少主 Agent ToolSet，无法建立主动回复工具边界")
        self._validate_callable("build_main_agent", caps.build_main_agent, self._BUILD_REQUIRED)
        self._validate_callable("run_agent", caps.run_agent, self._RUN_REQUIRED)
        self._validate_callable("_get_session_conv", caps.get_session_conv, frozenset())

    @staticmethod
    def _validate_callable(
        name: str,
        func: Callable[..., Any] | None,
        required_params: frozenset[str],
    ) -> None:
        if not callable(func):
            raise RuntimeError(f"当前 AstrBot 主 Agent API 不可用：{name}")
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
            raise RuntimeError(
                f"当前 AstrBot 的 {name} 签名不兼容，缺少参数：{', '.join(missing)}"
            )

    @property
    def tool_set(self) -> type[Any]:
        self.validate()
        assert self.capabilities.tool_set is not None
        return self.capabilities.tool_set

    @property
    def build_config_type(self) -> type[Any] | None:
        return self.capabilities.build_config

    @property
    def build_main_agent(self) -> Callable[..., Any]:
        self.validate()
        assert self.capabilities.build_main_agent is not None
        return self.capabilities.build_main_agent

    @property
    def get_session_conv(self) -> Callable[..., Any]:
        self.validate()
        assert self.capabilities.get_session_conv is not None
        return self.capabilities.get_session_conv

    @property
    def run_agent(self) -> Callable[..., Any]:
        self.validate()
        assert self.capabilities.run_agent is not None
        return self.capabilities.run_agent

    def new_tool_set(self) -> Any:
        return self.tool_set()

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
            return None
        try:
            return [str(getattr(tool, "name", "") or "").strip() for tool in tools]
        except Exception:
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
        except Exception:
            return False
        remaining = self.final_tool_ids(req)
        if remaining is None:
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
