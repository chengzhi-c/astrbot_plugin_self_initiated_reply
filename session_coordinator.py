"""会话状态协作（自 main.py 拆分，ticket 07）。

把散落在主插件上的隐式会话状态收敛为每会话协作入口：最近事件与事件时间
的写入、失效级联清理（事件/时间/图片/延迟任务）、图片索引写入与读取
（过期/去重/sticker 过滤）。

失效只有单点入口 ``invalidate``：任意路径（新消息/命令/巡检/白名单移除/
终止）都会级联清理该会话的全部协作资源。代次单调性（防 ABA）与只读视图
守护仍由 SessionGate 承担，本模块不复制。

状态容器经引用共享（main 的 dict 属性保持原字段名，既有调用点与测试
不变）；延迟任务取消与代次推进经注入回调执行。

0.9.3 移除 ``SessionPhase`` FSM 与 ``mark``/``state``：五个阶段只被写入
（main 的 check 流程 4 处）而无任何生产读点——既不参与任何判定，也未进
``/status`` 面板，是一层纯记账。运行中判定改由 ``SessionGate.is_running``
与事件表直接回答。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from astrbot.api import logger

from .models import MAX_CACHED_IMAGE_EVENTS, PLUGIN_ID, now_ts
from .session_gate import SessionGate


class SessionCoordinator:
    """每会话协作：事件缓存、失效级联单点与图片索引。"""

    def __init__(
        self,
        *,
        events: dict[str, Any],
        event_at: dict[str, float],
        images: dict[str, Any],
        gate: SessionGate,
        cancel_delay: Callable[[str, bool], None],
        notify_silence: Callable[[str], None],
    ) -> None:
        self._events = events
        self._event_at = event_at
        self._images = images
        self._gate = gate
        self._cancel_delay = cancel_delay
        self._notify_silence = notify_silence

    # ------------------------------------------------------------------
    # 写点
    # ------------------------------------------------------------------

    def record_event(self, umo: str, event: Any, at: float) -> None:
        """记录一条最近事件与其时间戳（消息触发与巡检的检查素材）。

        会话活动同时意味着静默期重置：通知延迟链立即醒来复查（ticket 11）。
        """
        self._events[umo] = event
        self._event_at[umo] = at
        self._notify_silence(umo)

    def capture_images(self, umo: str, timestamp: float, cached_images: list[Any]) -> None:
        """写入一批已冻结的图片到会话索引。"""
        image_events = self._images.setdefault(umo, deque(maxlen=MAX_CACHED_IMAGE_EVENTS))
        image_events.append((timestamp, cached_images))

    def invalidate(self, umo: str, *, force_cancel: bool = False) -> int:
        """会话失效单点入口：推进代次 → 取消延迟任务 → 级联清理全部协作资源。

        返回推进后的新代次（旧 token 随之必然失效，防 ABA）。
        """
        generation = self._gate.advance(umo)
        self._cancel_delay(umo, force_cancel)
        self.clear(umo)
        return generation

    def clear(self, umo: str) -> None:
        """清理会话的协作资源（事件/时间/图片）。"""
        self._events.pop(umo, None)
        self._event_at.pop(umo, None)
        self._images.pop(umo, None)

    def reset_all(self) -> None:
        """清空全部会话协作资源（插件终止路径）。"""
        self._events.clear()
        self._event_at.clear()
        self._images.clear()

    # ------------------------------------------------------------------
    # 读侧
    # ------------------------------------------------------------------

    def images_for(
        self,
        umo: str,
        *,
        vision_age_sec: float,
        vision_skip_stickers: bool,
        vision_max_images: int,
    ) -> list[Any]:
        """返回一个会话去重后的近期图片引用（过期条目顺手清理）。"""
        events = self._images.get(umo)
        if not events:
            logger.debug("[%s] images_for: no cached images for umo=%s", PLUGIN_ID, umo)
            return []
        cutoff = now_ts() - vision_age_sec
        while events and events[0][0] < cutoff:
            events.popleft()
        if not events:
            self._images.pop(umo, None)
            return []

        candidates: list[Any] = []
        seen: set[str] = set()
        for _event_at, images in reversed(events):
            for image in reversed(images):
                if vision_skip_stickers and getattr(image, "is_sticker", False):
                    continue
                key = image.cache_key()
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(image)
                if len(candidates) >= vision_max_images:
                    return list(reversed(candidates))
        return list(reversed(candidates))
