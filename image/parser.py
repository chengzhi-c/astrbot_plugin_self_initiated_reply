"""Vision image parser used by proactive replies."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import re
import socket
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx
from astrbot.api import logger

from ..models import (
    MAX_IMAGE_CACHE_BYTES,
    MAX_IMAGE_DESCRIPTION_CACHE_BYTES,
    PLUGIN_ID,
)
from ..utils import response_text
from ._support import (
    HTTP_SCHEMES,
    LOCAL_SOURCE_SCHEMES,
    MAX_DESCRIPTION_CHARS,
    VISION_MAX_CONCURRENT,
    ImageCache,
    ImageInfo,
    sniff_image_mime,
    to_data_url,
)
from .recorder_bridge import MAX_IMAGE_BYTES, MessageRecorderBridge

VISION_PROMPT_VERSION = "v1"

# 顶层常量：prompt 模板变更必须同步 bump VISION_PROMPT_VERSION（缓存键语义，
# 守卫见 tests/test_vision_parser_gaps.py 的模板指纹锚定）。
VISION_PROMPT_TEXT = "简要描述这张图片，重点说明文字和关键物体，不超过80字。"
VISION_SYSTEM_PROMPT_TEXT = (
    "你是主动回复插件的图片理解器。只描述图片中可观察到的内容，不要猜测身份、隐私或图片之外的信息。"
)


# 日志中 URL 的最大呈现长度（含脱敏标记）：与脱敏前的裸截断口径保持一致，
# 避免"为了安全"反而把日志行拉宽。
LOG_URL_MAX_CHARS = 80
_REDACTED_QUERY_MARK = "?<redacted>"


_UNABLE_PATTERNS = re.compile(
    r"无法[查查看].*图|不能.*[查查看].*图|没有.*图片|未.*上传|"
    r"图片.*失败|无法.*分析|不能.*分析|无法.*识别|不能.*识别|"
    r"无法.*获取|不能.*获取|抱歉.*图|sorry.*image",
    re.IGNORECASE,
)

_CacheEntry = tuple[float, Path, int, Path]


def _resolve_protected_cache_sources(
    resolved_root: Path, protected_sources: set[str] | None
) -> set[Path]:
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
    return protected


def _scan_source_cache(
    cache_root: Path, resolved_root: Path
) -> tuple[list[_CacheEntry], list[Path]]:
    """Take one cache-tree snapshot while rejecting links and invalid entries."""
    files: list[_CacheEntry] = []
    directories: list[Path] = []
    for path in cache_root.rglob("*"):
        try:
            if path.is_symlink():
                continue
            if path.is_dir():
                directories.append(path)
                continue
            if not path.is_file():
                continue
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
            stat_result = path.stat()
            files.append((stat_result.st_mtime, path, stat_result.st_size, resolved))
        except (OSError, ValueError):
            continue
    return files, directories


def _remove_expired_cache_files(
    files: list[_CacheEntry], protected: set[Path], cutoff: float
) -> tuple[int, list[_CacheEntry]]:
    """Remove expired files and retain every file that still occupies quota."""
    removed = 0
    survivors: list[_CacheEntry] = []
    for entry in files:
        mtime, path, _size, resolved = entry
        if resolved in protected or mtime >= cutoff:
            survivors.append(entry)
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            survivors.append(entry)
    return removed, survivors


def _remove_over_quota_cache_files(
    files: list[_CacheEntry], protected: set[Path], quota: int | None
) -> int:
    if quota is None:
        return 0
    removed = 0
    total_bytes = sum(size for _, _, size, _ in files)
    for _, path, size, resolved in sorted(files, key=lambda item: item[0]):
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
    return removed


def _remove_empty_cache_directories(directories: list[Path]) -> None:
    for directory in sorted(directories, reverse=True):
        try:
            if next(directory.iterdir(), None) is not None:
                continue
            directory.rmdir()
        except OSError:
            pass


def _atomic_write(path: Path, content: bytes) -> None:
    """Write bytes beside the target and publish them with one replacement."""
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        temporary_path.write_bytes(content)
        with temporary_path.open("rb+") as temporary:
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _redact_url(value: str) -> str:
    """去掉 query/fragment 后再截断，避免签名 token 进日志。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return text[:LOG_URL_MAX_CHARS]
    if not parsed.scheme or not parsed.netloc:
        return text[:LOG_URL_MAX_CHARS]
    suffix = _REDACTED_QUERY_MARK if (parsed.query or parsed.fragment) else ""
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return clean[: LOG_URL_MAX_CHARS - len(suffix)] + suffix


def _global_addresses(host: str) -> list[str]:
    """Resolve a host once and return only globally routable addresses."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return []
        addresses: set[str] = set()
        for info in infos:
            try:
                addresses.add(str(ipaddress.ip_address(info[4][0])))
            except (IndexError, ValueError):
                continue
        if not addresses:
            return []
        parsed = [ipaddress.ip_address(address) for address in addresses]
        if not all(address.is_global for address in parsed):
            return []
        return sorted(addresses)
    return [str(literal)] if literal.is_global else []


def _resolve_global_address(host: str) -> str | None:
    """Return one checked address; the caller must connect to this exact value.

    ``_global_addresses`` 只有在**全部**解析结果都是公网地址时才返回非空列表，
    因此 None 即"存在私网/保留地址或解析失败"，调用方据此拒绝连接。
    """
    addresses = _global_addresses(host)
    return addresses[0] if addresses else None


class _FixedAddressBackend(httpcore.AsyncNetworkBackend):
    """Delegate sockets while replacing only the TCP destination address."""

    def __init__(self, address: str, wrapped: Any | None = None) -> None:
        if wrapped is None:
            wrapped = httpcore.AnyIOBackend()
        self._address = address
        self._wrapped = wrapped

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        del host
        return await self._wrapped.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> Any:
        del path, timeout, socket_options
        raise RuntimeError("fixed-address image downloads do not support unix sockets")

    async def sleep(self, seconds: float) -> Any:
        return await self._wrapped.sleep(seconds)


class _FixedResponseStream(httpx.AsyncByteStream):
    """Close the one-request pool together with its response body."""

    def __init__(self, stream: Any, pool: Any, release: Any) -> None:
        self._stream = stream
        self._pool = pool
        self._release = release
        self._closed = False

    async def __aiter__(self):
        try:
            async for chunk in self._stream:
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stream.aclose()
        finally:
            await self._pool.aclose()
            self._release(self._pool)


class _FixedAddressTransport(httpx.AsyncBaseTransport):
    """HTTPX transport that binds each request to its checked DNS result.

    The request URL remains the original hostname, so httpcore still sends the
    correct Host header and uses that hostname for TLS SNI. Only the TCP
    backend receives the selected IP address. A new pool is used per request;
    this makes every redirect a fresh resolve-and-bind decision.
    """

    def __init__(self, resolver: Any | None = None, address: str | None = None) -> None:
        self._resolver = resolver
        self._address = address
        self._pools: set[Any] = set()

    async def handle_async_request(self, request: Any) -> Any:
        """Resolve the request host and issue one directly bound HTTP request."""
        host = str(request.url.host or "")
        scheme = str(request.url.scheme or "").lower()
        port = request.url.port or (443 if scheme == "https" else 80)
        if scheme not in HTTP_SCHEMES or not host or port not in {80, 443}:
            raise httpx.ConnectError(f"拒绝连接不安全的图片地址: {host}")
        resolver = self._resolver or _resolve_global_address
        address = self._address
        self._address = None
        address = address or await asyncio.to_thread(resolver, host)
        try:
            checked_address = ipaddress.ip_address(str(address))
        except ValueError:
            checked_address = None
        if checked_address is None or not checked_address.is_global:
            raise httpx.ConnectError(f"拒绝连接非公网主机: {host}")

        pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=1,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            network_backend=_FixedAddressBackend(str(address)),
        )
        self._pools.add(pool)
        try:
            core_request = httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
            response = await pool.handle_async_request(core_request)
        except Exception:
            await pool.aclose()
            self._pools.discard(pool)
            raise
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_FixedResponseStream(response.stream, pool, self._pools.discard),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        pools = tuple(self._pools)
        self._pools.clear()
        for pool in pools:
            await pool.aclose()


class ImageParser:
    """Resolve an image and ask a Vision-capable provider for a short description."""

    def __init__(
        self,
        bridge: Any,
        *,
        provider_id: str = "",
        recorder_bridge: MessageRecorderBridge | None = None,
        timeout_sec: float = 20.0,
        source_cache_dir: Path | None = None,
        data_root: Path | None = None,
    ) -> None:
        self._bridge = bridge
        self._provider_id = str(provider_id or "").strip()
        self._recorder_bridge = recorder_bridge
        self._cache = ImageCache(
            max_size=50,
            max_bytes=MAX_IMAGE_DESCRIPTION_CACHE_BYTES,
        )
        # 同 key 并发解析共享同一次 provider 调用（避免重复计费）
        self._inflight: dict[str, asyncio.Future] = {}
        self._timeout_sec = max(1.0, float(timeout_sec))
        self._source_cache_dir = Path(source_cache_dir) if source_cache_dir else None
        if self._source_cache_dir is not None:
            try:
                self._source_cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("[%s] image cache directory unavailable: %s", PLUGIN_ID, exc)
        # 本地读取的唯一判据：路径必须落在允许根下。
        #
        # 为什么不能沿用「提取层推断可信」：宿主 aiocqhttp 适配器走通用
        # ComponentTypes[t](**m["data"]) 分支装配 Image，其 file 是对端可控的
        # OneBot 原始值；而 Image 是 pydantic 组件、不是 Mapping，恰好满足
        # 旧判据 `not isinstance(component, Mapping)`。preprocess_stage 只规范化
        # Record、从不动 Image，所以被控协议端可令 file 为任意绝对路径并被判为
        # host-trusted，绕过本 allowlist（危害被下游魔数嗅探收窄为「只能外传
        # 真实图片文件」，但仍是任意文件读取）。
        #
        # <data> 根必须在表内：宿主合法生产者写的裸绝对路径都在它下面
        # （wecom `<data>/temp`、webchat `<data>/webchat`），只留 image_cache
        # 会 100% 拒掉这些真图片。image_cache 本身就在 <data> 下，但仍单列——
        # 未注入 data_root 的调用方（含既有测试）不能因此丢掉缓存根。
        roots: set[Path] = set()
        for candidate in (self._source_cache_dir, Path(data_root) if data_root else None):
            if candidate is None:
                continue
            try:
                roots.add(candidate.resolve())
            except OSError as exc:
                logger.warning("[%s] local image root unavailable: %s", PLUGIN_ID, exc)
        self._allowed_local_roots = roots

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
                path = await asyncio.to_thread(self._materialize_data_url, image_url)
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
        self, images: list[ImageInfo], *, max_concurrent: int = VISION_MAX_CONCURRENT
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
        if parsed.scheme in LOCAL_SOURCE_SCHEMES:
            return False
        path = Path(file_value)
        if not path.is_absolute():
            return False
        try:
            # 不再传 trusted=True：这条路径的 file 值来自对端可控的
            # OneBot 原始值，可信度判定统一交给 _file_to_data_url 的 allowlist。
            data_url = await asyncio.to_thread(
                self._file_to_data_url,
                path,
                trusted=False,
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
        self, images: list[ImageInfo], *, max_concurrent: int = VISION_MAX_CONCURRENT
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
                        if len(description) > MAX_DESCRIPTION_CHARS:
                            description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "..."
                        self._cache.put(cache_key, description)
                        result = description
            finally:
                # 无论成功/失败/取消，唤醒所有等待方；失败不写缓存
                if not pending.done():
                    pending.set_result(result)
                self._inflight.pop(cache_key, None)
            return result
        except TimeoutError:
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
        protected = _resolve_protected_cache_sources(resolved_root, protected_sources)
        # rglob 整体失败继续上抛给调用方；单文件错误由扫描步骤就地跳过。
        files, directories = _scan_source_cache(cache_root, resolved_root)
        removed, survivors = _remove_expired_cache_files(files, protected, cutoff)

        try:
            quota = None if max_total_bytes is None else max(0, int(max_total_bytes))
        except (TypeError, ValueError, OverflowError):
            quota = None
        removed += _remove_over_quota_cache_files(survivors, protected, quota)
        _remove_empty_cache_directories(directories)
        return removed

    async def parse_batch(
        self,
        images: list[ImageInfo],
        *,
        umo: str = "",
        max_concurrent: int = VISION_MAX_CONCURRENT,
    ) -> list[str | None]:
        """Parse images concurrently while preserving input order."""
        return await self._run_concurrent(
            images,
            lambda image: self.parse(image, umo=umo),
            max_concurrent=max_concurrent,
        )

    async def _resolve_image_url(self, image_info: ImageInfo) -> str | None:
        """把一条图片记录解析成可交给 Vision 的 data URL，按可信度降序尝试四条来源。

        顺序即优先级，越靠前越可信：
        1. ``prepared_source``（本插件已快照/下载的副本，trusted）；
        2. 录制桥按 message_id 找到的宿主本地文件（trusted）；
        3. ``file_path``——http(s) 走下载；**绝对本地路径一律走 allowlist**，
           相对路径经录制桥解析成功后才升为 trusted；
        4. ``url`` 远程下载。

        ``trusted`` 只对「来源不由消息内容决定」的路径置 True（本插件缓存副本、
        录制桥按 message_id 交回的宿主文件）。消息里带来的绝对路径一律交给
        ``_allowed_local_roots`` 判定，不再采信提取层的可信推断——见 ``__init__``
        里的可达性说明（防任意本地文件读取外传）。

        失败时：任一路仅在成功时提前返回，失败即继续下一路；全部失败返回
        ``None``（调用方据此跳过该图）。下载失败会记一条 URL 已脱敏的 INFO。
        """
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
                # 也走 allowlist：这条路径不是宿主自有产物。
                # get_local_image_path 取的是记录里的 local_path，最终交给第三方
                # recorder 插件的 get_media_absolute_path 解析（recorder_bridge.py:74,86），
                # 而 local_path 源头是对端可控的 OneBot 字段。若 resolver 是朴素
                # 拼接，`../../..` 可逃出媒体目录 —— 与本阶段要关的攻击面同型。
                # recorder 媒体目录在 <data>/plugin_data/ 下，已被 data_root 覆盖，
                # 合法文件不受影响。
                data_url = await asyncio.to_thread(
                    self._file_to_data_url, local_path, trusted=False
                )
                if data_url:
                    return data_url

        if image_info.file_path:
            file_value = str(image_info.file_path).strip()
            parsed = urlparse(file_value)
            if parsed.scheme in HTTP_SCHEMES:
                data_url = await self._fetch_image_data_url(file_value)
                if data_url:
                    return data_url
                logger.info(
                    "[%s] image URL download failed: %s", PLUGIN_ID, _redact_url(file_value)
                )
                return None
            path = Path(file_value)
            # 本地路径一律走 allowlist：image_info.trusted_local_path
            # 由提取层从「组件不是 Mapping」推断，而对端可控的 OneBot file 值
            # 恰好装配成非 Mapping 的 pydantic Image，该推断可被伪造。
            # 相对路径经录制桥解析后同样不升 trusted：resolver 的入参
            # 就是这里的 file_value —— 对端可控，`../../..` 不受 is_absolute 检查
            # 拦截，朴素拼接的 resolver 会交出媒体目录之外的路径。
            if not path.is_absolute() and self._recorder_bridge:
                resolved = self._recorder_bridge.resolve_relative_path(file_value)
                if resolved is not None:
                    path = resolved
            data_url = await asyncio.to_thread(self._file_to_data_url, path, trusted=False)
            if data_url:
                return data_url

        if image_info.url:
            data_url = await self._fetch_image_data_url(image_info.url)
            if data_url:
                return data_url
            logger.info(
                "[%s] image URL download failed: %s", PLUGIN_ID, _redact_url(image_info.url)
            )
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
                # 同大小内容被外部替换时重算哈希兜底，不符即原子重写。
                needs_write = target.stat().st_size != len(content) or (
                    hashlib.sha256(target.read_bytes()).hexdigest() != digest
                )
            else:
                needs_write = True
            if needs_write:
                _atomic_write(target, content)
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
        """下载远程图片并编码为 ``data:`` URL，失败返回 ``None``。

        安全约束（每条都是拒绝理由，不可为兼容性放宽）：仅走固定地址传输（SSRF 防护，
        每跳重新解析并绑定公网 IP）、TLS 证书验证、重定向上限 3 跳、体积双重设限
        （先看 content-length，再在流式读取中累计校验，声明值不可信）、MIME 由**载荷
        嗅探**决定而非响应头声明。

        失败时全部静默返回 ``None``（调用方据此降级为"本次不带图"）：
        URL 不安全、状态码 >= 400、超限、空响应、嗅探不出图片类型、以及任何异常
        （仅异常路径记 debug）。返回 ``None`` 的语义是"这张图不可用"，不是"出错了"，
        因此不向上抛。
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in HTTP_SCHEMES or not parsed.hostname:
                return None
            if parsed.port is not None and parsed.port not in {80, 443}:
                return None
            address = await asyncio.to_thread(_resolve_global_address, parsed.hostname)
            if not address:
                return None
            transport = _FixedAddressTransport(address=address)
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,  # 跟随重定向（QQ 图片 URL 通常会 302）
                max_redirects=3,
                trust_env=False,
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
                    return to_data_url(content_type, bytes(content))
        except Exception as exc:
            logger.debug("[%s] image download failed: %s", PLUGIN_ID, exc)
            return None

    @staticmethod
    def _is_unable_to_describe(content: str) -> bool:
        stripped = str(content or "").strip()
        return len(stripped) >= 10 and bool(_UNABLE_PATTERNS.search(stripped))
