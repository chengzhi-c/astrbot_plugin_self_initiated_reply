from __future__ import annotations

import inspect
import json
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from .models import MessageRecord, PLUGIN_ID
from .utils import content_to_text, maybe_await


class AstrBotBridge:
    """Small compatibility bridge for data the proactive plugin still owns.

    Reply generation now uses AstrBot's main Agent pipeline directly. Stealer and
    LivingMemory are intentionally not accessed here; they participate through
    their normal llm_tool and event-hook paths.
    """

    def __init__(self, context: Context):
        self.context = context

    @staticmethod
    def _supported_kwargs(
        func: Any,
        kwargs: dict[str, Any],
        aliases: dict[str, tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return kwargs
        params = signature.parameters.values()
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params):
            return kwargs
        supported = {
            name
            for name, param in signature.parameters.items()
            if param.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        }
        mapped: dict[str, Any] = {}
        aliases = aliases or {}
        for key, value in kwargs.items():
            candidates = aliases.get(key, (key,))
            target = next((name for name in candidates if name in supported), "")
            if target:
                mapped[target] = value
        return mapped

    @staticmethod
    async def _call_compat(
        func: Any,
        *,
        kwargs: dict[str, Any],
        minimal_kwargs: dict[str, Any],
        aliases: dict[str, tuple[str, ...]] | None = None,
    ) -> Any:
        call_kwargs = AstrBotBridge._supported_kwargs(func, kwargs, aliases)
        try:
            return await maybe_await(func(**call_kwargs))
        except TypeError as exc:
            minimal = AstrBotBridge._supported_kwargs(func, minimal_kwargs, aliases)
            if minimal == call_kwargs:
                raise
            try:
                return await maybe_await(func(**minimal))
            except TypeError:
                raise exc

    @staticmethod
    def _method_call_options(func: Any, umo: str) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            return [((), {"umo": umo}), ((umo,), {}), ((), {})]
        params = signature.parameters
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()):
            return [((), {"umo": umo}), ((), {})]
        for name in ("umo", "session_id", "unified_msg_origin"):
            if name in params:
                return [((), {name: umo}), ((), {})]
        positional = [
            param
            for param in params.values()
            if param.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
            and param.default is inspect.Parameter.empty
        ]
        if positional:
            return [((umo,), {}), ((), {})]
        return [((), {})]

    @staticmethod
    async def _call_first_supported(func: Any, umo: str, log_name: str) -> Any:
        last_type_error: TypeError | None = None
        for args, kwargs in AstrBotBridge._method_call_options(func, umo):
            try:
                return await maybe_await(func(*args, **kwargs))
            except TypeError as exc:
                last_type_error = exc
                continue
            except Exception as exc:
                logger.debug("[%s] %s failed: %s", PLUGIN_ID, log_name, exc)
                return None
        if last_type_error:
            logger.debug("[%s] %s unsupported signature: %s", PLUGIN_ID, log_name, last_type_error)
        return None

    async def llm_generate(
        self,
        *,
        provider_id: str,
        prompt: str,
        system_prompt: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Bare LLM call used only for lightweight should-reply decisions.

        Do not use this for proactive reply generation: it does not run tools.
        Reply generation lives in main.py via AstrBot's main Agent pipeline.
        """
        llm_generate = getattr(self.context, "llm_generate", None)
        if not callable(llm_generate):
            raise RuntimeError("当前 AstrBot Context 不支持 llm_generate")
        kwargs = {"chat_provider_id": provider_id, "prompt": prompt}
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return await self._call_compat(
            llm_generate,
            kwargs=kwargs,
            minimal_kwargs={"chat_provider_id": provider_id, "prompt": prompt},
            aliases={
                "chat_provider_id": ("chat_provider_id", "provider_id", "provider"),
                "prompt": ("prompt", "content", "query"),
                "system_prompt": ("system_prompt",),
                "temperature": ("temperature",),
                "max_tokens": ("max_tokens",),
            },
        )

    async def resolve_provider_id(self, umo: str, preferred: str) -> str:
        preferred = str(preferred or "").strip()
        if preferred:
            return preferred
        get_current = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(get_current):
            provider_id = await self._call_first_supported(get_current, umo, "get_current_chat_provider_id")
            if provider_id:
                return str(provider_id).strip()
        get_using = getattr(self.context, "get_using_provider", None)
        if callable(get_using):
            provider = await self._call_first_supported(get_using, umo, "get_using_provider")
            provider_id = await self._provider_id_from_meta(provider)
            if provider_id:
                return provider_id
        return ""

    @staticmethod
    async def _provider_id_from_meta(provider: Any) -> str:
        if provider is None:
            return ""
        try:
            meta_method = getattr(provider, "meta", None)
            meta = meta_method() if callable(meta_method) else None
            meta = await maybe_await(meta)
            if isinstance(meta, dict):
                return str(meta.get("id") or "").strip()
            return str(getattr(meta, "id", "") or "").strip()
        except Exception:
            return ""

    async def read_astrbot_history(self, umo: str, *, limit: int) -> list[MessageRecord]:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return []
        try:
            cid = await maybe_await(manager.get_curr_conversation_id(umo))
            if not cid:
                return []
            conversation = await maybe_await(manager.get_conversation(umo, cid))
            raw = getattr(conversation, "history", "")
            history = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            logger.debug("[%s] read astrobot history failed session=%s error=%s", PLUGIN_ID, umo, exc)
            return []
        if not isinstance(history, list):
            return []
        records: list[MessageRecord] = []
        for item in history[-limit:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            text = content_to_text(item.get("content"))
            if not text:
                continue
            records.append(
                MessageRecord(
                    role=role,
                    name="Bot" if role == "assistant" else str(item.get("name") or "用户"),
                    text=text,
                    sender_id=str(item.get("sender_id") or ""),
                    at=0.0,
                )
            )
        return records
