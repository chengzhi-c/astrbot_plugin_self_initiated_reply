"""宿主公开 API 的调用封装（``AstrBotBridge``）。

拥有：provider 的文本生成调用与形态兼容、provider id 解析、宿主会话历史
读取，以及这些调用在不同宿主版本上的返回形态归一。

与 ``runtime_adapter`` 的分工是本文件最容易混淆的一点：这里只走
``astrbot.api.*`` 公开层，私有层（``astrbot.core.*``）的符号探测与契约校验
全在 ``runtime_adapter``。两者都叫「适配」，但隔离对象不同，不可合并。
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from .models import PLUGIN_ID, MessageRecord, history_display_name
from .utils import content_to_text, maybe_await, redact_exc_text


class AstrBotBridge:
    """Small compatibility bridge for data the proactive plugin still owns.

    Reply generation now uses AstrBot's main Agent pipeline directly. Stealer and
    LivingMemory are intentionally not accessed here; they participate through
    their normal llm_tool and event-hook paths.
    """

    def __init__(self, context: Context) -> None:
        self.context = context

    @staticmethod
    def _is_missing_provider_error(exc: Exception) -> bool:
        """识别未由公开 Context API 导出的缺失 Provider 异常。"""
        return any(
            error_type.__module__ == "astrbot.core.exceptions"
            and error_type.__name__ == "ProviderNotFoundError"
            for error_type in type(exc).__mro__
        )

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
            if param.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
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
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            signature = None
        if signature is not None and not any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
        ):
            # 预校验参数绑定：只有签名不匹配（绑定失败）才回退 minimal；
            # 函数体内部抛出的 TypeError 直接上抛，绝不重试——重试意味着
            # 同一函数可能执行两次（对 LLM 调用即重复计费）。
            try:
                signature.bind(**call_kwargs)
            except TypeError as exc:
                minimal = AstrBotBridge._supported_kwargs(func, minimal_kwargs, aliases)
                if minimal == call_kwargs:
                    raise
                try:
                    return await maybe_await(func(**minimal))
                except TypeError:
                    raise exc from None
        return await maybe_await(func(**call_kwargs))

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
            if param.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
            and param.default is inspect.Parameter.empty
        ]
        if positional:
            return [((umo,), {}), ((), {})]
        return [((), {})]

    @staticmethod
    async def _call_first_supported(func: Any, umo: str, log_name: str) -> Any:
        try:
            signature = inspect.signature(func)
        except (TypeError, ValueError):
            args, kwargs = AstrBotBridge._method_call_options(func, umo)[0]
            try:
                return await maybe_await(func(*args, **kwargs))
            except Exception as exc:
                # 宿主方法失败常把带凭证的请求 URL 写进异常文本，日志同样要脱敏。
                logger.warning("[%s] %s failed: %s", PLUGIN_ID, log_name, redact_exc_text(exc))
                raise

        last_type_error: TypeError | None = None
        for args, kwargs in AstrBotBridge._method_call_options(func, umo):
            try:
                signature.bind(*args, **kwargs)
            except TypeError as exc:
                last_type_error = exc
                continue
            try:
                return await maybe_await(func(*args, **kwargs))
            except Exception as exc:
                logger.warning("[%s] %s failed: %s", PLUGIN_ID, log_name, redact_exc_text(exc))
                raise
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
        image_urls: list[str] | None = None,
    ) -> Any:
        """Bare LLM call used only for lightweight should-reply decisions.

        Do not use this for proactive reply generation: it does not run tools.
        Reply generation lives in main.py via AstrBot's main Agent pipeline.
        """
        llm_generate = getattr(self.context, "llm_generate", None)
        if not callable(llm_generate):
            raise RuntimeError("当前 AstrBot Context 不支持 llm_generate")
        kwargs: dict[str, Any] = {"chat_provider_id": provider_id, "prompt": prompt}
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if image_urls:
            kwargs["image_urls"] = list(image_urls)
        # 不走 _call_compat：宿主 llm_generate 是带 **kwargs 的公开方法，无需
        # 别名猜测与 minimal 回退；text_chat 侧兼容层在 llm_generate_direct。
        call_kwargs = self._supported_kwargs(llm_generate, kwargs)
        if image_urls and "image_urls" not in call_kwargs:
            # 图片支持是硬前提，不可静默降级成纯文本判断（看不见图还照样下结论）。
            raise RuntimeError("当前 AstrBot Context 的 LLM 接口不支持图片输入")
        return await maybe_await(llm_generate(**call_kwargs))

    async def llm_generate_direct(
        self,
        *,
        provider_id: str,
        prompt: str,
        system_prompt: str = "",
        image_urls: list[str] | None = None,
        contexts: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Call a Provider directly, bypassing the normal LLM hook chain.

        This is intentionally used for Vision parsing so image analysis does not
        recursively trigger this plugin or other request hooks.
        """
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            raise RuntimeError("未指定图片解析 Provider")
        image_urls = list(image_urls or [])
        if not image_urls:
            raise ValueError("图片解析至少需要一个 image_url")

        provider = None
        get_provider = getattr(self.context, "get_provider_by_id", None)
        if callable(get_provider):
            provider = await maybe_await(get_provider(provider_id))
        if provider is None:
            manager = getattr(self.context, "provider_manager", None)
            manager_get_provider = getattr(manager, "get_provider_by_id", None)
            if callable(manager_get_provider):
                provider = await maybe_await(manager_get_provider(provider_id))
            if provider is None:
                inst_map = getattr(manager, "inst_map", None)
                if isinstance(inst_map, dict):
                    provider = inst_map.get(provider_id)
        text_chat = getattr(provider, "text_chat", None)
        if not callable(text_chat):
            raise RuntimeError(f"Provider 不可用或不支持 text_chat: {provider_id}")

        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "contexts": contexts or [],
            "system_prompt": system_prompt,
            "image_urls": image_urls,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        aliases = {
            "prompt": ("prompt", "content", "query"),
            "contexts": ("contexts", "context"),
            "system_prompt": ("system_prompt", "system"),
            "image_urls": ("image_urls", "images", "image_urls_list"),
            "temperature": ("temperature",),
            "max_tokens": ("max_tokens", "max_new_tokens"),
        }
        supported = self._supported_kwargs(text_chat, kwargs, aliases)
        if not any(key in supported for key in aliases["image_urls"]):
            raise RuntimeError(f"Provider 不支持图片输入: {provider_id}")
        return await self._call_compat(
            text_chat,
            kwargs=kwargs,
            minimal_kwargs={"prompt": prompt, "contexts": contexts or [], "image_urls": image_urls},
            aliases=aliases,
        )

    async def resolve_provider_id(self, umo: str, preferred: str) -> str:
        preferred = str(preferred or "").strip()
        if preferred:
            return preferred
        get_current = getattr(self.context, "get_current_chat_provider_id", None)
        if callable(get_current):
            try:
                provider_id = await self._call_first_supported(
                    get_current, umo, "get_current_chat_provider_id"
                )
            except Exception as exc:
                if not self._is_missing_provider_error(exc):
                    raise
                provider_id = ""
            if provider_id:
                return str(provider_id).strip()
        # 二级回退：get_current_chat_provider_id 抛 ProviderNotFoundError 时，
        # 由 get_using_provider + meta().id 兜底。
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
        """读宿主会话历史的最后 ``limit`` 条，归一为 ``MessageRecord`` 列表。

        宿主把 history 存成 JSON 字符串或已解析列表两种形态，此处都接受；
        单条记录经 ``content_to_text`` 归一（多模态分片只取文本部分）。

        失败时**一律返回空列表，从不抛出**：本方法的产出只是判断模型的补充
        上下文，取不到应当降级为"没有历史"而不是让主动回复整体失败。因此
        六条早退（无 conversation_manager / 无当前会话 id / 读取或 JSON 解析
        异常 / history 非列表 / 单条非 dict / role 不在 user|assistant / 文本为空）
        都静默跳过，只有异常路径记 debug 日志。
        """
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
            logger.debug(
                "[%s] read astrobot history failed session=%s error=%s", PLUGIN_ID, umo, exc
            )
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
                    name=history_display_name(role, item.get("name")),
                    text=text,
                    sender_id=str(item.get("sender_id") or ""),
                    at=0.0,
                )
            )
        return records
