"""Optional bridge to astrbot_plugin_message_recorder for local media files."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..models import PLUGIN_ID
from .safety import sniff_image_mime


RECORDER_PLUGIN_NAME = "astrbot_plugin_message_recorder"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class MessageRecorderBridge:
    """Resolve platform media references to local files when recorder is available."""

    def __init__(self, context: Any | None = None):
        self._context = context
        self._api: Any = None
        self._checked = False

    def _ensure_api(self) -> bool:
        if self._checked:
            return self._api is not None
        self._checked = True
        if self._context is None:
            return False
        try:
            get_star = getattr(self._context, "get_registered_star", None)
            if not callable(get_star):
                return False
            meta = get_star(RECORDER_PLUGIN_NAME)
            instance = getattr(meta, "star_instance", None) or getattr(meta, "instance", None)
            get_api = getattr(instance, "get_api", None)
            if not callable(get_api):
                return False
            self._api = get_api()
            return self._api is not None
        except Exception as exc:
            logger.debug("[%s] message recorder bridge unavailable: %s", PLUGIN_ID, exc)
            return False

    async def get_local_image_path(self, message_id: str, image_url: str = "") -> Path | None:
        """Find a recorded local image matching a platform message."""
        if not message_id or not self._ensure_api():
            return None
        try:
            record = await _maybe_await(self._api.get_by_platform_message_id(message_id))
            if not record:
                return None
            chain = record.get_message_chain_list()
            image_components = [
                item for item in (chain or [])
                if isinstance(item, dict) and str(item.get("type") or "").lower() == "image"
            ]
            if not image_components:
                return None
            selected = None
            if image_url:
                selected = next(
                    (item for item in image_components if str(item.get("url") or "") == image_url),
                    None,
                )
            selected = selected or image_components[0]
            local_path = str(selected.get("local_path") or "").strip()
            if not local_path:
                return None
            return self.resolve_relative_path(local_path)
        except Exception as exc:
            logger.debug("[%s] recorder image lookup failed: %s", PLUGIN_ID, exc)
            return None

    def resolve_relative_path(self, value: str) -> Path | None:
        if not value or not self._ensure_api():
            return None
        try:
            resolver = getattr(self._api, "get_media_absolute_path", None)
            if not callable(resolver):
                return None
            path = resolver(value)
            path = Path(path) if path else None
            return path if path and path.exists() and path.is_file() else None
        except Exception as exc:
            logger.debug("[%s] recorder path resolution failed: %s", PLUGIN_ID, exc)
            return None

    @staticmethod
    def image_to_data_url(path: Path) -> str | None:
        """Convert a local file to a ``data:`` URL only if it really is an image.

        The MIME type is derived from the payload's magic bytes rather than the
        file name. A file whose bytes are not a recognised image container is
        rejected outright, so an adapter-supplied path pointing at credentials,
        keys or logs can never be base64-encoded and shipped to a Vision
        provider.
        """
        try:
            if not path.is_file():
                return None
            size = path.stat().st_size
            if size <= 0 or size > MAX_IMAGE_BYTES:
                return None
            data = path.read_bytes()
            if not data or len(data) > MAX_IMAGE_BYTES:
                return None
            mime = sniff_image_mime(data)
            if not mime:
                logger.debug(
                    "[%s] rejected non-image payload path=%s", PLUGIN_ID, path
                )
                return None
            return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        except (OSError, ValueError):
            return None


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


_default_bridge: MessageRecorderBridge | None = None


def get_recorder_bridge(context: Any | None = None) -> MessageRecorderBridge:
    global _default_bridge
    if _default_bridge is None or context is not None:
        _default_bridge = MessageRecorderBridge(context)
    return _default_bridge
