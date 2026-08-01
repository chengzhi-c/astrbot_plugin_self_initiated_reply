"""Vision image parser used by proactive replies."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger

from .cache import ImageCache
from .models import ImageInfo
from .recorder_bridge import MAX_IMAGE_BYTES, MessageRecorderBridge
from .safety import sniff_image_mime

try:
    import httpx
except ImportError:  # pragma: no cover - AstrBot normally bundles httpx
    httpx = None  # type: ignore[assignment]


_UNABLE_PATTERNS = re.compile(
    r"无法[查查看].*图|不能.*[查查看].*图|没有.*图片|未.*上传|"
    r"图片.*失败|无法.*分析|不能.*分析|无法.*识别|不能.*识别|"
    r"无法.*获取|不能.*获取|抱歉.*图|sorry.*image",
    re.IGNORECASE,
)


def _host_all_global(host: str) -> bool:
    """Require every DNS result for a host to be globally routable."""
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = set()
    for info in infos:
        try:
            addresses.add(ipaddress.ip_address(info[4][0]))
        except (IndexError, ValueError):
            continue
    return bool(addresses) and all(address.is_global for address in addresses)


if httpx is not None:

    class _GlobalOnlyTransport(httpx.AsyncBaseTransport):
        """Re-check DNS immediately before connecting to reduce DNS rebinding risk."""

        def __init__(self, wrapped: Any):
            self._wrapped = wrapped

        async def handle_async_request(self, request: Any) -> Any:
            host = request.url.host
            if host and not await asyncio.to_thread(_host_all_global, host):
                raise httpx.ConnectError(f"拒绝连接非公网主机: {host}")
            return await self._wrapped.handle_async_request(request)

        async def aclose(self) -> None:
            await self._wrapped.aclose()


class ImageParser:
    """Resolve an image and ask a Vision-capable provider for a short description."""

    def __init__(
        self,
        bridge: Any,
        *,
        provider_id: str = "",
        recorder_bridge: MessageRecorderBridge | None = None,
        cache: ImageCache | None = None,
        timeout_sec: float = 20.0,
        source_cache_dir: Path | None = None,
    ):
        self._bridge = bridge
        self._provider_id = str(provider_id or "").strip()
        self._recorder_bridge = recorder_bridge
        self._cache = cache or ImageCache(max_size=50)
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._source_cache_dir = Path(source_cache_dir) if source_cache_dir else None

    async def prepare(self, image_info: ImageInfo) -> bool:
        """Freeze a message image before the delayed proactive check.

        The image is downloaded and cached while the original event is still
        being handled.  Selfreply previously kept only the expiring QQ URL and
        fetched it minutes later, after which the provider often saw an unusable
        image.  A successful data URL is therefore materialized into the plugin data
        directory and the ImageInfo is changed to point at that local file.
        """
        if not image_info.has_any_source:
            return False
        if image_info.prepared_source:
            return True
        try:
            image_url = await self._resolve_image_url(image_info)
            if not image_url:
                logger.info("[selfreply] image source unavailable during event capture")
                return False
            if image_url.startswith("data:") and self._source_cache_dir:
                path = self._materialize_data_url(image_url)
                if path:
                    image_info.file_path = str(path)
                    image_info.prepared_source = str(path)
                    logger.info("[selfreply] image frozen to local cache: %s", path.name)
                    return True
            if image_url.startswith("data:"):
                image_info.prepared_source = image_url
                logger.info("[selfreply] image frozen as in-memory data URL")
                return True
            logger.warning("[selfreply] image source was not materialized; refusing delayed raw URL")
            return False
        except Exception as exc:
            logger.warning("[selfreply] image capture failed: %s", exc)
            return False

    async def prepare_batch(
        self, images: list[ImageInfo], *, max_concurrent: int = 2
    ) -> list[bool]:
        """Freeze image sources concurrently while preserving input order."""
        semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))

        async def prepare_one(image: ImageInfo) -> bool:
            async with semaphore:
                return await self.prepare(image)

        return list(await asyncio.gather(*(prepare_one(image) for image in images)))

    async def parse(self, image_info: ImageInfo, *, umo: str = "") -> str | None:
        """Parse one image and return a compact description, or ``None`` on failure."""
        if not image_info.has_any_source:
            return None
        cache_key = image_info.cache_key()
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        try:
            image_url = await self._resolve_image_url(image_info)
            if not image_url:
                logger.info("[selfreply] no usable image source for parsing")
                return None
            provider_id = await self._bridge.resolve_provider_id(umo, self._provider_id)
            if not provider_id:
                logger.info("[selfreply] no Vision provider available; skip image parsing")
                return None
            response = await asyncio.wait_for(
                self._bridge.llm_generate_direct(
                    provider_id=provider_id,
                    prompt="简要描述这张图片，重点说明文字和关键物体，不超过80字。",
                    system_prompt=(
                        "你是主动回复插件的图片理解器。只描述图片中可观察到的内容，"
                        "不要猜测身份、隐私或图片之外的信息。"
                    ),
                    temperature=0.2,
                    max_tokens=120,
                    image_urls=[image_url],
                ),
                timeout=self._timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.info("[selfreply] image parsing timed out")
            return None
        except Exception as exc:
            logger.warning("[selfreply] image parsing failed: %s", exc)
            return None

        description = self._response_text(response)
        if not description or self._is_unable_to_describe(description):
            return None
        description = description.strip()
        if len(description) > 300:
            description = description[:300].rstrip() + "..."
        self._cache.put(cache_key, description)
        return description

    @staticmethod
    def cleanup_source_cache(
        root: Path | None,
        *,
        protected_sources: set[str] | None = None,
        max_age_sec: float = 172800.0,
        now: float | None = None,
    ) -> int:
        """Remove expired frozen image files without touching active sources."""
        if root is None:
            return 0
        cache_root = Path(root)
        if not cache_root.is_dir():
            return 0
        try:
            cutoff = (time.time() if now is None else float(now)) - max(60.0, float(max_age_sec))
        except (TypeError, ValueError, OverflowError):
            return 0

        resolved_root = cache_root.resolve()
        protected: set[Path] = set()
        for source in protected_sources or set():
            value = str(source or "").strip()
            if not value or value.startswith("data:"):
                continue
            try:
                candidate = Path(value).resolve()
                candidate.relative_to(resolved_root)
                protected.add(candidate)
            except (OSError, ValueError):
                continue

        removed = 0
        for path in cache_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(resolved_root)
                if resolved in protected or path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
            except (OSError, ValueError):
                continue

        for directory in sorted(
            (item for item in cache_root.rglob("*") if item.is_dir()),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        return removed

    async def parse_batch(
        self,
        images: list[ImageInfo],
        *,
        umo: str = "",
        max_concurrent: int = 2,
    ) -> list[str | None]:
        """Parse images concurrently while preserving input order."""
        semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))

        async def parse_one(image: ImageInfo) -> str | None:
            async with semaphore:
                return await self.parse(image, umo=umo)

        return list(await asyncio.gather(*(parse_one(image) for image in images)))

    async def _resolve_image_url(self, image_info: ImageInfo) -> str | None:
        if image_info.prepared_source:
            prepared = str(image_info.prepared_source).strip()
            if prepared.startswith("data:"):
                return prepared
            prepared_path = Path(prepared)
            data_url = self._file_to_data_url(prepared_path)
            if data_url:
                return data_url

        if self._recorder_bridge and image_info.message_id:
            local_path = await self._recorder_bridge.get_local_image_path(
                image_info.message_id,
                image_info.url,
            )
            if local_path:
                data_url = self._file_to_data_url(local_path)
                if data_url:
                    return data_url

        if image_info.file_path:
            file_value = str(image_info.file_path).strip()
            parsed = urlparse(file_value)
            if parsed.scheme in {"http", "https"}:
                data_url = await self._fetch_image_data_url(file_value)
                if data_url:
                    return data_url
                logger.info("[selfreply] image URL download failed: %s", file_value[:80])
                return None
            path = Path(file_value)
            if not path.is_absolute() and self._recorder_bridge:
                resolved = self._recorder_bridge.resolve_relative_path(file_value)
                path = resolved or path
            data_url = self._file_to_data_url(path)
            if data_url:
                return data_url

        if image_info.url:
            data_url = await self._fetch_image_data_url(image_info.url)
            if data_url:
                return data_url
            logger.info("[selfreply] image URL download failed: %s", image_info.url[:80])
        return None

    def _materialize_data_url(self, data_url: str) -> Path | None:
        """Persist a validated data URL using a content-addressed local path."""
        header, separator, encoded = data_url.partition(",")
        if not separator or ";base64" not in header:
            return None
        mime = header[5:].split(";", 1)[0].lower().strip()
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
        }.get(mime)
        if not extension:
            return None
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return None
        if not content or len(content) > MAX_IMAGE_BYTES or sniff_image_mime(content) != mime:
            return None
        digest = hashlib.sha256(content).hexdigest()
        root = self._source_cache_dir
        if root is None:
            return None
        target = root / digest[:2] / f"{digest}{extension}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(content)
            return target
        except OSError as exc:
            logger.debug("[selfreply] image cache write failed: %s", exc)
            return None

    @staticmethod
    def _file_to_data_url(path: Path) -> str | None:
        if not path.is_absolute():
            return None
        return MessageRecorderBridge.image_to_data_url(path)

    async def _fetch_image_data_url(self, url: str) -> str | None:
        if httpx is None or not await self._is_safe_url(url):
            return None
        try:
            transport = _GlobalOnlyTransport(httpx.AsyncHTTPTransport(verify=True))
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,  # 跟随重定向（QQ 图片 URL 通常会 302）
                transport=transport,
            ) as client:
                response = await client.get(url)
                if response.status_code >= 400:
                    return None
                content = response.content
                if not content or len(content) > MAX_IMAGE_BYTES:
                    return None
                # The declared content-type is a hint; the payload decides.
                content_type = sniff_image_mime(content)
                if not content_type:
                    return None
                return f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
        except Exception as exc:
            logger.debug("[selfreply] image download failed: %s", exc)
            return None

    @staticmethod
    async def _is_safe_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        return await asyncio.to_thread(_host_all_global, parsed.hostname)

    @staticmethod
    def _response_text(response: Any) -> str:
        text = str(getattr(response, "completion_text", "") or "").strip()
        if text:
            return text
        chain = getattr(response, "result_chain", None)
        getter = getattr(chain, "get_plain_text", None)
        if callable(getter):
            try:
                return str(getter() or "").strip()
            except Exception:
                return ""
        return ""

    @staticmethod
    def _is_unable_to_describe(content: str) -> bool:
        stripped = str(content or "").strip()
        return len(stripped) >= 10 and bool(_UNABLE_PATTERNS.search(stripped))
