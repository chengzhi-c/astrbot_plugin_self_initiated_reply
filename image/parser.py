"""识图解析：把一条图片来源变成可交给 Vision 的 data URL，并取回一句描述。

拥有：SSRF 安全的远程下载传输（每跳重新解析 + 只绑定已校验的公网地址）、
冻结图片的内容寻址磁盘缓存与过期/配额清理、单图解析（并发合流、超时、拒答
过滤、描述 LRU）、本地文件放行的唯一判据（路径必须落在允许根内）。

不拥有：图片来源的提取（``extractor``）、缓存索引与内存预算
（``session_coordinator``）、provider 选择（``adapters``）、何时解析
（``vision_runtime``）。文件刻意不拆：传输层私有名被 ``tests/test_vision.py``
按本模块对象 monkeypatch，而生产侧只有一个调用方。
"""

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
    MAX_IMAGE_BYTES,
    MAX_IMAGE_CACHE_BYTES,
    MAX_IMAGE_DESCRIPTION_CACHE_BYTES,
    PLUGIN_ID,
)
from ..storage import fsync_dir, replace_with_retry
from ..utils import redact_exc_text, redact_url, response_text
from ._support import (
    ALLOWED_IMAGE_PORTS,
    HTTP_SCHEMES,
    MAX_DESCRIPTION_CHARS,
    MAX_FREEZE_BUDGET_SEC,
    MIME_EXTENSIONS,
    URL_SCHEMES,
    VISION_MAX_CONCURRENT,
    ImageCache,
    ImageInfo,
    sniff_image_mime,
    to_data_url,
    url_scheme,
)
from .recorder_bridge import MessageRecorderBridge

# 顶层常量：prompt 模板是描述缓存的语义键之一。
# VISION_PROMPT_VERSION 直接由模板内容派生，模板一改缓存键自动变，
# 不再依赖"改模板记得手动 bump 版本"的人工同步。
VISION_PROMPT_TEXT = "简要描述这张图片，重点说明文字和关键物体，不超过80字。"
VISION_SYSTEM_PROMPT_TEXT = (
    "你是主动回复插件的图片理解器。只描述图片中可观察到的内容，不要猜测身份、隐私或图片之外的信息。"
)
VISION_PROMPT_VERSION = hashlib.sha256(
    (VISION_PROMPT_TEXT + VISION_SYSTEM_PROMPT_TEXT).encode("utf-8")
).hexdigest()[:12]


_UNABLE_PATTERNS = re.compile(
    r"无法[查看].*图|不能.*[查看].*图|没有.*图片|未.*上传|"
    r"图片.*失败|无法.*分析|不能.*分析|无法.*识别|不能.*识别|"
    r"无法.*获取|不能.*获取|抱歉.*图|sorry.*image",
    re.IGNORECASE,
)
# 命中的拒答片段之外，剩余正文短于该值才判为拒答。
# 实测标定（tests/test_vision.py 双向钉住）：真拒答的剩余正文最长 8 字符，
# 有效描述最短 12 字符，阈值取 10 留余量。不设「正文过短即拒答」的独立分支：
# 有效描述也可能只有七个字，空描述由调用方处理。
_UNABLE_RESIDUAL_MIN_LENGTH = 10
# HTTP 状态码 >= 该值即视为下载失败（图片 URL 通常是 302 后的 CDN，4xx/5xx 一律放弃）。
_HTTP_ERROR_STATUS_MIN = 400
# 增量哈希的分块大小：内容寻址缓存的完整性复核不该付第二份载荷的内存。
_HASH_CHUNK_BYTES = 1 << 16
# NAT64 知名前缀：64:ff9b::/96 内的 IPv4 地址在 ipaddress 里 is_global 为 True，
# 但主机真把该前缀路由给翻译器时，::ffff:7f00:1 就是可达的环回。地址判据单点收口
# 在 _is_safe_public_address，传输层与预检共用。
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")
_SSL_CONTEXT: ssl.SSLContext | None = None

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
            # 归属校验对目录同样必需：Windows 目录联接（junction）的
            # is_symlink() 为 False 且 rglob 会穿透；resolve() 同时覆盖
            # junction 与 symlink，无需平台分支。
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
            if path.is_dir():
                directories.append(path)
                continue
            if not path.is_file():
                continue
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
    files: list[_CacheEntry], protected: set[Path], quota: int | None, *, not_before: float
) -> int:
    if quota is None:
        return 0
    removed = 0
    total_bytes = sum(size for _, _, size, _ in files)
    for mtime, path, size, resolved in sorted(files, key=lambda item: item[0]):
        if total_bytes <= quota:
            break
        if resolved in protected:
            continue
        # 清理起点之后写入的文件不在 protected 快照里——受保护源集合是调用方
        # 在 await to_thread 之前算的，这期间刚冻结的图还没进集合。超配额时
        # 按 mtime 升序会一路跳过受保护的老文件，最终删掉这个最新的活文件；
        # 而 prepared_source 已固化，parse 不会重新冻结，该图从此静默消失。
        # 宽限整个冻结预算窗口（MAX_FREEZE_BUDGET_SEC）：mtime 落在
        # [not_before - 预算, not_before] 的文件可能正被在途冻结持有、
        # 尚未 capture_images，删了同样静默消失。
        # 代价是**整个宽限窗口内的写入量都不可配额回收**，不是"一个批次"：
        # 熬过过期阶段的文件要么受保护（仍在索引里）、要么年龄 ≤ 保留窗口，
        # 因此 vision_image_age_sec ≤ 预算（滑块下限 60 < 120）时配额阶段
        # 一个都删不了。那种配置下磁盘由保留窗口兜底——文件至多存活
        # 「保留窗口 + 在途时长」，配额只是冗余的第二道闸，不是硬上限。
        if mtime > not_before - MAX_FREEZE_BUDGET_SEC:
            continue
        try:
            path.unlink()
            total_bytes -= size
            removed += 1
        except OSError:
            continue
    return removed


def _remove_empty_cache_directories(directories: list[Path], resolved_root: Path) -> None:
    """Remove empty directories, re-checking that each one belongs to the cache.

    The scan already filters by ownership; this second check is deliberate
    defence in depth, because ``rmdir`` is unrecoverable once executed.
    """
    for directory in sorted(directories, reverse=True):
        try:
            directory.resolve().relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        try:
            if next(directory.iterdir(), None) is not None:
                continue
            directory.rmdir()
        except OSError:
            pass


def _hash_file(path: Path) -> str:
    """增量 sha256：内容寻址缓存的完整性复核不该付第二份载荷的内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    """Write bytes beside the target and publish them with one replacement."""
    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
        except FileNotFoundError:
            # 清理线程在 mkdir 之后、mkstemp 之前把这个空 shard 目录 rmdir 了
            # （_remove_empty_cache_directories 的 iterdir→rmdir 与这里分处两个
            # to_thread 线程）。重建一次即可，不是内容层面的失败。
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )

        temporary_path = Path(temporary_name)
        # 写已持有的描述符，不按名字重开：重开是经典的 reopen-after-close
        # TOCTOU，期间在该随机名上植一个 symlink 就能把内容改道。
        # 必须循环：os.write 允许短写（POSIX 明文许可，大载荷+信号即可触发），
        # 少写的那截会被 os.replace 原样发布出去。
        view = memoryview(content)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        # 图片缓存写入频率高，一撞上瞬时占用就放弃会让已冻结的图静默消失（prepare
        # 退化成内存 data URL，随后被会话预算拒掉），故复用 storage 的替换重试。
        replace_with_retry(temporary_path, path)
        temporary_path = None
        # 目录 fsync 的理由见 storage.fsync_dir（失败降级不抛）。
        fsync_dir(path)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _shared_ssl_context() -> ssl.SSLContext:
    """进程内共享 TLS 上下文。

    create_default_context() 每次要读约 95 个 CA（本机实测 20-40ms），而这里每个
    请求、包括每一跳重定向都新建一个连接池：一批 5 张图最顺也要 10 次请求，
    叠起来是数百毫秒纯 CPU 停顿，且全插件共享同一事件循环。
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _is_safe_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an address is both globally routable and not a special-use range.

    ``is_global`` alone is not the whole SSRF story: ``ipaddress`` leaves
    multicast (224.0.0.0/4, ff00::/8) and the well-known NAT64 prefix
    (64:ff9b::/96) marked global, and a NAT64-mapped loopback is exactly the
    "public-looking" address that reaches an internal service. The remaining
    flags are belt-and-braces so a future CPython change to ``is_global``
    cannot silently widen the gate.
    """
    if address in _NAT64_NETWORK:
        return False
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_link_local
        and not address.is_loopback
        and not address.is_private
        and not address.is_unspecified
    )


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
        if not all(_is_safe_public_address(address) for address in parsed):
            return []
        # IPv4 优先、组内按字符串稳定排序：纯字符串排序会把双栈域名的 IPv6
        # 顶到首位，而运行主机 v6 无路由时调用方只连第一个地址，等于整站下载
        # 恒失败。不轮询下一地址：每次下载只 pin 一个已校验地址。
        return [str(ip) for ip in sorted(parsed, key=lambda ip: (ip.version, str(ip)))]
    return [str(literal)] if _is_safe_public_address(literal) else []


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


class _ImageAddressBlocked(httpx.ConnectError):
    """传输层按安全判据主动拒绝，与网络故障区分：前者值得 WARNING（契约 §9）。"""


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
        if scheme not in HTTP_SCHEMES or not host or port not in ALLOWED_IMAGE_PORTS:
            raise _ImageAddressBlocked(f"拒绝连接不安全的图片地址: {host}")
        resolver = self._resolver or _resolve_global_address
        # 一次性注入地址只在首跳生效：消费后即清空，重定向等后续每跳都重新
        # 解析并重新做公网校验（host 虽复用，地址不缓存）。
        address = self._address
        self._address = None
        address = address or await asyncio.to_thread(resolver, host)
        try:
            checked_address = ipaddress.ip_address(str(address))
        except ValueError:
            checked_address = None
        if checked_address is None or not _is_safe_public_address(checked_address):
            raise _ImageAddressBlocked(f"拒绝连接非公网主机: {host}")

        pool = httpcore.AsyncConnectionPool(
            ssl_context=_shared_ssl_context(),
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
        # 描述 LRU 的条目数上限：与字节预算（MAX_IMAGE_DESCRIPTION_CACHE_BYTES）
        # 并列。50 条的取值理由是「一个会话的近期图片索引上限是
        # MAX_CACHED_IMAGE_EVENTS × vision_max_images，同一张图的重复解析在
        # 一轮检查内必然撞上；条目上限只防不同图把 key 撑爆，不参与淘汰顺序」。
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
            # 固化为绝对路径：_file_to_data_url 只接受绝对路径，相对缓存目录
            # 会让每张冻结图永久读不回（prepare 仍回报 True、文件已落盘），
            # 而宿主给相对插件数据目录路径是完全可能的。
            self._source_cache_dir = self._source_cache_dir.expanduser().resolve()
        # 本地读取的唯一判据：路径必须落在允许根下。提取层交回的路径不可信：
        # 宿主 aiocqhttp 用对端可控的 OneBot 原始值装配 Image 的 file 字段，
        # 被控协议端能把 file 写成任意绝对路径。
        #
        # <data> 根必须在表内：宿主合法生产者写的裸绝对路径都在它下面
        # （wecom temp、webchat 等），只留 image_cache 会拒掉这些真图片；
        # 未注入 data_root 的调用方（含既有测试）不能丢掉缓存根。
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

        QQ direct URLs expire, so fetching one minutes later (when the delayed
        check runs) often hands the provider an unusable image.  The download
        therefore happens while the original event is still being handled, and a
        successful data URL is materialized into the plugin data directory with
        the ImageInfo repointed at that local file.
        """
        if not image_info.has_any_source:
            return False
        if image_info.prepared_source:
            return True
        try:
            # _resolve_image_url 只产 data URL 或 None；冻结失败不回退到远端。
            image_url = await self._resolve_image_url(image_info)
            if not image_url:
                logger.info("[%s] image source unavailable during event capture", PLUGIN_ID)
                return False
            path = (
                await asyncio.to_thread(self._materialize_data_url, image_url)
                if self._source_cache_dir
                else None
            )
            if path:
                image_info.file_path = str(path)
                image_info.prepared_source = str(path)
                logger.debug("[%s] image frozen to local cache: %s", PLUGIN_ID, path.name)
            else:
                image_info.prepared_source = image_url
                logger.debug("[%s] image frozen as in-memory data URL", PLUGIN_ID)
            return True
        except Exception as exc:
            # 脱敏后进日志：这条路径覆盖远程下载，异常文本常带签名 URL。
            logger.warning("[%s] image capture failed: %s", PLUGIN_ID, redact_exc_text(exc))
            return False

    @staticmethod
    async def _run_concurrent(
        images: list[ImageInfo], fn: Any, *, max_concurrent: int, error_value: Any
    ) -> list[Any]:
        """并发执行 fn(image) 并保持输入顺序（三个批方法共用模板）。

        ``return_exceptions`` 隔离单图异常：一张图的漏网异常不得取消同批其余
        快照。取消（CancelledError）是控制流，原样上抛。
        """
        semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))

        async def run_one(image: ImageInfo) -> Any:
            async with semaphore:
                return await fn(image)

        results = await asyncio.gather(
            *(run_one(image) for image in images), return_exceptions=True
        )
        normalized: list[Any] = []
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.debug("[%s] image batch step failed: %r", PLUGIN_ID, result)
                normalized.append(error_value)
            else:
                normalized.append(result)
        return normalized

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
            images, self._snapshot_local_source, max_concurrent=max_concurrent, error_value=False
        )

    async def _snapshot_local_source(self, image_info: ImageInfo) -> bool:
        if image_info.prepared_source:
            return True
        if not image_info.trusted_local_path or not image_info.file_path:
            return False
        file_value = str(image_info.file_path).strip()
        if url_scheme(file_value) in URL_SCHEMES:
            return False
        path = Path(file_value)
        if not path.is_absolute():
            return False
        try:
            # 这条路径的 file 值来自对端可控的 OneBot 原始值，可信度判定统一
            # 交给 _file_to_data_url 的 allowlist（契约 §7.1）。
            data_url = await asyncio.to_thread(self._file_to_data_url, path)
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
        except Exception as exc:
            # 捕获面对齐 prepare()：窄捕获会让漏网异常穿过 gather 连带取消同批
            # 其余快照；单图快照失败只该让这张图不可用。
            logger.debug("[%s] host image snapshot failed: %s", PLUGIN_ID, exc)
            return False

    async def prepare_batch(
        self, images: list[ImageInfo], *, max_concurrent: int = VISION_MAX_CONCURRENT
    ) -> list[bool]:
        """Freeze image sources concurrently while preserving input order."""
        return await self._run_concurrent(
            images, self.prepare, max_concurrent=max_concurrent, error_value=False
        )

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
                # shield 防止等待方被取消时把取消传播到共享 Future。
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
            # provider SDK 的异常串常带出请求 URL（含 api_key/Signature 等
            # query 凭证），与 decision/adapters 同口径脱敏后再记日志。
            logger.warning("[%s] image parsing failed: %s", PLUGIN_ID, redact_exc_text(exc))
            return None

    @staticmethod
    def cleanup_source_cache(
        root: Path | None,
        *,
        protected_sources: set[str] | None = None,
        max_age_sec: float,
        max_total_bytes: int | None = MAX_IMAGE_CACHE_BYTES,
        now: float | None = None,
    ) -> int:
        """Remove expired frozen image files without touching active sources."""
        if root is None:
            return 0
        cache_root = Path(root)
        if not cache_root.is_dir():
            return 0
        # 保留窗口的下限夹取在 scheduler._image_age_sec，此处只按传入值执行。
        # 清理起点同时是两道闸的共同边界：晚于它写入的文件既不算过期，也不算
        # 可驱逐（见 _remove_over_quota_cache_files 的 not_before）。
        snapshot_now = time.time() if now is None else now
        # 过期阶段同样要让出在途冻结窗口：文件在冻结时落盘、到 capture_images
        # 提交之间既不在受保护集合里（还没进索引），mtime 又已早于保留窗口
        # （vision_image_age_sec 下限 60 < MAX_FREEZE_BUDGET_SEC），直接按 cutoff
        # 删会留下指向已删文件的 prepared_source——parse 的本地分支全部失败，
        # 图片在保留期内静默消失。_remove_over_quota_cache_files 的 not_before
        # 处理的正是同一窗口，两道闸必须同口径。
        cutoff = min(snapshot_now - max_age_sec, snapshot_now - MAX_FREEZE_BUDGET_SEC)

        resolved_root = cache_root.resolve()
        protected = _resolve_protected_cache_sources(resolved_root, protected_sources)
        # rglob 整体失败继续上抛给调用方；单文件错误由扫描步骤就地跳过。
        files, directories = _scan_source_cache(cache_root, resolved_root)
        removed, survivors = _remove_expired_cache_files(files, protected, cutoff)

        removed += _remove_over_quota_cache_files(
            survivors, protected, max_total_bytes, not_before=snapshot_now
        )
        _remove_empty_cache_directories(directories, resolved_root)
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
            error_value=None,
        )

    async def _resolve_image_url(self, image_info: ImageInfo) -> str | None:
        """把一条图片记录解析成可交给 Vision 的 data URL，按可用性顺序尝试四条来源。

        1. ``prepared_source``（本插件已快照/下载的副本）；
        2. 录制桥按 message_id 找到的宿主本地文件；
        3. ``file_path``，http(s) 走下载；
        4. ``url`` 远程下载。

        本地路径一律由 ``_allowed_local_roots`` 判定，不留任何例外分支
        （契约 §7.1）：不采信提取层的可信推断，宿主 aiocqhttp 的 ``Image.file``
        是对端可控的 OneBot 原始值，提取层的信任推断可伪造。失败即继续下一路，
        全部失败返回 ``None``；``file`` 下载失败不终局，否则对端填个坏 file_path
        就能屏蔽 url 分支。
        """
        if image_info.prepared_source:
            prepared = str(image_info.prepared_source).strip()
            if prepared.startswith("data:"):
                return prepared
            prepared_path = Path(prepared)
            data_url = await asyncio.to_thread(self._file_to_data_url, prepared_path)
            if data_url:
                return data_url

        if self._recorder_bridge and image_info.message_id:
            local_path = await self._recorder_bridge.get_local_image_path(
                image_info.message_id,
                image_info.url,
            )
            if local_path:
                # 也走 allowlist：local_path 源头是对端可控的 OneBot 字段，
                # 朴素拼接的 resolver 可被 `../../..` 逃出媒体目录。recorder
                # 媒体目录在 <data>/plugin_data/ 下，已被 data_root 覆盖。
                data_url = await asyncio.to_thread(self._file_to_data_url, local_path)
                if data_url:
                    return data_url

        if image_info.file_path:
            file_value = str(image_info.file_path).strip()
            if url_scheme(file_value) in HTTP_SCHEMES:
                data_url = await self._fetch_image_data_url(file_value)
                if data_url:
                    return data_url
                # 网络故障记 INFO；地址策略拒绝已由
                # _download_image_data_url 自己记 WARNING（契约 §9）。
                # 两者分级的原因是否则第一种会把第二种淹没。
                logger.info("[%s] image URL download failed: %s", PLUGIN_ID, redact_url(file_value))
            path = Path(file_value)
            # 本地路径一律走 allowlist：trusted_local_path 是提取层的推断值，
            # 可被对端伪造。相对路径经录制桥解析后同样受同一判据约束。
            if not path.is_absolute() and self._recorder_bridge:
                resolved = await self._recorder_bridge.resolve_relative_path(file_value)
                if resolved is not None:
                    path = resolved
            data_url = await asyncio.to_thread(self._file_to_data_url, path)
            if data_url:
                return data_url

        if image_info.url:
            data_url = await self._fetch_image_data_url(image_info.url)
            if data_url:
                return data_url
            # 同上：网络故障 INFO，策略拒绝已自行 WARNING。
            logger.info("[%s] image URL download failed: %s", PLUGIN_ID, redact_url(image_info.url))
        return None

    def _materialize_data_url(self, data_url: str) -> Path | None:
        """Persist a validated data URL using a content-addressed local path."""
        header, separator, encoded = data_url.partition(",")
        if not separator or ";base64" not in header:
            return None
        mime = header[5:].split(";", 1)[0].lower().strip()
        extension = MIME_EXTENSIONS.get(mime)
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
        try:
            # 归属校验必须先于 mkdir：`root/<digest[:2]>` 是目录，被换成
            # 目录 symlink / Windows junction 时 mkdir(exist_ok=True) 会静默
            # 成功（os.mkdir 抛 FileExistsError，exist_ok 吞掉；is_dir() 跟随
            # 链接返回 True），后续写入就落在 image_cache 之外——而 _scan_source_cache
            # 按 resolved.relative_to(root) 过滤，逃逸文件从此既不计入配额也永不
            # 被回收。判据与清理扫描同一表达式。
            shard = root / digest[:2]
            resolved_root = root.resolve()
            resolved_shard = shard.resolve()
            resolved_shard.relative_to(resolved_root)
            shard.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError):
            logger.debug("[%s] image cache shard unavailable: %s", PLUGIN_ID, digest[:2])
            return None
        target = shard / f"{digest}{extension}"
        try:
            # exists() 对断开的 symlink 返回 False；先单独拒绝 symlink，
            # 避免 write_bytes 跟随链接把内容写出 image_cache。
            if target.is_symlink():
                return None
            if target.exists():
                if not target.is_file():
                    return None
                # 内容寻址的完整性前提是"文件内容 = 文件名 hash"：
                # 同大小内容被外部替换时重算哈希兜底，不符即原子重写。
                # 增量哈希而非 read_bytes：否则一次缓存命中要付两份载荷的内存。
                needs_write = target.stat().st_size != len(content) or (
                    _hash_file(target) != digest
                )
            else:
                needs_write = True
            if needs_write:
                _atomic_write(target, content)
            # 内容寻址只决定文件身份；mtime 表示最近一次被使用的生命周期。
            # 重复图片复用旧文件时刷新它，避免清理任务按旧时间提前回收。
            #
            # EXISTS 与 utime 之间有一个真实窗口：并发的过期/配额清理可以在这
            # 一刻 unlink（此时该文件还没进索引，不在受保护集合里），utime 于是
            # 抛 FileNotFoundError。这里不能当作写失败返回 None——那张图会退回
            # 内存 data URL，或在 _snapshot_local_source 里直接丢失。补写一次即可。
            try:
                os.utime(target, None)
            except FileNotFoundError:
                _atomic_write(target, content)
                os.utime(target, None)
            return target
        except OSError as exc:
            logger.debug("[%s] image cache write failed: %s", PLUGIN_ID, exc)
            return None

    def _file_to_data_url(self, path: Path) -> str | None:
        try:
            if not path.is_absolute() or path.is_symlink():
                return None
            candidate = path.resolve(strict=True)
            if not any(
                candidate == root or root in candidate.parents for root in self._allowed_local_roots
            ):
                logger.warning(
                    "[%s] rejected local image outside trusted roots: %s", PLUGIN_ID, path
                )
                return None
            return MessageRecorderBridge.image_to_data_url(candidate)
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _warn_policy_refusal(url: str, reason: str) -> None:
        """地址策略拒绝的统一出口：WARNING + 脱敏 URL。

        与网络故障的 INFO 分开（契约 §9）。URL 必须脱敏：它是聊天里出现的远端
        地址，query 可能带签名凭证，而这条日志要能给运维看。
        """
        logger.warning(
            "[%s] image download refused by address policy (%s): %s",
            PLUGIN_ID,
            reason,
            redact_url(url),
        )

    async def _fetch_image_data_url(self, url: str) -> str | None:
        """下载远程图片；整个下载（DNS+连接+读取）受单图超时约束，超限返回 None。

        没有整体预算时：httpx 的 timeout 只作用于单次操作，慢速滴流式响应体
        可让读取无限拖延，解析路径会一直占着主动检查协程。
        """
        try:
            return await asyncio.wait_for(
                self._download_image_data_url(url), timeout=self._timeout_sec
            )
        except TimeoutError:
            logger.info("[%s] image download timed out", PLUGIN_ID)
            return None

    async def _download_image_data_url(self, url: str) -> str | None:
        """下载远程图片并编码为 ``data:`` URL，失败返回 ``None``。

        安全约束（每条都是拒绝理由）：固定地址传输（每跳重新解析并绑定公网 IP）、
        TLS 证书验证、重定向上限 3 跳、体积双重设限（content-length 先看，
        流式读取再累计校验）、MIME 由载荷嗅探决定。

        日志分级（契约 §9）：**地址策略**拒绝记 WARNING——它说明有对端在敲被拦的
        门（私网、非白名单端口、逐跳重解析后落回内网），运维必须看得见；网络故障
        记 INFO。三处预检必须自己留痕：只靠传输层那条 WARNING 时，策略拒绝在预检
        处就被挡回去、走不到传输层，§9 想保住的那个信号实际是丢的。
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in HTTP_SCHEMES or not parsed.hostname:
                self._warn_policy_refusal(url, "scheme not allowed")
                return None
            if parsed.port is not None and parsed.port not in ALLOWED_IMAGE_PORTS:
                self._warn_policy_refusal(url, f"port {parsed.port} not in allowlist")
                return None
            address = await asyncio.to_thread(_resolve_global_address, parsed.hostname)
            if not address:
                self._warn_policy_refusal(url, "no global address for host")
                return None
            transport = _FixedAddressTransport(address=address)
            async with httpx.AsyncClient(
                # 单操作超时与整体预算同源（外层 wait_for 用同一 ``_timeout_sec``）：
                # 硬编码值在配大时永远吃不满，配小时又形同虚设。
                timeout=self._timeout_sec,
                follow_redirects=True,  # 跟随重定向（QQ 图片 URL 通常会 302）
                max_redirects=3,
                trust_env=False,
                transport=transport,
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code >= _HTTP_ERROR_STATUS_MIN:
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
        except _ImageAddressBlocked as exc:
            logger.warning("[%s] image download refused by address policy: %s", PLUGIN_ID, exc)
            return None
        except Exception as exc:
            logger.debug("[%s] image download failed: %s", PLUGIN_ID, redact_exc_text(exc))
            return None

    @staticmethod
    def _is_unable_to_describe(content: str) -> bool:
        """正文去掉命中片段后所剩无几，才算 provider 给不出内容。

        只按「全文任意位置命中 pattern」判定会误杀正常描述：描述一张报错截图
        本身就必须提到「图片加载失败」这类字样，被丢弃后既不写缓存、又会让
        每次触发重复调用 provider。

        已知误判（保留，不再收紧）：11 字的「图片加载失败的错误弹窗」剩余 5 字，
        与真拒答的剩余区间（≤8 字）重叠，纯长度无法区分——要区分就得判断剩余
        正文是否在描述内容，那是另一层语义判断。两种错误的代价都低（描述不写
        缓存，下一轮重试），故停在当前判据。
        """
        stripped = str(content or "").strip()
        match = _UNABLE_PATTERNS.search(stripped)
        if match is None:
            return False
        remainder = (stripped[: match.start()] + stripped[match.end() :]).strip()
        return len(remainder) < _UNABLE_RESIDUAL_MIN_LENGTH
