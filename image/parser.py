"""Vision image parser used by proactive replies."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger

from ..models import MAX_IMAGE_CACHE_BYTES, PLUGIN_ID
from ..utils import response_text
from .cache import ImageCache
from .models import ImageInfo
from .recorder_bridge import MAX_IMAGE_BYTES, MessageRecorderBridge
from .safety import sniff_image_mime

try:
    import httpx
except ImportError:  # pragma: no cover - AstrBot normally bundles httpx
    httpx = None  # type: ignore[assignment]


VISION_PROMPT_VERSION = "v1"

# 顶层常量：prompt 模板变更必须同步 bump VISION_PROMPT_VERSION（缓存键语义，
# 守卫见 tests/test_vision_parser_gaps.py 的模板指纹锚定）。
VISION_PROMPT_TEXT = "简要描述这张图片，重点说明文字和关键物体，不超过80字。"
VISION_SYSTEM_PROMPT_TEXT = (
    "你是主动回复插件的图片理解器。只描述图片中可观察到的内容，不要猜测身份、隐私或图片之外的信息。"
)


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
        # 同 key 并发解析共享同一次 provider 调用（避免重复计费）
        self._inflight: dict[str, asyncio.Future] = {}
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._source_cache_dir = Path(source_cache_dir) if source_cache_dir else None
        if self._source_cache_dir is not None:
            try:
                self._source_cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("[%s] image cache directory unavailable: %s", PLUGIN_ID, exc)
        self._allowed_local_roots = (
            {self._source_cache_dir.resolve()} if self._source_cache_dir is not None else set()
        )

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
                logger.info("[%s] image source unavailable during event capture", PLUGIN_ID)
                return False
            if image_url.startswith("data:") and self._source_cache_dir:
                path = self._materialize_data_url(image_url)
                if path:
                    image_info.file_path = str(path)
                    image_info.prepared_source = str(path)
                    logger.debug("[%s] image frozen to local cache: %s", PLUGIN_ID, path.name)
                    return True
            if image_url.startswith("data:"):
                image_info.prepared_source = image_url
                logger.debug("[%s] image frozen as in-memory data URL", PLUGIN_ID)
                return True
            logger.warning(
                "[%s] image source was not materialized; refusing delayed raw URL", PLUGIN_ID
            )
            return False
        except Exception as exc:
            logger.warning("[%s] image capture failed: %s", PLUGIN_ID, exc)
            return False

    @staticmethod
    async def _run_concurrent(
        images: list[ImageInfo], fn: Any, *, max_concurrent: int
    ) -> list[Any]:
        """并发执行 fn(image) 并保持输入顺序（三个批方法共用模板）。"""
        semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))

        async def run_one(image: ImageInfo) -> Any:
            async with semaphore:
                return await fn(image)

        return list(await asyncio.gather(*(run_one(image) for image in images)))

    async def snapshot_local_sources(
        self, images: list[ImageInfo], *, max_concurrent: int = 2
    ) -> list[bool]:
        """Copy host-provided temporary images before the event handler returns.

        AstrBot may delete or recycle a normalized Image's temporary path after
        the message pipeline finishes. Only sources explicitly marked by the
        extractor as host-trusted enter this fast local snapshot path; arbitrary
        ImageInfo paths remain subject to the normal cache-root restriction.
        """
        return await self._run_concurrent(
            images, self._snapshot_local_source, max_concurrent=max_concurrent
        )

    async def _snapshot_local_source(self, image_info: ImageInfo) -> bool:
        if image_info.prepared_source:
            return True
        if not image_info.trusted_local_path or not image_info.file_path:
            return False
        file_value = str(image_info.file_path).strip()
        parsed = urlparse(file_value)
        if parsed.scheme in {"http", "https", "file"}:
            return False
        path = Path(file_value)
        if not path.is_absolute():
            return False
        try:
            data_url = await asyncio.to_thread(
                self._file_to_data_url,
                path,
                trusted=True,
            )
            if not data_url:
                return False
            cached_path = await asyncio.to_thread(self._materialize_data_url, data_url)
            if not cached_path:
                return False
            image_info.file_path = str(cached_path)
            image_info.prepared_source = str(cached_path)
            logger.debug(
                "[%s] host image snapshot created: %s",
                PLUGIN_ID,
                cached_path.name,
            )
            return True
        except (OSError, RuntimeError, ValueError) as exc:
            logger.debug("[%s] host image snapshot failed: %s", PLUGIN_ID, exc)
            return False

    async def prepare_batch(
        self, images: list[ImageInfo], *, max_concurrent: int = 2
    ) -> list[bool]:
        """Freeze image sources concurrently while preserving input order."""
        return await self._run_concurrent(images, self.prepare, max_concurrent=max_concurrent)

    async def parse(self, image_info: ImageInfo, *, umo: str = "") -> str | None:
        """Parse one image and return a compact description, or ``None`` on failure."""
        if not image_info.has_any_source:
            return None
        try:
            provider_id = await self._bridge.resolve_provider_id(umo, self._provider_id)
            if not provider_id:
                logger.info("[%s] no Vision provider available; skip image parsing", PLUGIN_ID)
                return None
            cache_key = (
                f"vision:{VISION_PROMPT_VERSION}|provider:{provider_id}|{image_info.cache_key()}"
            )
            cached = self._cache.get(cache_key)
            if cached:
                return cached
            pending = self._inflight.get(cache_key)
            if pending is not None:
                # 同一图片正在并发解析：共享同一次 provider 调用，避免重复计费。
                # shield 防止等待方被取消时把取消传播到共享 Future（Task.cancel
                # 会取消其正在等待的 Future，波及其他等待方）；等待方仍正常抛
                # CancelledError，生产方结果不被吞掉。
                return await asyncio.shield(pending)
            pending = asyncio.get_running_loop().create_future()
            self._inflight[cache_key] = pending
            result: str | None = None
            try:
                image_url = await self._resolve_image_url(image_info)
                if not image_url:
                    logger.info("[%s] no usable image source for parsing", PLUGIN_ID)
                else:
                    response = await asyncio.wait_for(
                        self._bridge.llm_generate_direct(
                            provider_id=provider_id,
                            prompt=VISION_PROMPT_TEXT,
                            system_prompt=VISION_SYSTEM_PROMPT_TEXT,
                            temperature=0.2,
                            max_tokens=120,
                            image_urls=[image_url],
                        ),
                        timeout=self._timeout_sec,
                    )
                    description = response_text(response)
                    if not description or self._is_unable_to_describe(description):
                        logger.info("[%s] no usable description from provider", PLUGIN_ID)
                    else:
                        description = description.strip()
                        if len(description) > 300:
                            description = description[:300].rstrip() + "..."
                        self._cache.put(cache_key, description)
                        result = description
            finally:
                # 无论成功/失败/取消，唤醒所有等待方；失败不写缓存
                if not pending.done():
                    pending.set_result(result)
                self._inflight.pop(cache_key, None)
            return result
        except asyncio.TimeoutError:
            logger.info("[%s] image parsing timed out", PLUGIN_ID)
            return None
        except Exception as exc:
            logger.warning("[%s] image parsing failed: %s", PLUGIN_ID, exc)
            return None

    @staticmethod
    def cleanup_source_cache(
        root: Path | None,
        *,
        protected_sources: set[str] | None = None,
        max_age_sec: float = 172800.0,
        max_total_bytes: int | None = MAX_IMAGE_CACHE_BYTES,
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
            if not path.is_file() or path.is_symlink():
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

        try:
            quota = None if max_total_bytes is None else max(0, int(max_total_bytes))
        except (TypeError, ValueError, OverflowError):
            quota = None
        if quota is not None:
            entries: list[tuple[float, Path, int, Path]] = []
            total_bytes = 0
            for path in cache_root.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    resolved = path.resolve()
                    resolved.relative_to(resolved_root)
                    size = path.stat().st_size
                    entries.append((path.stat().st_mtime, path, size, resolved))
                    total_bytes += size
                except (OSError, ValueError):
                    continue
            for _, path, size, resolved in sorted(entries, key=lambda item: item[0]):
                if total_bytes <= quota:
                    break
                if resolved in protected:
                    continue
                try:
                    path.unlink()
                    total_bytes -= size
                    removed += 1
                except OSError:
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
        return await self._run_concurrent(
            images,
            lambda image: self.parse(image, umo=umo),
            max_concurrent=max_concurrent,
        )

    async def _resolve_image_url(self, image_info: ImageInfo) -> str | None:
        if image_info.prepared_source:
            prepared = str(image_info.prepared_source).strip()
            if prepared.startswith("data:"):
                return prepared
            prepared_path = Path(prepared)
            data_url = await asyncio.to_thread(self._file_to_data_url, prepared_path, trusted=True)
            if data_url:
                return data_url

        if self._recorder_bridge and image_info.message_id:
            local_path = await self._recorder_bridge.get_local_image_path(
                image_info.message_id,
                image_info.url,
            )
            if local_path:
                data_url = await asyncio.to_thread(self._file_to_data_url, local_path, trusted=True)
                if data_url:
                    return data_url

        if image_info.file_path:
            file_value = str(image_info.file_path).strip()
            parsed = urlparse(file_value)
            if parsed.scheme in {"http", "https"}:
                data_url = await self._fetch_image_data_url(file_value)
                if data_url:
                    return data_url
                logger.info("[%s] image URL download failed: %s", PLUGIN_ID, file_value[:80])
                return None
            path = Path(file_value)
            trusted = bool(image_info.trusted_local_path)
            if not path.is_absolute() and self._recorder_bridge:
                resolved = self._recorder_bridge.resolve_relative_path(file_value)
                if resolved is not None:
                    path = resolved
                    trusted = True
            data_url = await asyncio.to_thread(self._file_to_data_url, path, trusted=trusted)
            if data_url:
                return data_url

        if image_info.url:
            data_url = await self._fetch_image_data_url(image_info.url)
            if data_url:
                return data_url
            logger.info("[%s] image URL download failed: %s", PLUGIN_ID, image_info.url[:80])
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
            # exists() 对断开的 symlink 返回 False；先单独拒绝 symlink，
            # 避免 write_bytes 跟随链接把内容写出 image_cache。
            if target.is_symlink():
                return None
            if target.exists():
                if not target.is_file():
                    return None
                # 内容寻址的完整性前提是"文件内容 = 文件名 hash"：
                # write_bytes 中断会留下半截文件，命中分支先校验大小；
                # 同大小内容被外部替换时重算哈希兜底，不符即重写。
                if target.stat().st_size != len(content) or (
                    hashlib.sha256(target.read_bytes()).hexdigest() != digest
                ):
                    target.write_bytes(content)
            else:
                target.write_bytes(content)
            # 内容寻址只决定文件身份；mtime 表示最近一次被使用的生命周期。
            # 重复图片复用旧文件时刷新它，避免清理任务按旧时间提前回收。
            os.utime(target, None)
            return target
        except OSError as exc:
            logger.debug("[%s] image cache write failed: %s", PLUGIN_ID, exc)
            return None

    def _file_to_data_url(self, path: Path, *, trusted: bool = False) -> str | None:
        try:
            if not path.is_absolute() or path.is_symlink():
                return None
            candidate = path.resolve(strict=True)
            if not trusted and not any(
                candidate == root or root in candidate.parents for root in self._allowed_local_roots
            ):
                logger.warning(
                    "[%s] rejected local image outside trusted roots: %s", PLUGIN_ID, path
                )
                return None
            return MessageRecorderBridge.image_to_data_url(candidate)
        except (OSError, RuntimeError, ValueError):
            return None

    async def _fetch_image_data_url(self, url: str) -> str | None:
        if httpx is None or not await self._is_safe_url(url):
            return None
        try:
            transport = _GlobalOnlyTransport(httpx.AsyncHTTPTransport(verify=True))
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,  # 跟随重定向（QQ 图片 URL 通常会 302）
                max_redirects=3,
                transport=transport,
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        return None
                    content_length = response.headers.get("content-length")
                    try:
                        if content_length and int(content_length) > MAX_IMAGE_BYTES:
                            return None
                    except (TypeError, ValueError):
                        pass
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_IMAGE_BYTES:
                            return None
                    if not content:
                        return None
                    # The declared content-type is a hint; the payload decides.
                    content_type = sniff_image_mime(bytes(content))
                    if not content_type:
                        return None
                    return f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
        except Exception as exc:
            logger.debug("[%s] image download failed: %s", PLUGIN_ID, exc)
            return None

    @staticmethod
    async def _is_safe_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.port is not None and parsed.port not in {80, 443}:
            # 仅允许标准 Web 端口，收缩公网主机任意端口可达的 SSRF 面
            return False
        return await asyncio.to_thread(_host_all_global, parsed.hostname)

    @staticmethod
    def _is_unable_to_describe(content: str) -> bool:
        stripped = str(content or "").strip()
        return len(stripped) >= 10 and bool(_UNABLE_PATTERNS.search(stripped))
