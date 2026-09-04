"""Extract image components from AstrBot message events."""

from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Mapping
from html import unescape
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from astrbot.api import logger

from ..models import PLUGIN_ID
from ._support import ImageInfo

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


_IMAGE_TYPES = {"image", "img", "picture", "photo"}
_CQ_COMPONENT_RE = re.compile(
    r"\[CQ:(?P<type>[^,\]]+)(?:,(?P<body>[^\]]*))?\]",
    re.IGNORECASE,
)


def _parse_raw_cq_components(raw: Any) -> list[dict[str, Any]]:
    """Recover image segments when an adapter exposes only raw CQ text."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", errors="replace")
        except Exception:
            return []
    if not isinstance(raw, str):
        return []
    components: list[dict[str, Any]] = []
    for match in _CQ_COMPONENT_RE.finditer(raw):
        component_type = str(match.group("type") or "").strip().lower()
        if component_type not in _IMAGE_TYPES:
            continue
        data: dict[str, str] = {}
        for item in str(match.group("body") or "").split(","):
            key, separator, value = item.partition("=")
            if not separator:
                continue
            data[unescape(key).strip()] = unescape(value).strip()
        components.append({"type": component_type, "data": data})
    return components


def _component_field(component: Any, name: str) -> Any:
    """Read a component field across AstrBot objects and raw mapping shapes."""
    sources = [component]
    nested = (
        component.get("data") if isinstance(component, dict) else getattr(component, "data", None)
    )
    if nested is not None:
        sources.append(nested)
    for source in sources:
        if isinstance(source, dict) and name in source:
            return source[name]
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _component_type(component: Any) -> str:
    value = _component_field(component, "type")
    if not value:
        value = component.__class__.__name__
    result = str(value or "").strip().lower()
    # AstrBot 将组件类型封装为枚举，转字符串后形如 "componenttype.image"
    # 取最后一段，兼容裸字符串和枚举两种写法
    if "." in result:
        result = result.rsplit(".", 1)[-1]
    return result


def _component_value(component: Any, *names: str) -> str:
    for name in names:
        value = _component_field(component, name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _is_absolute_local_source(value: str) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    if urlparse(normalized).scheme in {"http", "https", "file"}:
        return False
    return os.path.isabs(normalized) or ntpath.isabs(normalized)


def _is_sticker_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    return normalized in {
        "1",
        "true",
        "yes",
        "on",
        "sticker",
        "emoji",
        "face",
        "表情",
        "表情包",
        "贴图",
    }


def _explicit_sticker_marker(component: Any) -> tuple[bool, bool]:
    """Read one component's explicit sticker marker.

    The first tuple item distinguishes ``subType=0``/``False`` from a missing
    field.  That distinction matters when a normalized AstrBot Image defaults
    ``subType`` to zero while the raw OneBot segment still says ``subType=1``.
    """
    for name in ("subType", "sub_type", "subtype", "is_sticker", "is_emoji", "sticker", "emoji"):
        value = _component_field(component, name)
        if value is not None:
            return True, _is_sticker_marker(value)
    return False, False


def _component_is_sticker(component: Any, *, raw_component: Any = None) -> bool:
    """Return whether the platform explicitly marks an image as a sticker.

    AstrBot's aiocqhttp adapter normalizes a OneBot image into ``Image`` and
    may drop platform-only fields such as ``subType``.  When the event retains
    the raw OneBot message, its marker is authoritative over normalized
    defaults; otherwise the normalized component metadata is used.
    """
    if raw_component is not None:
        found, is_sticker = _explicit_sticker_marker(raw_component)
        if found:
            return is_sticker
    _, is_sticker = _explicit_sticker_marker(component)
    return is_sticker


def _field_value(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    getter = getattr(source, "get", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:
            # 消息段结构不可信：不同宿主/协议端的段对象可能提供签名不兼容的 get
            # （如要求两个参数、或对未知键抛错）。此处静默是为了让下方 getattr
            # 兜底路径继续生效——字段取不到应回退为 None，而不是让整条图片
            # 提取链失败。
            pass
    return getattr(source, name, None)


def _event_raw_message(event: Any) -> Any:
    """Return the platform raw event retained by AstrBot, when available."""
    message_obj = getattr(event, "message_obj", None)
    for owner in (event, message_obj):
        if owner is None:
            continue
        for name in ("raw_message", "raw_event"):
            value = _field_value(owner, name)
            if value is not None:
                return value
    return None


def _raw_image_components(event: Any) -> list[Any]:
    """Extract direct raw image segments for platform metadata recovery."""
    raw = _event_raw_message(event)
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        return _parse_raw_cq_components(raw)
    segments = _field_value(raw, "message")
    if isinstance(segments, (str, bytes)):
        return _parse_raw_cq_components(segments)
    if segments is None and isinstance(raw, (list, tuple)):
        segments = raw
    if not isinstance(segments, (list, tuple)):
        return []
    return [component for component in segments if _component_type(component) in _IMAGE_TYPES]


def _image_entries(event: Any) -> list[tuple[Any, Any]]:
    """Pair normalized image components with raw image segments by order."""
    getter = getattr(event, "get_messages", None)
    components = getter() if callable(getter) else []
    raw_components = _raw_image_components(event)
    entries: list[tuple[Any, Any]] = []
    raw_index = 0
    for component in components or []:
        if _component_type(component) not in _IMAGE_TYPES:
            continue
        raw_component = raw_components[raw_index] if raw_index < len(raw_components) else None
        raw_index += 1
        entries.append((component, raw_component))
    return entries


def _event_message_id(event: Any) -> str:
    for name in ("message_id", "msg_id"):
        value = getattr(event, name, None)
        if value:
            return str(value).strip()
    message_obj = getattr(event, "message_obj", None)
    for name in ("message_id", "msg_id"):
        value = getattr(message_obj, name, None)
        if value:
            return str(value).strip()
    getter = getattr(event, "get_message_id", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            # 宿主 get_message_id 的实现不受本插件约束（可能依赖已失效的连接态）。
            # 消息 ID 仅用于图片缓存去重与日志定位，取不到就返回空串走无 ID 路径，
            # 不能让取 ID 失败中断整条图片提取。
            pass
    return ""


class ImageExtractor:
    """Extract image URLs or local file references from a message event."""

    @staticmethod
    def extract_images(
        event: AstrMessageEvent,
        *,
        skip_stickers: bool = False,
    ) -> list[ImageInfo]:
        """从消息事件抽取图片来源，归一化为 ``ImageInfo`` 列表。

        对每个图片组件做三件事：判定表情包（``skip_stickers`` 时跳过）、
        在归一化组件与原始 OneBot 组件之间取回可用来源（AstrBot 可能已把 Image
        规范化为只在当前事件阶段有效的临时文件，此时需回捞原始 URL/file）、
        按 scheme 把 URL 与本地路径归位（``file`` 里放的 http(s) 提升为 url，
        ``url`` 里放的非 http(s) 降级为 file）。

        ``trusted_local_path`` 只在**非 Mapping** 的归一化组件且来源是绝对本地
        路径时为真：原始 mapping 可能携带用户/平台可控数据，默认不可信，下游
        ``_file_to_data_url`` 据此决定是否放行本地读取（防任意文件读取外传）。

        失败时：整体 try 包裹，任何宿主结构异常只记 debug 并返回**已抽到的部分**
        （宁少不炸——图片是增强信息，缺失只降级为纯文本主动回复）。
        """
        images: list[ImageInfo] = []
        try:
            message_id = _event_message_id(event)
            for component, raw_component in _image_entries(event):
                is_sticker = _component_is_sticker(
                    component,
                    raw_component=raw_component,
                )
                if skip_stickers and is_sticker:
                    continue
                raw_url = _component_value(component, "url", "src")
                normalized_file = _component_value(component, "file", "path", "local_path")
                # Only a non-mapping, normalized AstrBot component may mark an
                # absolute local source as host-trusted. Raw mappings can carry
                # user/platform data and remain untrusted by default.
                normalized_local_source = normalized_file or raw_url
                trusted_local_path = bool(
                    not isinstance(component, Mapping)
                    and _is_absolute_local_source(normalized_local_source)
                )
                raw_file = normalized_file
                # AstrBot may normalize an Image's source to a temporary local
                # file before this plugin runs.  Prefer it, but recover the
                # original OneBot URL/file metadata when the normalized object
                # no longer carries a usable source.
                if raw_component is not None:
                    raw_url = raw_url or _component_value(raw_component, "url", "src")
                    raw_file = raw_file or _component_value(
                        raw_component,
                        "file",
                        "path",
                        "local_path",
                    )
                parsed_file = urlparse(raw_file)
                if not raw_url and parsed_file.scheme in {"http", "https"}:
                    raw_url, raw_file = raw_file, ""
                elif raw_url and urlparse(raw_url).scheme not in {"http", "https"}:
                    if not raw_file:
                        raw_file = raw_url
                    raw_url = ""
                if not raw_url and not raw_file:
                    continue
                images.append(
                    ImageInfo(
                        url=raw_url,
                        file_path=raw_file,
                        message_id=message_id,
                        is_sticker=is_sticker,
                        trusted_local_path=trusted_local_path,
                    )
                )
        except Exception as exc:
            logger.debug("[%s] image extraction failed: %s", PLUGIN_ID, exc)
        return images

    @staticmethod
    def has_images(event: AstrMessageEvent, *, skip_stickers: bool = False) -> bool:
        try:
            return any(
                not (
                    skip_stickers
                    and _component_is_sticker(
                        component,
                        raw_component=raw_component,
                    )
                )
                for component, raw_component in _image_entries(event)
            )
        except Exception:
            return False

    @staticmethod
    def is_sticker(component: Any) -> bool:
        """Expose platform sticker detection for diagnostics and tests."""
        return _component_is_sticker(component)
