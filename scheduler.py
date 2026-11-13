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
    MAX_PATROL_INACTIVE_SEC,
    MAX_RELEASE_WAIT_ROUNDS,
    MIN_VISION_IMAGE_AGE_SEC,
    PATROL_BACKOFF_DELAY_SEC,
    PATROL_STOP_TIMEOUT_SEC,
    PLUGIN_ID,
    RELEASE_WAIT_TIMEOUT_SEC,
    CheckTrigger,
    SessionState,
    Settings,
    now_ts,
)
from .session_gate import SessionGate
from .utils import is_full_umo, redact_exc_text, session_is_private


class CheckSessionCallback(Protocol):
    """会话检查回调（trigger/force/expected_generation 关键字调用）。"""

    def __call__(
        self, umo: str, *, trigger: str, force: bool, expected_generation: int | None
    ) -> Awaitable[str]: ...


def _log_removed_images(removed_images: int) -> int:
    """两条清理路径共用的收尾：有清理数才记 INFO，原样透传数量。"""
    if removed_images:
        logger.info(
            "[%s] cleaned up %d expired frozen images",
            PLUGIN_ID,
            removed_images,
        )
    return removed_images


class SessionScheduler:
    """每会话延迟检查、后台巡检与周期清理的时序实现。"""

    def __init__(
        self,
        *,
        settings: Settings,
        gate: SessionGate,
        image_cache_dir: Path,
        spawn: Callable[[Any], asyncio.Task[Any] | None],
        should_run: Callable[[], bool],
        state_for: Callable[[str], SessionState],
        check_session: CheckSessionCallback,
        clear_event: Callable[[str, float], None],
        drop_older_images: Callable[[float], None],
        last_events: dict[str, Any],
        last_event_at: dict[str, float],
        last_decisions: dict[str, Any],
        recent_image_events: dict[str, Any],
        whitelist_runtime_umos: dict[str, set[str]],
        delay_tasks: dict[str, asyncio.Task[Any]],
        running_check_tasks: dict[str, asyncio.Task[Any]],
        background_tasks: set[asyncio.Task[Any]],
        quarantine_task: Callable[[asyncio.Task[Any], str], None] | None = None,
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
        # 内部沿用各自的私有属性名：既有调用点与守卫（CONTAINER_HOLDERS
        # 按属性名枚举）不必跟着改。
        self._last_events = last_events
        self._last_event_at = last_event_at
        self._last_decisions = last_decisions
        self._recent_image_events = recent_image_events
        self._whitelist_runtime_umos = whitelist_runtime_umos
        self._delay_tasks = delay_tasks
        self._running_check_tasks = running_check_tasks
        self._background_tasks = background_tasks
        self._quarantine_task = quarantine_task
        self._silence_events: dict[str, asyncio.Event] = {}
        self._leak_warned: set[str] = set()
        self._patrol_task: asyncio.Task[Any] | None = None
        self._image_cleanup_task: asyncio.Task[Any] | None = None
        self._image_cleanup_lock = asyncio.Lock()
        # 节流起点取构造时刻：0.0 会让第一条消息就触发一整轮事件/图片索引回收，
        # 而构造即回收既无意义又把遍历成本压进消息热路径。
        self._last_cleanup = now_ts()

    @property
    def patrol_task(self) -> asyncio.Task[Any] | None:
        return self._patrol_task

    @property
    def image_cleanup_task(self) -> asyncio.Task[Any] | None:
        return self._image_cleanup_task

    # ------------------------------------------------------------------
    # 延迟检查
    # ------------------------------------------------------------------

    def message_trigger_delay(self) -> int:
        # 触发源只剩消息静默一种：延迟不得低于静默门槛（短于静默期的检查
        # 只会反复撞「静默不足」闸门空转）。
        min_silence = max(0, int(self.settings.min_silence_sec))
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
        task.add_done_callback(lambda done: self._discard_delay_task(umo, done))

    def cancel_delay(self, umo: str, *, force: bool = False) -> None:
        def _cancel_once(pending: asyncio.Task[Any]) -> None:
            """取消一个未结束的任务；已在停止路径上的不再补第二次。

            第二次取消会落进被取消方**自己的**有界收敛等待——``generation``
            的 ``_graceful_stop`` 与 ``decision`` 的 ``_converge_provider_task``
            都把「宽限窗口内又收到取消」判成"run task 吞掉了取消"并隔离登记，
            于是 ``_mark_degraded`` 把插件永久打成 DEGRADED：``/selfreply on``
            与 force check 全被拒，只能重载插件。``/selfreply off`` 正是这个
            形状——先经 ``_cancel_event_session`` 取消一次，落盘让出事件循环后
            ``apply_enabled_topology`` 再取消一次，而真实 LLM 调用完全落在那个
            让出窗口里。

            跳过是安全的：被取消方自带「宽限 → 兜底取消 → 再宽限 → 隔离」的
            有界收敛，不需要外部补第二次取消；``cancelling()>0`` 即"取消已
            投递且它还没uncancel"，本仓库从不调用 ``uncancel()``。
            """
            if not pending.cancelling():
                pending.cancel()

        task = self._delay_tasks.get(umo)
        running_task = self._running_check_tasks.get(umo)
        keep_running = (
            not force
            and running_task is not None
            and not running_task.done()
            and task is running_task
        )
        if keep_running:
            # 运行中的检查不能被新消息取消：await 链还要走到代次闸门。
            # 表里若是后续排队的 delayed_check，必须让位，否则每条新消息都会叠一次判断。
            logger.debug(
                "[%s] leave running check alive for stale-generation suppression session=%s",
                PLUGIN_ID,
                umo,
            )
            return
        self._delay_tasks.pop(umo, None)
        self._silence_events.pop(umo, None)
        if task and not task.done() and (force or task is not running_task):
            _cancel_once(task)
        if force and running_task and not running_task.done() and running_task is not task:
            _cancel_once(running_task)

    def _discard_delay_task(self, umo: str, task: asyncio.Future[Any]) -> None:
        """``add_done_callback`` 回调：形参收 ``Future``，写窄成 ``Task`` 只能靠
        ignore 绕过类型检查。"""
        if self._delay_tasks.get(umo) is task:
            self._delay_tasks.pop(umo, None)

    def notify_activity(self, umo: str) -> None:
        """会话活动（新消息等）：置位当前静默事件，唤醒正在静默等待的延迟检查。

        等待者每次醒来都以实际会话状态复查（通知只是加速，状态才是权威）。
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
        state = self._state_for(umo)
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
                    # 静默等待只多等 0.1s：刚好覆盖事件循环调度抖动，不改变静默语义。
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
        released = await self._gate.await_release(
            umo,
            trigger=trigger,
            timeout_sec=RELEASE_WAIT_TIMEOUT_SEC,
            max_rounds=MAX_RELEASE_WAIT_ROUNDS,
            # 每轮等待后重验运行资格与代次：任一失效即放弃，白名单移除后
            # 不得再跑一次检查。
            on_round_expired=lambda: self._should_run() and self._gate.is_current(umo, generation),
        )
        # 轮数耗尽才告警：调用方主动放弃（插件停用/代次已变）是正常路径且
        # 原 release 等待实现静默返回，而 release 事件迟迟不来意味着门与
        # 运行集脱同步，必须留痕。以「仍具备运行资格」区分两种归因。
        if not released and self._should_run() and self._gate.is_current(umo, generation):
            logger.warning(
                "[%s] release gate desynced, drop check session=%s trigger=%s rounds=%d",
                PLUGIN_ID,
                umo,
                trigger,
                MAX_RELEASE_WAIT_ROUNDS,
            )
        return released

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
        """延迟后对会话跑一次检查。

        每道等待闸门后都重验 ``_should_run`` 与代次，任一失效即放弃，白名单移除
        或会话重加（ABA）后的旧任务不得复活发送。``force=True`` 跳过静默期，但
        不跳过代次校验与运行互斥。

        失败时：``CancelledError`` 静默返回（停止/失效路径的正常收敛）；其余异常
        记 warning 后吞掉（本任务由 ``asyncio.Task`` 驱动，抛出只会变成无人接管的
        任务异常）。静默等待步骤的 ``finally`` 仅在表中仍是自己时才删事件
        （交错重建时误删会让新任务丢失通知）。
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
            # 与巡检侧的同名日志（_patrol_one_session）同级：两者包的是同一次检查、
            # 打的是同一种结果串，级别必须一致，否则同一信息在一条路径上可见、
            # 另一条上不可见。取 DEBUG：result 绝大多数是跳过理由（冷却中/观察窗
            # 已推进/静默不足），属高频成功路径；要诊断"为什么不接话"看的是
            # session_pipeline 的 decision 行（INFO，含 should_reply 与理由），
            # README 排障步骤引用的就是那一行。
            logger.debug(
                "[%s] check result session=%s trigger=%s result=%s", PLUGIN_ID, umo, trigger, result
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # 不开 exc_info：traceback 末尾是 str(exc) 原文，会把 redact_exc_text
            # 刚脱掉的凭证原样带回同一条日志（provider SDK 的异常串标准形态就带
            # 签名 URL）。口径同 delivery/generation 的失败分支。
            logger.warning(
                "[%s] delayed check failed session=%s error=%s",
                PLUGIN_ID,
                umo,
                redact_exc_text(exc),
            )

    # ------------------------------------------------------------------
    # 图片与事件清理
    # ------------------------------------------------------------------

    def _image_age_sec(self) -> float:
        """图片保留窗口；规格表已按同一下限夹取，这里兜住直写 ``settings`` 的路径。"""
        return max(float(MIN_VISION_IMAGE_AGE_SEC), float(self.settings.vision_image_age_sec))

    def _expire_image_index(self, current: float) -> float:
        """回收过期图片索引（纯内存，无磁盘 IO），返回保留窗口。"""
        image_age = self._image_age_sec()
        self._drop_older_images(current - image_age)
        return image_age

    def _protected_image_sources(self) -> set[str]:
        """仍在索引里的图片源：磁盘清理不得删掉它们。"""
        return {
            image.prepared_source
            for events in self._recent_image_events.values()
            for _, images in events
            for image in images
            if image.prepared_source
        }

    def _prune_image_index(self, current: float) -> tuple[float, set[str]]:
        """过期回收 + 受保护源，供要清磁盘的两条路径一次取全。"""
        image_age = self._expire_image_index(current)
        return image_age, self._protected_image_sources()

    def cleanup_image_sources(self, *, now: float | None = None) -> int:
        """同步清理过期图片索引与磁盘缓存。

        仅用于无事件循环的场景（插件构造期的启动清理）。协程内必须改用
        ``run_image_cleanup``，否则磁盘遍历（rglob + 全量 stat）会阻塞事件循环。
        """
        current = now_ts() if now is None else float(now)
        image_age, protected_sources = self._prune_image_index(current)
        return _log_removed_images(
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
        磁盘遍历交由线程执行。锁是必需的：to_thread 引入了真实 await 间隙，
        两次清理可能并发 unlink 同一文件。
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
            return _log_removed_images(removed_images)

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
        # 陈旧阈值必须覆盖巡检的不活跃窗口：事件被回收后 _patrol_one_session 的
        # 「没有可用的最近消息事件」判据会让该会话永久跳过，patrol_inactive_after_sec
        # 配到 1h 以上的部分随之完全失效（上限 604800）。条数仍由
        # MAX_CACHED_EVENTS 兜底，放宽时间窗不会无界增长。
        # 0 的语义是"不截断"（面板如此承诺），直接进 max() 会被抬成 1 小时：
        # 事件先被回收，_patrol_one_session 随后因"没有可用的最近消息事件"永久
        # 跳过该会话。折算成规格上限仍是有限窗口，条数另由 MAX_CACHED_EVENTS 兜底。
        patrol_window = self.settings.patrol_inactive_after_sec or MAX_PATROL_INACTIVE_SEC
        stale_cutoff = max(EVENT_CLEANUP_INTERVAL_SEC, patrol_window)
        stale = [item for item in removable if now - item[0] >= stale_cutoff]
        for active_at, umo in stale:
            self._clear_event(umo, active_at)
            # 裁决条目与事件缓存同生命周期：不同步回收，静默会话的条目只增不减
            # （上限只是白名单规模 1000），并被 GET /status 整表回显。
            self._last_decisions.pop(umo, None)

        # 只做纯内存的图片索引回收：本方法由 on_message（协程）同步调用，
        # 磁盘遍历会阻塞消息热路径，磁盘侧由 _image_cleanup_loop 独立承担
        # （其周期严于本方法的 1h 节流，回收不会延后）。
        # 此处不得调用 ensure_image_cleanup：它经 create_task 起循环，而本
        # 方法在无事件循环的同步上下文也会被调用。
        # 也不建受保护源集合：那要遍历全部会话的全部图片（≤1000×20×5），
        # 而本路径只做内存过期，集合建了没人用。
        self._expire_image_index(now)

        if len(self._last_events) > MAX_CACHED_EVENTS:
            excess = len(self._last_events) - MAX_CACHED_EVENTS
            removed = 0
            for active_at, umo in removable:
                if removed >= excess:
                    break
                if umo in self._last_events and umo not in live_sessions:
                    self._clear_event(umo, active_at)
                    # 溢出通道也必须同步：被这里摘掉的会话此后不再出现在
                    # removable 里，只靠上面的陈旧通道就永远回收不到它的裁决。
                    self._last_decisions.pop(umo, None)
                    removed += 1
            if removed:
                logger.info(
                    "[%s] cleaned up %d cached events (total: %d)",
                    PLUGIN_ID,
                    removed,
                    len(self._last_events),
                )

        # 回收长期无活动的运行时 UMO 映射，避免对白名单内会话只增不减。
        # 起点复用 live_sessions：本方法是同步的（无 await），中间只调
        # _clear_event 与 _expire_image_index，都不碰运行中会话与延迟任务表。
        # 活跃窗口用与事件回收同一条 stale_cutoff：裸群号条目唯一的 UMO 来源就是
        # 本表，按 1h 摘掉而事件还在（patrol_inactive_after_sec 可到 7 天）会让
        # 该群在巡检里"没有可用的最近消息事件"式地静默消失。
        active_umos = set(live_sessions)
        active_umos.update(
            umo for umo, at in self._last_event_at.items() if now - at < stale_cutoff
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
        # ``cancelling()`` 也要重启：取消已交付但任务尚未收敛时它既不是 None
        # 也不是 done，旧判据会跳过重启——于是 runtime_enabled=True 而清理
        # 循环已经没了（webapi 回滚路径上「取消后不 await 直接 ensure」恰好
        # 命中这个窗口）。
        task = self._image_cleanup_task
        if task is None or task.done() or task.cancelling():
            self._image_cleanup_task = self._spawn(self._image_cleanup_loop())

    async def _image_cleanup_loop(self) -> None:
        while self._should_run():
            try:
                image_age = self._image_age_sec()
                # 清理周期取图片保留窗口的一半，夹在 [MIN_VISION_IMAGE_AGE_SEC,
                # EVENT_CLEANUP_INTERVAL_SEC]：磁盘上限可控，又不至于频繁 rglob。
                await asyncio.sleep(
                    min(
                        float(EVENT_CLEANUP_INTERVAL_SEC),
                        max(float(MIN_VISION_IMAGE_AGE_SEC), image_age / 2.0),
                    )
                )
                if not self._should_run():
                    return
                await self.run_image_cleanup()
            except Exception as exc:
                logger.warning(
                    "[%s] image cleanup loop failed: %s", PLUGIN_ID, redact_exc_text(exc)
                )
                # 清理失败后按巡检退避同量级重试，不空转也不久拖。
                await asyncio.sleep(float(PATROL_BACKOFF_DELAY_SEC))

    # ------------------------------------------------------------------
    # 巡检
    # ------------------------------------------------------------------

    def _runtime_umos_for_whitelist_item(self, item: str) -> set[str]:
        value = str(item or "").strip()
        if is_full_umo(value):
            return {value}
        return set(self._whitelist_runtime_umos.get(value, set()))

    def ensure_patrol(self) -> None:
        if not self.settings.enabled_patrol_trigger or not self._should_run():
            return
        # 同 ensure_image_cleanup：取消在途（尚未 done）也要能重启。
        task = self._patrol_task
        if task is None or task.done() or task.cancelling():
            self._patrol_task = self._spawn(self._patrol_loop())

    async def _patrol_loop(self) -> None:
        """巡检后台循环：按 ``check_interval_sec`` 轮询白名单会话并尝试主动接话。

        每轮先做事件缓存清理，再遍历白名单条目展开的运行期 UMO（去重）。
        失败分三层保证循环不死：单会话异常继续下一个；整轮异常退避后继续；
        ``CancelledError`` 向上抛出，terminate 才能真正停掉本任务。
        """
        while self._should_run() and self.settings.enabled_patrol_trigger:
            try:
                await asyncio.sleep(self.settings.check_interval_sec)
                # 醒来后必须复核开关：关掉巡检不会取消在途睡眠（ensure_patrol
                # 在关闭方向直接 return），不复核就会在最长 check_interval_sec
                # 之后仍跑完整一轮主动回复。同 _image_cleanup_loop 的醒来复核。
                if not self._should_run() or not self.settings.enabled_patrol_trigger:
                    return
                self.cleanup_events_if_needed()
                seen_patrol_umos: set[str] = set()
                for item in list(self.settings.whitelist):
                    for umo in self._runtime_umos_for_whitelist_item(item):
                        if umo in seen_patrol_umos:
                            continue
                        seen_patrol_umos.add(umo)
                        # now 逐会话取：单个会话可能阻塞近一个 generation_timeout_sec，
                        # 复用轮次起点会让后续会话的活跃窗口判定停在数小时前。
                        await self._patrol_one_session(umo, now_ts())
            except Exception as exc:
                # 不开 exc_info：理由同 delayed_check（traceback 原文带回凭证）
                logger.warning(
                    "[%s] patrol loop failed error=%s",
                    PLUGIN_ID,
                    redact_exc_text(exc),
                )
                # 添加退避延迟，避免错误循环
                await asyncio.sleep(min(PATROL_BACKOFF_DELAY_SEC, self.settings.check_interval_sec))

    async def _patrol_one_session(self, umo: str, now: float) -> None:
        """巡检单个会话：跳过条件 → 触发检查，单会话异常隔离不中断整轮。"""
        try:
            if not self.settings.enabled_private_sessions and session_is_private(umo):
                return
            if not self._last_events.get(umo):
                return
            state = self._state_for(umo)
            if self.settings.patrol_inactive_after_sec and (
                not state.last_active_at
                or now - state.last_active_at > self.settings.patrol_inactive_after_sec
            ):
                return
            if self._gate.is_running(umo):
                return
            generation = self._gate.current(umo)
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
            # 不开 exc_info：理由同 delayed_check（traceback 原文带回凭证）
            logger.warning(
                "[%s] patrol session failed session=%s error=%s",
                PLUGIN_ID,
                umo,
                redact_exc_text(exc),
            )

    async def stop_patrol(self) -> None:
        task = self._patrol_task
        self._patrol_task = None
        if task and not task.done():
            # 同 cancel_delay/_cancel_once：调用方（apply_enabled_topology）已经先
            # 经 _cancel_delay_tasks 取消过一次，这里再取消一次会把第二次取消送进
            # 被取消方的有界收敛窗口，被误判成"吞掉取消"而隔离登记 →
            # _mark_degraded 永久 DEGRADED，只能重载插件。
            if not task.cancelling():
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=max(0.0, PATROL_STOP_TIMEOUT_SEC))
            if not done:
                if self._quarantine_task:
                    self._quarantine_task(task, "patrol stop deadline exceeded")
                else:
                    logger.warning(
                        "[%s] patrol task ignored stop deadline and is unregistered",
                        PLUGIN_ID,
                    )
            else:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning(
                        "[%s] patrol task failed during stop: %s", PLUGIN_ID, redact_exc_text(exc)
                    )
