"""会话级调度器：延迟检查、巡检与图片/事件清理。

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
    LEAK_WARN_SESSION_THRESHOLD,
    LEAK_WARN_TASK_THRESHOLD,
    MAX_CACHED_EVENTS,
    MAX_RELEASE_WAIT_ROUNDS,
    PATROL_BACKOFF_DELAY_SEC,
    PLUGIN_ID,
    RELEASE_WAIT_TIMEOUT_SEC,
    CheckTrigger,
    SessionState,
    Settings,
    now_ts,
)
from .utils import session_is_private, whitelist_storage_key


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
        clear_event: Callable[[str, float], None],
        drop_older_images: Callable[[float], None],
        last_events: dict[str, Any],
        last_event_at: dict[str, float],
        recent_image_events: dict[str, Any],
        whitelist_runtime_umos: dict[str, set[str]],
        delay_tasks: dict[str, asyncio.Task[Any]],
        running_check_tasks: dict[str, asyncio.Task[Any]],
        background_tasks: set[asyncio.Task[Any]],
    ) -> None:
        self.settings = settings
        self._gate = gate
        self._image_cache_dir = image_cache_dir
        self._spawn = spawn
        self._should_run = should_run
        self._state_for = state_for
        self._check_session = check_session
        self._clear_event = clear_event
        self._drop_older_images = drop_older_images
        self._last_events = last_events
        self._last_event_at = last_event_at
        self._recent_image_events = recent_image_events
        self._whitelist_runtime_umos = whitelist_runtime_umos
        self._delay_tasks = delay_tasks
        self._running_check_tasks = running_check_tasks
        self._background_tasks = background_tasks
        self._silence_events: dict[str, asyncio.Event] = {}
        self._leak_warned: set[str] = set()
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
        if trigger == CheckTrigger.REPLY_REQUEST:
            return min_silence
        return max(int(self.settings.message_delay_sec), min_silence)

    def remaining_silence_sec(self, state: SessionState) -> float:
        return state.remaining_silence_sec(self.settings.min_silence_sec, now_ts())

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
            self.delayed_check(
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

    async def _wait_initial_delay_and_validate(
        self, umo: str, delay_sec: int | None, generation: int | None
    ) -> bool:
        delay = self.settings.message_delay_sec if delay_sec is None else max(0, delay_sec)
        if delay > 0:
            await asyncio.sleep(delay)
        return self._should_run() and self._gate.is_current(umo, generation)

    async def _wait_for_minimum_silence(
        self,
        umo: str,
        *,
        trigger: str,
        force: bool,
        generation: int | None,
    ) -> bool:
        state = self._state_for(whitelist_storage_key(umo))
        silence_left = self.remaining_silence_sec(state)
        silence_event: asyncio.Event | None = None
        try:
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
                    await asyncio.wait_for(silence_event.wait(), timeout=silence_left + 0.1)
                except TimeoutError:
                    pass
                if not self._should_run() or not self._gate.is_current(umo, generation):
                    return False
                silence_left = self.remaining_silence_sec(state)
            return True
        finally:
            if silence_event is not None and self._silence_events.get(umo) is silence_event:
                self._silence_events.pop(umo, None)

    async def _wait_for_previous_check_release(
        self, umo: str, trigger: str, generation: int | None
    ) -> bool:
        release_rounds = 0
        while self._gate.is_running(umo):
            logger.debug(
                "[%s] wait for previous check to finish session=%s trigger=%s",
                PLUGIN_ID,
                umo,
                trigger,
            )
            try:
                await asyncio.wait_for(
                    self._gate.release_event(umo).wait(), timeout=RELEASE_WAIT_TIMEOUT_SEC
                )
            except TimeoutError:
                pass
            if not self._should_run() or not self._gate.is_current(umo, generation):
                return False
            release_rounds += 1
            if release_rounds >= MAX_RELEASE_WAIT_ROUNDS:
                logger.warning(
                    "[%s] release gate desynced, drop check session=%s trigger=%s rounds=%d",
                    PLUGIN_ID,
                    umo,
                    trigger,
                    release_rounds,
                )
                return False
        return True

    async def _run_registered_check(
        self, umo: str, *, trigger: str, force: bool, generation: int | None
    ) -> str:
        running_task = asyncio.current_task()
        if running_task is not None:
            self._running_check_tasks[umo] = running_task
        try:
            return await self._check_session(
                umo,
                trigger=trigger,
                force=force,
                expected_generation=generation,
            )
        finally:
            if running_task is not None and self._running_check_tasks.get(umo) is running_task:
                self._running_check_tasks.pop(umo, None)

    async def delayed_check(
        self,
        umo: str,
        *,
        delay_sec: int | None = None,
        trigger: str = CheckTrigger.MESSAGE_DELAY,
        force: bool = False,
        generation: int | None = None,
    ) -> None:
        """延迟后对会话跑一次检查，穿过三道等待闸门才真正执行。

        闸门顺序：``delay_sec`` 睡眠 → 最小静默期（事件化等待，新消息到达即醒来
        复查）→ 同会话上一次检查完成。每道闸门后都重验 ``_should_run`` 与代次，
        任一失效即放弃——白名单移除或会话重加（ABA）后的旧任务不得复活发送。
        ``force=True`` 跳过静默期，但不跳过代次校验与运行互斥。

        失败时：``CancelledError`` 静默返回（停止/失效路径的正常收敛）；其余异常
        记 warning 后吞掉，不向调用方冒泡——它由 ``asyncio.Task`` 驱动，抛出只会
        变成无人接管的任务异常。静默等待步骤的 ``finally`` 必定回收本任务创建的
        事件，且仅在表中仍是自己时才删（交错重建时误删会让新任务丢失通知）。
        """
        try:
            if not await self._wait_initial_delay_and_validate(umo, delay_sec, generation):
                return
            if not await self._wait_for_minimum_silence(
                umo, trigger=trigger, force=force, generation=generation
            ):
                return
            if not await self._wait_for_previous_check_release(umo, trigger, generation):
                return
            result = await self._run_registered_check(
                umo, trigger=trigger, force=force, generation=generation
            )
            logger.info(
                "[%s] check result session=%s trigger=%s result=%s", PLUGIN_ID, umo, trigger, result
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("[%s] delayed check failed session=%s error=%s", PLUGIN_ID, umo, exc)

    # ------------------------------------------------------------------
    # 图片与事件清理
    # ------------------------------------------------------------------

    def _prune_image_index(self, current: float) -> tuple[float, set[str]]:
        """回收过期图片索引（纯内存，无磁盘 IO），返回 (保留窗口, 受保护源)。"""
        image_age = max(60.0, float(self.settings.vision_image_age_sec))
        self._drop_older_images(current - image_age)

        protected_sources = {
            image.prepared_source
            for events in self._recent_image_events.values()
            for _, images in events
            for image in images
            if image.prepared_source
        }
        return image_age, protected_sources

    @staticmethod
    def _log_removed_images(removed_images: int) -> int:
        if removed_images:
            logger.info(
                "[%s] cleaned up %d expired frozen images",
                PLUGIN_ID,
                removed_images,
            )
        return removed_images

    def cleanup_image_sources(self, *, now: float | None = None) -> int:
        """同步清理过期图片索引与磁盘缓存。

        仅用于无事件循环的场景（插件构造期的启动清理）。协程内必须改用
        ``run_image_cleanup``，否则磁盘遍历（rglob + 全量 stat）会阻塞事件循环。
        """
        current = now_ts() if now is None else float(now)
        image_age, protected_sources = self._prune_image_index(current)
        return self._log_removed_images(
            ImageParser.cleanup_source_cache(
                self._image_cache_dir,
                protected_sources=protected_sources,
                max_age_sec=image_age,
                now=current,
            )
        )

    async def run_image_cleanup(self) -> int:
        """Serialize manual and periodic cleanup requests.

        索引回收留在事件循环内（纯内存，且须与事件表保持同一时刻视图）；
        磁盘遍历交由线程执行，避免阻塞事件循环。锁在此处是必需的：
        to_thread 引入了真实 await 间隙，两次清理可能并发 unlink 同一文件。
        """
        async with self._image_cleanup_lock:
            current = now_ts()
            image_age, protected_sources = self._prune_image_index(current)
            removed_images = await asyncio.to_thread(
                ImageParser.cleanup_source_cache,
                self._image_cache_dir,
                protected_sources=protected_sources,
                max_age_sec=image_age,
                now=current,
            )
            return self._log_removed_images(removed_images)

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
        for active_at, umo in stale:
            self._clear_event(umo, active_at)

        # 只做纯内存的图片索引回收：本方法由 on_message（协程）同步调用，
        # 磁盘遍历会阻塞消息热路径。磁盘侧由 _image_cleanup_loop 经
        # run_image_cleanup 独立承担，其周期（image_age/2，上限 1h）严于
        # 本方法的 1h 节流，故回收不会延后。
        # 此处不得调用 ensure_image_cleanup：它经 create_task 起循环，而本
        # 方法在无事件循环的同步上下文也会被调用（实测 RuntimeError）。
        self._prune_image_index(now)

        if len(self._last_events) > MAX_CACHED_EVENTS:
            excess = len(self._last_events) - MAX_CACHED_EVENTS
            removed = 0
            for active_at, umo in removable:
                if removed >= excess:
                    break
                if umo in self._last_events and umo not in live_sessions:
                    self._clear_event(umo, active_at)
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

        self._warn_leaks_if_needed()

    def _warn_leaks_if_needed(self) -> None:
        """任务表/代次表规模超阈值告警（运维状态，低频）；回落前不重复。"""
        task_count = (
            len(self._delay_tasks) + len(self._running_check_tasks) + len(self._background_tasks)
        )
        if task_count >= LEAK_WARN_TASK_THRESHOLD:
            if "task" not in self._leak_warned:
                self._leak_warned.add("task")
                logger.warning(
                    "[%s] background task count above threshold tasks=%d delay=%d"
                    " running_check=%d background=%d (leak suspected)",
                    PLUGIN_ID,
                    task_count,
                    len(self._delay_tasks),
                    len(self._running_check_tasks),
                    len(self._background_tasks),
                )
        else:
            self._leak_warned.discard("task")
        session_count = len(self._gate.generation_view)
        if session_count >= LEAK_WARN_SESSION_THRESHOLD:
            if "session" not in self._leak_warned:
                self._leak_warned.add("session")
                logger.warning(
                    "[%s] session generation table above threshold sessions=%d (leak suspected)",
                    PLUGIN_ID,
                    session_count,
                )
        else:
            self._leak_warned.discard("session")

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
        """巡检后台循环：按 ``check_interval_sec`` 轮询白名单会话并尝试主动接话。

        每轮先做事件缓存清理，再遍历白名单条目展开的运行期 UMO（``seen_patrol_umos``
        去重，防同一 UMO 被多个白名单条目重复检查）。逐会话跳过：无缓存事件、
        超过 ``patrol_inactive_after_sec`` 未活动、已有检查在运行。

        失败时分三层，保证循环不死：单会话异常 → warning 后继续下一个会话；
        整轮异常 → warning 后退避 ``min(PATROL_BACKOFF_DELAY_SEC, check_interval_sec)``
        再继续（避免异常态高频空转）；``CancelledError`` 向上抛出，terminate
        才能真正停掉本任务。
        """
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
                        await self._patrol_one_session(umo, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[%s] patrol loop failed error=%s", PLUGIN_ID, exc, exc_info=True)
                # 添加退避延迟，避免错误循环
                await asyncio.sleep(min(PATROL_BACKOFF_DELAY_SEC, self.settings.check_interval_sec))

    async def _patrol_one_session(self, umo: str, now: float) -> None:
        """巡检单个会话：跳过条件 → 触发检查，单会话异常隔离不中断整轮。"""
        try:
            if not self.settings.enabled_private_sessions and session_is_private(umo):
                return
            if not self._last_events.get(umo):
                return
            state = self._state_for(whitelist_storage_key(umo))
            if self.settings.patrol_inactive_after_sec and (
                not state.last_active_at
                or now - state.last_active_at > self.settings.patrol_inactive_after_sec
            ):
                return
            if self._gate.is_running(umo):
                return
            generation = self._gate.generation_view.get(umo, 0)
            result = await self._check_session(
                umo,
                trigger=CheckTrigger.PATROL,
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

    async def stop_patrol(self) -> None:
        task = self._patrol_task
        self._patrol_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
