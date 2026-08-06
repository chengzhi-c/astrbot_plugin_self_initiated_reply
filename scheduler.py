"""会话级调度器：延迟检查、巡检与图片/事件清理（自 main.py 拆分，ticket 02）。

只负责"什么时候该做什么"的时序逻辑；裁决、生成、发送等业务动作经注入回调
执行，因此可脱离主插件实例独立单测（注入设置、代次闸门与回调即可）。

状态容器（事件缓存、延迟任务表等）由插件持有并经引用共享：测试对
``plugin._delay_tasks`` 等属性的断言保持有效。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from astrbot.api import logger

from .image.parser import ImageParser
from .models import (
    EVENT_CLEANUP_INTERVAL_SEC,
    MAX_CACHED_EVENTS,
    PATROL_BACKOFF_DELAY_SEC,
    PLUGIN_ID,
    SessionState,
    Settings,
    now_ts,
)
from .utils import whitelist_storage_key


class CheckSessionCallback(Protocol):
    """会话检查回调（trigger/force/expected_generation 关键字调用）。"""

    def __call__(
        self, umo: str, *, trigger: str, force: bool, expected_generation: int | None
    ) -> Awaitable[str]: ...


class SessionScheduler:
    """每会话延迟检查、后台巡检与周期清理的时序实现。"""

    def __init__(
        self,
        *,
        settings: Settings,
        gate: Any,
        image_cache_dir: Path,
        spawn: Callable[[Any], asyncio.Task[Any] | None],
        should_run: Callable[[], bool],
        state_for: Callable[[str], SessionState],
        check_session: CheckSessionCallback,
        clear_cached_event: Callable[[str], None],
        last_events: dict[str, Any],
        last_event_at: dict[str, float],
        recent_image_events: dict[str, Any],
        whitelist_runtime_umos: dict[str, set[str]],
        delay_tasks: dict[str, asyncio.Task[Any]],
        running_check_tasks: dict[str, asyncio.Task[Any]],
    ) -> None:
        self.settings = settings
        self._gate = gate
        self._image_cache_dir = image_cache_dir
        self._spawn = spawn
        self._should_run = should_run
        self._state_for = state_for
        self._check_session = check_session
        self._clear_cached_event = clear_cached_event
        self._last_events = last_events
        self._last_event_at = last_event_at
        self._recent_image_events = recent_image_events
        self._whitelist_runtime_umos = whitelist_runtime_umos
        self._delay_tasks = delay_tasks
        self._running_check_tasks = running_check_tasks
        self._silence_events: dict[str, asyncio.Event] = {}
        self._patrol_task: asyncio.Task[Any] | None = None
        self._image_cleanup_task: asyncio.Task[Any] | None = None
        self._image_cleanup_lock = asyncio.Lock()
        self._last_cleanup = 0.0

    @property
    def patrol_task(self) -> asyncio.Task[Any] | None:
        return self._patrol_task

    @property
    def image_cleanup_task(self) -> asyncio.Task[Any] | None:
        return self._image_cleanup_task

    @property
    def last_cleanup_at(self) -> float:
        return self._last_cleanup

    @last_cleanup_at.setter
    def last_cleanup_at(self, value: float) -> None:
        self._last_cleanup = float(value)

    # ------------------------------------------------------------------
    # 延迟检查
    # ------------------------------------------------------------------

    def message_trigger_delay(self, trigger: str) -> int:
        min_silence = max(0, int(self.settings.min_silence_sec))
        if trigger == "reply_request":
            return min_silence
        return max(int(self.settings.message_delay_sec), min_silence)

    def remaining_silence_sec(self, state: SessionState) -> float:
        if not state.last_active_at:
            return 0.0
        silence_left = self.settings.min_silence_sec - (now_ts() - state.last_active_at)
        return max(0.0, silence_left)

    def schedule_delayed_check(
        self,
        umo: str,
        *,
        delay_sec: int | None,
        trigger: str,
        force: bool,
        generation: int | None = None,
    ) -> None:
        if generation is None:
            generation = self._gate.advance(umo)
        if not self._should_run() or not self._gate.is_current(umo, generation):
            logger.debug(
                "[%s] skip stale delayed-task registration session=%s generation=%s",
                PLUGIN_ID,
                umo,
                generation,
            )
            return
        self.cancel_delay(umo)
        task = self._spawn(
            self._delayed_check(
                umo,
                delay_sec=delay_sec,
                trigger=trigger,
                force=force,
                generation=generation,
            )
        )
        if task is None:
            return
        self._delay_tasks[umo] = task
        task.add_done_callback(
            lambda done: self._discard_delay_task(umo, done)  # type: ignore[arg-type]
        )

    def cancel_delay(self, umo: str, *, force: bool = False) -> None:
        task = self._delay_tasks.get(umo)
        running_task = self._running_check_tasks.get(umo)
        if not force and (self._gate.is_running(umo) or running_task is not None):
            # 新消息使运行中的检查失效，但不能取消其 await 链；旧任务会到达
            # 代次闸门后干净地抑制过期回复。取消会向装饰钩子注入
            # CancelledError（例如智能分段）。
            logger.debug(
                "[%s] leave running check alive for stale-generation suppression session=%s",
                PLUGIN_ID,
                umo,
            )
            return
        self._delay_tasks.pop(umo, None)
        if task and not task.done():
            task.cancel()
        if force and running_task and not running_task.done() and running_task is not task:
            running_task.cancel()

    def _discard_delay_task(self, umo: str, task: asyncio.Task[Any]) -> None:
        if self._delay_tasks.get(umo) is task:
            self._delay_tasks.pop(umo, None)

    def notify_activity(self, umo: str) -> None:
        """会话活动（新消息等）：置位当前静默事件，唤醒正在静默等待的延迟检查。

        等待者每次醒来都以实际会话状态复查（通知只是加速，状态才是权威）；
        事件被消费后从表内移除，下次等待重建——无通知时由超时兜底照常推进。
        """
        event = self._silence_events.pop(umo, None)
        if event is not None:
            event.set()

    async def _delayed_check(
        self,
        umo: str,
        *,
        delay_sec: int | None = None,
        trigger: str = "message_delay",
        force: bool = False,
        generation: int | None = None,
    ) -> None:
        try:
            delay = self.settings.message_delay_sec if delay_sec is None else max(0, delay_sec)
            if delay > 0:
                await asyncio.sleep(delay)
            if not self._should_run() or not self._gate.is_current(umo, generation):
                return
            state = self._state_for(whitelist_storage_key(umo, self.settings.whitelist))
            silence_left = self.remaining_silence_sec(state)
            silence_event: asyncio.Event | None = None
            while not force and silence_left > 0:
                logger.debug(
                    "[%s] wait for minimum silence session=%s trigger=%s remaining=%.2fs",
                    PLUGIN_ID,
                    umo,
                    trigger,
                    silence_left,
                )
                silence_event = self._silence_events.setdefault(umo, asyncio.Event())
                try:
                    # 事件化等待：新消息到达（notify_activity）立即醒来复查；
                    # 超时兜底保留原 sleep 语义，保证无通知时照常推进。
                    await asyncio.wait_for(silence_event.wait(), timeout=silence_left + 0.1)
                except asyncio.TimeoutError:
                    pass
                if not self._should_run() or not self._gate.is_current(umo, generation):
                    return
                silence_left = self.remaining_silence_sec(state)
            while self._gate.is_running(umo):
                logger.debug(
                    "[%s] wait for previous check to finish session=%s trigger=%s",
                    PLUGIN_ID,
                    umo,
                    trigger,
                )
                await self._gate.release_event(umo).wait()
                if not self._should_run() or not self._gate.is_current(umo, generation):
                    return
            running_task = asyncio.current_task()
            if running_task is not None:
                self._running_check_tasks[umo] = running_task
            try:
                result = await self._check_session(
                    umo,
                    trigger=trigger,
                    force=force,
                    expected_generation=generation,
                )
            finally:
                if running_task is not None and self._running_check_tasks.get(umo) is running_task:
                    self._running_check_tasks.pop(umo, None)
            logger.debug(
                "[%s] check result session=%s trigger=%s result=%s", PLUGIN_ID, umo, trigger, result
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("[%s] delayed check failed session=%s error=%s", PLUGIN_ID, umo, exc)
        finally:
            # 回收本任务创建的静默事件（仅当表中仍是它时——取消/新任务
            # 交错时新任务可能已 setdefault 重建，误删会导致其通知丢失）。
            if silence_event is not None and self._silence_events.get(umo) is silence_event:
                self._silence_events.pop(umo, None)

    # ------------------------------------------------------------------
    # 图片与事件清理
    # ------------------------------------------------------------------

    def cleanup_image_sources(self, *, now: float | None = None) -> int:
        """清理过期图片索引和插件临时缓存，保护仍在有效窗口内的源。"""
        current = now_ts() if now is None else float(now)
        image_age = max(60.0, float(self.settings.vision_image_age_sec))
        cutoff = current - image_age
        for umo, events in list(self._recent_image_events.items()):
            while events and events[0][0] < cutoff:
                events.popleft()
            if not events:
                self._recent_image_events.pop(umo, None)

        protected_sources = {
            image.prepared_source
            for events in self._recent_image_events.values()
            for _, images in events
            for image in images
            if image.prepared_source
        }
        removed_images = ImageParser.cleanup_source_cache(
            self._image_cache_dir,
            protected_sources=protected_sources,
            max_age_sec=image_age,
            now=current,
        )
        if removed_images:
            logger.info(
                "[%s] cleaned up %d expired frozen images",
                PLUGIN_ID,
                removed_images,
            )
        return removed_images

    async def run_image_cleanup(self) -> int:
        """Serialize manual and periodic cleanup requests."""
        async with self._image_cleanup_lock:
            return self.cleanup_image_sources(now=now_ts())

    def cleanup_events_if_needed(self) -> None:
        """定期清理没有任务或运行中的陈旧事件。"""
        now = now_ts()
        if now - self._last_cleanup < EVENT_CLEANUP_INTERVAL_SEC:
            return
        self._last_cleanup = now

        live_sessions = set(self._gate.running_sessions_view)
        live_sessions.update(
            umo for umo, task in self._delay_tasks.items() if task and not task.done()
        )
        removable = sorted(
            (
                self._last_event_at.get(umo, 0.0),
                umo,
            )
            for umo in self._last_events
            if umo not in live_sessions
        )
        stale = [item for item in removable if now - item[0] >= EVENT_CLEANUP_INTERVAL_SEC]
        for _, umo in stale:
            self._clear_cached_event(umo)

        self.cleanup_image_sources(now=now)

        if len(self._last_events) > MAX_CACHED_EVENTS:
            excess = len(self._last_events) - MAX_CACHED_EVENTS
            removed = 0
            for _, umo in removable:
                if removed >= excess:
                    break
                if umo in self._last_events and umo not in live_sessions:
                    self._clear_cached_event(umo)
                    removed += 1
            if removed:
                logger.info(
                    "[%s] cleaned up %d cached events (total: %d)",
                    PLUGIN_ID,
                    removed,
                    len(self._last_events),
                )

        # 回收长期无活动的运行时 UMO 映射，避免对白名单内会话只增不减
        # （巡检对无事件会话会自然跳过，移除安全）。
        active_umos = set(self._gate.running_sessions_view)
        active_umos.update(
            umo for umo, at in self._last_event_at.items() if now - at < EVENT_CLEANUP_INTERVAL_SEC
        )
        active_umos.update(
            umo for umo, task in self._delay_tasks.items() if task and not task.done()
        )
        for key, values in list(self._whitelist_runtime_umos.items()):
            kept = values & active_umos
            if kept:
                self._whitelist_runtime_umos[key] = kept
            else:
                self._whitelist_runtime_umos.pop(key, None)

    def ensure_image_cleanup(self) -> None:
        if not self._should_run():
            return
        if self._image_cleanup_task is None or self._image_cleanup_task.done():
            self._image_cleanup_task = self._spawn(self._image_cleanup_loop())

    async def _image_cleanup_loop(self) -> None:
        while self._should_run():
            try:
                image_age = max(60.0, float(self.settings.vision_image_age_sec))
                await asyncio.sleep(min(3600.0, max(60.0, image_age / 2.0)))
                if not self._should_run():
                    return
                await self.run_image_cleanup()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[%s] image cleanup loop failed: %s", PLUGIN_ID, exc)
                await asyncio.sleep(60.0)

    # ------------------------------------------------------------------
    # 巡检
    # ------------------------------------------------------------------

    def _runtime_umos_for_whitelist_item(self, item: str) -> set[str]:
        value = str(item or "").strip()
        if ":" in value:
            return {value}
        return set(self._whitelist_runtime_umos.get(value, set()))

    def ensure_patrol(self) -> None:
        if not self.settings.enabled_patrol_trigger or not self._should_run():
            return
        if self._patrol_task is None or self._patrol_task.done():
            self._patrol_task = self._spawn(self._patrol_loop())

    async def _patrol_loop(self) -> None:
        while self._should_run() and self.settings.enabled_patrol_trigger:
            try:
                await asyncio.sleep(self.settings.check_interval_sec)
                now = now_ts()
                self.cleanup_events_if_needed()
                seen_patrol_umos: set[str] = set()
                for item in list(self.settings.whitelist):
                    for umo in self._runtime_umos_for_whitelist_item(item):
                        if umo in seen_patrol_umos:
                            continue
                        seen_patrol_umos.add(umo)
                        try:
                            if not self._last_events.get(umo):
                                continue
                            state = self._state_for(
                                whitelist_storage_key(umo, self.settings.whitelist)
                            )
                            if self.settings.patrol_inactive_after_sec and (
                                not state.last_active_at
                                or now - state.last_active_at
                                > self.settings.patrol_inactive_after_sec
                            ):
                                continue
                            if self._gate.is_running(umo):
                                continue
                            generation = self._gate.generation_view.get(umo, 0)
                            result = await self._check_session(
                                umo,
                                trigger="patrol",
                                force=False,
                                expected_generation=generation,
                            )
                            logger.debug(
                                "[%s] patrol result session=%s result=%s",
                                PLUGIN_ID,
                                umo,
                                result,
                            )
                        except Exception as exc:
                            logger.warning(
                                "[%s] patrol session failed session=%s error=%s",
                                PLUGIN_ID,
                                umo,
                                exc,
                                exc_info=True,
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[%s] patrol loop failed error=%s", PLUGIN_ID, exc, exc_info=True)
                # 添加退避延迟，避免错误循环
                await asyncio.sleep(min(PATROL_BACKOFF_DELAY_SEC, self.settings.check_interval_sec))

    async def stop_patrol(self) -> None:
        task = self._patrol_task
        self._patrol_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
