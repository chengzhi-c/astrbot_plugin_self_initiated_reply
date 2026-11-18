"""识图解析器缓存与会话图片上下文：parser 缓存、后台 freeze、prompt 图片上下文。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..adapters import AstrBotBridge
from ..models import PLUGIN_ID, Settings
from ..session_coordinator import SessionCoordinator
from ..session_gate import SessionGate
from ..utils import redact_exc_text
from ._support import (
    MAX_FREEZE_BUDGET_SEC,
    VISION_MAX_CONCURRENT,
    ImageInfo,
    format_image_context,
)
from .parser import ImageParser
from .recorder_bridge import MessageRecorderBridge


class VisionService:
    """Vision parser cache plus capture/context operations for one plugin instance."""

    def __init__(
        self,
        *,
        settings: Settings,
        bridge: AstrBotBridge,
        context: Any,
        source_cache_dir: Path,
        data_root: Path,
        coordinator: SessionCoordinator,
        gate: SessionGate,
        is_stopping: Callable[[], bool],
        track_background_task: Callable[[Coroutine[Any, Any, None]], Any],
    ) -> None:
        self._settings = settings
        self._bridge = bridge
        self._context = context
        self._source_cache_dir = source_cache_dir
        self._data_root = data_root
        self._coordinator = coordinator
        self._gate = gate
        self._is_stopping = is_stopping
        self._track_background_task = track_background_task
        self._parsers: dict[str, ImageParser] = {}

    def clear_parsers(self) -> None:
        """Discard parser instances after a Vision configuration change."""
        self._parsers.clear()

    def get_image_parser(self, provider_id: str = "") -> ImageParser | None:
        if not self._settings.vision_enabled:
            return None
        timeout = float(self._settings.vision_timeout_sec)
        key = str(provider_id or "").strip()
        parser = self._parsers.get(key)
        if parser is None:
            parser = ImageParser(
                self._bridge,
                provider_id=key,
                recorder_bridge=MessageRecorderBridge(self._context),
                timeout_sec=timeout,
                source_cache_dir=self._source_cache_dir,
                data_root=self._data_root,
            )
            self._parsers[key] = parser
        return parser

    async def capture(
        self,
        umo: str,
        *,
        generation: int,
        active_at: float,
        images: list[ImageInfo],
    ) -> None:
        """Snapshot expiring local sources, then freeze images in a tracked task."""
        parser = self.get_image_parser()
        if parser is not None:
            try:
                await parser.snapshot_local_sources(images)
            except Exception as exc:
                logger.debug(
                    "[%s] local image snapshot stage failed: %s",
                    PLUGIN_ID,
                    redact_exc_text(exc),
                )
        self._track_background_task(
            self._freeze_images(
                umo,
                generation=generation,
                active_at=active_at,
                images=images,
            )
        )

    async def _freeze_images(
        self,
        umo: str,
        *,
        generation: int,
        active_at: float,
        images: list[ImageInfo],
    ) -> None:
        try:
            parser = self.get_image_parser()
            if parser is None:
                return
            # 冻结预算按批次形状推导：prepare_batch 以 VISION_MAX_CONCURRENT
            # 并发跑，N 张图需要 ceil(N/并发) 波，每波至多一个单图超时。固定
            # 「单图超时 ×2」在 vision_max_images>2 时会整批误杀（已落盘的图
            # 一并丢弃），且 min(30, ...) 的上限让调大单图超时也救不回来。
            #
            # 每波按 2 个超时算：``_resolve_image_url`` 对同一张图可能串行下载
            # 两次（``file_path`` 与 ``url`` 都可能是 http 值，参见
            # ``extractor.extract_images`` 的归一化分支），每次各有独立的
            # ``_timeout_sec``。按 1 个超时推导会让「本来还能成功」的批次被
            # wait_for 掐断，只能退回 except TimeoutError 的部分救回路径。
            concurrency = max(1, VISION_MAX_CONCURRENT)
            waves = max(1, -(-len(images) // concurrency))
            per_image = float(self._settings.vision_timeout_sec) * 2.0
            budget = max(5.0, min(MAX_FREEZE_BUDGET_SEC, waves * per_image + 5.0))
            prepared = await asyncio.wait_for(parser.prepare_batch(images), timeout=budget)
            if self._is_stopping() or not self._gate.is_current(umo, generation):
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
            accepted_images = self._coordinator.capture_images(umo, active_at, cached_images)
            accepted_count = len(accepted_images)
            logger.debug(
                "[%s] captured %s/%s images into local vision cache for umo=%s",
                PLUGIN_ID,
                accepted_count,
                len(images),
                umo,
            )
        except TimeoutError:
            # 整批超时不等于先完成的图也该丢：已冻结（prepared_source 落地）
            # 的图照常进索引，闸门与成功路径同一套判定。
            cached_images = [image for image in images if image.prepared_source]
            if cached_images and not self._is_stopping() and self._gate.is_current(umo, generation):
                self._coordinator.capture_images(umo, active_at, cached_images)
            logger.warning("[%s] image capture timed out for umo=%s", PLUGIN_ID, umo)
        except Exception as exc:
            logger.warning(
                "[%s] image capture failed for umo=%s error=%s",
                PLUGIN_ID,
                umo,
                redact_exc_text(exc),
            )

    async def build_context(self, umo: str, *, enabled: bool, provider_id: str = "") -> str:
        if not enabled:
            return ""
        parser = self.get_image_parser(provider_id)
        if parser is None:
            return ""
        images = self._coordinator.images_for(
            umo,
            vision_age_sec=float(self._settings.vision_image_age_sec),
            vision_skip_stickers=self._settings.vision_skip_stickers,
            vision_max_images=self._settings.vision_max_images,
        )
        if not images:
            return ""
        descriptions = await parser.parse_batch(
            images,
            umo=umo,
            max_concurrent=min(VISION_MAX_CONCURRENT, self._settings.vision_max_images),
        )
        return format_image_context(descriptions)
