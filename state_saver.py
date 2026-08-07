"""状态落盘合并（ticket 12）：脏标记 + 合并写。

主动回复的高频落盘路径（每次尝试都写盘）改为：置脏并调度一次延迟
flush，窗口内重复置脏只复用同一调度（合并写）；窗口静默后自动落盘。
``flush`` 是强制落盘（进程终止/插件重载路径），失败保持脏状态并在
窗口后自动重试，最终落盘由调用方（terminate）兜底。

白名单双写（同步回滚语义）不经过本合并器，保持逐次落盘。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from astrbot.api import logger

from .models import PLUGIN_ID, STATE_SAVE_DEBOUNCE_SEC


class DebouncedStateSaver:
    """脏标记 + 合并写：连续记录只在静默窗口结束后落盘一次。"""

    def __init__(
        self,
        *,
        do_save: Callable[[], Awaitable[None]],
        debounce_sec: float = STATE_SAVE_DEBOUNCE_SEC,
    ) -> None:
        self._do_save = do_save
        self._debounce_sec = float(debounce_sec)
        self._pending = False
        self._task: asyncio.Task[None] | None = None
        self.saved_count = 0

    @property
    def pending(self) -> bool:
        return self._pending

    def mark_dirty(self) -> None:
        """置脏并确保已调度一次延迟 flush（窗口内重复置脏不重建任务）。"""
        if self._pending:
            return
        self._pending = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(self._debounce_sec)
            await self.flush()
        except asyncio.CancelledError:
            # 被 cancel/flush 取消：脏标记保留，落盘由调用方接管
            raise

    async def flush(self) -> bool:
        """强制落盘；取消未到期的延迟 flush。失败保持脏状态并自动重试。"""
        current = asyncio.current_task()
        if self._task is not None and not self._task.done() and self._task is not current:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if not self._pending:
            return True
        self._pending = False
        try:
            await self._do_save()
        except asyncio.CancelledError:
            # 定时路径（_flush_later 自调用）的取消会在此投递：恢复脏标记，
            # 由下一次调度/terminate 兜底，避免静默丢失（0.8.8 复审修复）。
            self._pending = True
            raise
        except Exception as exc:
            self._pending = True
            logger.warning("[%s] debounced state save failed: %s", PLUGIN_ID, exc)
            self._ensure_retry()
            return False
        self.saved_count += 1
        return True

    def _ensure_retry(self) -> None:
        """失败后重新调度一次延迟 flush（防脏数据永不落盘）。"""
        current = asyncio.current_task()
        if self._task is not None and not self._task.done() and self._task is not current:
            self._task.cancel()
        self._task = asyncio.create_task(self._flush_later())

    def cancel(self) -> None:
        """取消未到期的延迟 flush（终止路径：落盘由调用方 flush 接管）。"""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
