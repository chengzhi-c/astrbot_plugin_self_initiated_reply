"""Extract image components from AstrBot message events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from astrbot.api import logger

from .models import ImageInfo

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


_IMAGE_TYPES = {"image", "img", "picture", "photo"}


def _component_type(component: Any) -> str:
    value = getattr(component, "type", "")
    if not value:
        value = component.__class__.__name__
    return str(value or "").strip().lower()


def _component_value(component: Any, *names: str) -> str:
    for name in names:
        value = getattr(component, name, None)
        if value:
            return str(value).strip()
    return ""


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
            pass
    return ""


class ImageExtractor:
    """Extract image URLs or local file references from a message event."""

    @staticmethod
    def extract_images(
        event: "AstrMessageEvent",
        *,
        sender_id: str = "",
        timestamp: float = 0.0,
    ) -> list[ImageInfo]:
        images: list[ImageInfo] = []
        try:
            getter = getattr(event, "get_messages", None)
            components = getter() if callable(getter) else []
            message_id = _event_message_id(event)
            for component in components or []:
                if _component_type(component) not in _IMAGE_TYPES:
                    continue
                raw_url = _component_value(component, "url", "src")
                raw_file = _component_value(component, "file", "path", "local_path")
                parsed_file = urlparse(raw_file)
                if not raw_url and parsed_file.scheme in {"http", "https"}:
                    raw_url, raw_file = raw_file, ""
                elif raw_url and urlparse(raw_url).scheme not in {"http", "https"}:
                    if not raw_file:
                        raw_file = raw_url
                    raw_url = ""
                if not raw_url and not raw_file:
                    continue
                image_format = _component_value(component, "format", "mime_type")
                if not image_format:
                    image_format = _infer_format(raw_url or raw_file)
                images.append(
                    ImageInfo(
                        url=raw_url,
                        file_path=raw_file,
                        format=image_format,
                        message_id=message_id,
                        sender_id=str(sender_id or ""),
                        timestamp=float(timestamp or 0.0),
                    )
                )
        except Exception as exc:
            logger.debug("[selfreply] image extraction failed: %s", exc)
        return images

    @staticmethod
    def has_images(event: "AstrMessageEvent") -> bool:
        try:
            getter = getattr(event, "get_messages", None)
            components = getter() if callable(getter) else []
            return any(_component_type(component) in _IMAGE_TYPES for component in components or [])
        except Exception:
            return False


def _infer_format(value: str) -> str:
    lowered = str(value or "").lower()
    for suffix, image_format in (
        (".jpeg", "jpeg"),
        (".jpg", "jpg"),
        (".png", "png"),
        (".gif", "gif"),
        (".webp", "webp"),
    ):
        if suffix in lowered:
            return image_format
    return ""
