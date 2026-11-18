"""宿主公开 API 的调用封装（``AstrBotBridge``）。

拥有：provider 的文本生成调用、provider id 解析与宿主会话历史读取。

与 ``runtime_adapter`` 的分工：这里只走 ``astrbot.api.*`` 公开层，私有层
（``astrbot.core.*``）的符号探测与契约校验全在 ``runtime_adapter``。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from .models import PLUGIN_ID, MessageRecord, history_display_name
from .utils import content_to_text, conversation_history, maybe_await, redact_exc_text


class AstrBotBridge:
    """通过公开 Context API 调用判断模型、识图 Provider 并读取会话历史。"""

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
        """调用判断模型；回复生成使用 generation.py 中的主 Agent 管线。"""
        kwargs: dict[str, Any] = {"chat_provider_id": provider_id, "prompt": prompt}
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if image_urls:
            kwargs["image_urls"] = list(image_urls)
        return await self.context.llm_generate(**kwargs)

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

        provider = self.context.get_provider_by_id(provider_id)
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
        return await text_chat(**kwargs)

    async def resolve_provider_id(self, umo: str, preferred: str) -> str:
        preferred = str(preferred or "").strip()
        if preferred:
            return preferred
        try:
            # 4.23 的公开方法为同步调用，较新宿主为协程。
            provider_id = await maybe_await(self.context.get_current_chat_provider_id(umo))
        except Exception as exc:
            if not self._is_missing_provider_error(exc):
                raise
            return ""
        return str(provider_id or "").strip()

    async def read_astrbot_history(self, umo: str, *, limit: int) -> list[MessageRecord]:
        """读宿主会话历史的最后 ``limit`` 条，归一为 ``MessageRecord`` 列表。

        失败时**一律返回空列表，从不抛出**：产出只是判断模型的补充上下文，
        取不到应降级为"没有历史"而不是让主动回复整体失败。各早退静默跳过，
        异常路径记 debug（异常文本常带连接串，经脱敏）。
        """
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return []
        try:
            cid = await maybe_await(manager.get_curr_conversation_id(umo))
            if not cid:
                return []
            conversation = await maybe_await(manager.get_conversation(umo, cid))
            history = conversation_history(conversation)
        except Exception as exc:
            # 宿主存储后端异常常含连接串，历史读取失败日志也要脱敏。
            logger.debug(
                "[%s] read astrbot history failed session=%s error=%s",
                PLUGIN_ID,
                umo,
                redact_exc_text(exc),
            )
            return []
        if not isinstance(history, list):
            return []
        # -0 等于 0，直接切片会在零条请求时返回全量历史。
        window = history[-limit:] if limit > 0 else []
        records: list[MessageRecord] = []
        for item in window:
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
                    at=0.0,
                )
            )
        return records
