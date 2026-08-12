"""识图解析器缓存与会话图片上下文。

从 main 抽出：parser 缓存、后台 freeze、prompt 图片上下文。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from ..models import PLUGIN_ID
from . import format_image_context
from .models import ImageInfo
from .parser import ImageParser
from .recorder_bridge import MessageRecorderBridge


def get_image_parser(plugin: Any, provider_id: str = "") -> ImageParser | None:
    if not plugin.settings.vision_enabled:
        return None
    timeout = float(plugin.settings.vision_timeout_sec)
    if plugin._image_parser_timeout != timeout:
        plugin._image_parsers.clear()
        plugin._image_parser_timeout = timeout
    key = str(provider_id or "").strip()
    parser = plugin._image_parsers.get(key)
    if parser is None:
        parser = ImageParser(
            plugin.bridge,
            provider_id=key,
            recorder_bridge=MessageRecorderBridge(plugin.context),
            timeout_sec=timeout,
            source_cache_dir=plugin._image_cache_dir,
            data_root=plugin._data_path,
        )
        plugin._image_parsers[key] = parser
    return parser


async def prepare_images_for_session(
    plugin: Any,
    umo: str,
    *,
    generation: int,
    active_at: float,
    images: list[ImageInfo],
) -> None:
    try:
        parser = get_image_parser(plugin)
        if parser is None:
            return
        prepared = await asyncio.wait_for(
            parser.prepare_batch(images, max_concurrent=2),
            timeout=max(5.0, min(30.0, float(plugin.settings.vision_timeout_sec) * 2)),
        )
        if plugin._stopping or not plugin._gate.is_current(umo, generation):
            return
        cached_images = [image for image, ok in zip(images, prepared, strict=True) if ok]
        if not cached_images:
            logger.warning(
                "[%s] extracted %s images but none could be frozen for umo=%s",
                PLUGIN_ID,
                len(images),
                umo,
            )
            return
        plugin._coordinator.capture_images(umo, active_at, cached_images)
        logger.debug(
            "[%s] captured %s/%s images into local vision cache for umo=%s",
            PLUGIN_ID,
            len(cached_images),
            len(images),
            umo,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        logger.warning("[%s] image capture timed out for umo=%s", PLUGIN_ID, umo)
    except Exception as exc:
        logger.warning("[%s] image capture failed for umo=%s error=%s", PLUGIN_ID, umo, exc)


async def build_image_context(
    plugin: Any, umo: str, *, enabled: bool, provider_id: str = ""
) -> str:
    if not enabled:
        return ""
    parser = get_image_parser(plugin, provider_id)
    if parser is None:
        return ""
    images = plugin._coordinator.images_for(
        umo,
        vision_age_sec=float(plugin.settings.vision_image_age_sec),
        vision_skip_stickers=plugin.settings.vision_skip_stickers,
        vision_max_images=plugin.settings.vision_max_images,
    )
    if not images:
        return ""
    descriptions = await parser.parse_batch(
        images,
        umo=umo,
        max_concurrent=min(2, plugin.settings.vision_max_images),
    )
    return format_image_context(descriptions)
