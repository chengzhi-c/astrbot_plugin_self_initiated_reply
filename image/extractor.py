"""Extract image components from AstrBot message events."""

from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Iterator, Mapping
from html import unescape
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

from ..models import PLUGIN_ID
from ..utils import event_message_id
from ._support import HTTP_SCHEMES, URL_SCHEMES, ImageInfo, url_scheme

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
    """读组件字段：先组件本体、再嵌套 ``data``。与 :func:`_field_value` 不是一个
    抽象，勿合并：本函数处理组件特有的两层回退，后者是通用的单层取值。"""
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
    if url_scheme(normalized) in URL_SCHEMES:
        return False
    return os.path.isabs(normalized) or ntpath.isabs(normalized)


def _is_sticker_marker(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    # OneBot subType 实际是 0/1 整数；"true" 覆盖 raw dict 显式布尔。其余写法
    # 无宿主样本证据，收窄以免臆想兼容面持续膨胀。
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true"}


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
    """通用单层取值（Mapping → ``get`` 方法 → 属性），不查嵌套 ``data``。"""
    if isinstance(source, Mapping):
        return source.get(name)
    getter = getattr(source, "get", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:
            # 消息段结构不可信：不同宿主/协议端的 get 可能签名不兼容。静默让
            # 下方 getattr 兜底路径继续生效。
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


def _eligible_image_entries(event: Any, *, skip_stickers: bool) -> Iterator[tuple[Any, Any]]:
    """产出参与判定的图片条目（``(归一化组件, 原始段)``），按需滤掉表情包。

    ``has_images``（是否存在图片）与 ``extract_images``（能否抽出可用来源）是
    两个判据，不能互相替代：组件存在但 url/file 全空时前者为真、后者为空。
    贴纸判据只在 ``skip_stickers`` 为真时计算：``has_images`` 把任何异常都当
    "没有图片"，无条件计算会给纯图片消息新开一条被整条丢弃的路径。
    """
    for component, raw_component in _image_entries(event):
        if skip_stickers and _component_is_sticker(component, raw_component=raw_component):
            continue
        yield component, raw_component


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


class ImageExtractor:
    """Extract image URLs or local file references from a message event."""

    @staticmethod
    def extract_images(
        event: AstrMessageEvent,
        *,
        skip_stickers: bool = False,
    ) -> list[ImageInfo]:
        """从消息事件抽取图片来源，归一化为 ``ImageInfo`` 列表。

        对每个图片组件：判定表情包、在归一化组件与原始 OneBot 组件之间取回
        可用来源（AstrBot 可能已把 Image 规范化为临时文件）、按 scheme 把 URL
        与本地路径归位。``trusted_local_path`` 只作宿主临时图的**快照分流提示**；
        本地读取的放行判据与之无关，唯一判据是路径落在允许根内（契约 §7.1）。

        失败时整体 try 包裹，任何宿主结构异常只记 debug 并返回已抽到的部分
        （宁少不炸，图片缺失只降级为纯文本主动回复）。
        """
        images: list[ImageInfo] = []
        try:
            message_id = event_message_id(event)
            for component, raw_component in _eligible_image_entries(
                event, skip_stickers=skip_stickers
            ):
                is_sticker = _component_is_sticker(
                    component,
                    raw_component=raw_component,
                )
                raw_url = _component_value(component, "url")
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
                    raw_url = raw_url or _component_value(raw_component, "url")
                    raw_file = raw_file or _component_value(
                        raw_component,
                        "file",
                        "path",
                        "local_path",
                    )
                # scheme 判定统一走 _support.url_scheme：畸形值（`http://[::1/x`
                # 让 urlparse 抛 ValueError）按「无 scheme」处理，单组件的坏值
                # 不得中断整条消息的图片提取。
                if not raw_url and url_scheme(raw_file) in HTTP_SCHEMES:
                    raw_url, raw_file = raw_file, ""
                elif raw_url:
                    if url_scheme(raw_url) not in HTTP_SCHEMES:
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
        """是否存在**参与判定**的图片组件（不要求能抽出可用来源）。

        与 :meth:`extract_images` 共用 ``_eligible_image_entries`` 的过滤，但
        不构造 ``ImageInfo``：只回答"这条消息有图吗"，用于决定是否走图片分支。
        """
        try:
            return any(True for _ in _eligible_image_entries(event, skip_stickers=skip_stickers))
        except Exception:
            return False
