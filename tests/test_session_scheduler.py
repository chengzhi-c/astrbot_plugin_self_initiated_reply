"""SessionScheduler 独立单测（ticket 02 验收）：脱离主插件实例，仅注入依赖。

复用 test_vision 的宿主桩与包加载；调度器构造不依赖插件类，时序逻辑
（延迟注册/取消、清理窗口、巡检启动、静默等待）在此直接验证。
"""

from __future__ import annotations

import asyncio
import importlib
import os
from collections import deque
from pathlib import Path

from .test_vision import PACKAGE_NAME, _load_modules


def _scheduler_module():
    return importlib.import_module(f"{PACKAGE_NAME}.scheduler")


def _gate_module():
    return importlib.import_module(f"{PACKAGE_NAME}.session_gate")


def _make_scheduler(tmp_path: Path, config: dict | None = None):
    _, _, models = _load_modules()
    scheduler_mod = _scheduler_module()
    settings = models.Settings.from_config(config or {})
    gate = _gate_module().SessionGate()
    spawned: list[asyncio.Task] = []

    def spawn(coro):
        task = asyncio.create_task(coro)
        spawned.append(task)
        task.add_done_callback(spawned.remove)
        return task

    state_map: dict[str, models.SessionState] = {}
    checks: list[tuple[str, str, bool, int | None]] = []
    last_events: dict[str, object] = {}
    last_event_at: dict[str, float] = {}
    recent_image_events: dict[str, object] = {}
    whitelist_runtime_umos: dict[str, set[str]] = {}
    delay_tasks: dict[str, asyncio.Task] = {}
    running_check_tasks: dict[str, asyncio.Task] = {}
    background_tasks: set[asyncio.Task] = set()
    coordinator_mod = importlib.import_module(f"{PACKAGE_NAME}.session_coordinator")
    coordinator = coordinator_mod.SessionCoordinator(
        events=last_events,
        event_at=last_event_at,
        images=recent_image_events,
        gate=gate,
        cancel_delay=lambda umo, force: None,
        notify_silence=lambda umo: None,
    )

    async def check_session(umo, *, trigger, force, expected_generation):
        checks.append((umo, trigger, force, expected_generation))
        return "ok"

    scheduler = scheduler_mod.SessionScheduler(
        settings=settings,
        gate=gate,
        image_cache_dir=tmp_path / "image_cache",
        spawn=spawn,
        should_run=lambda: True,
        state_for=lambda umo: state_map.setdefault(umo, models.SessionState()),
        check_session=check_session,
        clear_event=coordinator.clear_event,
        drop_older_images=coordinator.drop_older_than,
        last_events=last_events,
        last_event_at=last_event_at,
        recent_image_events=recent_image_events,
        whitelist_runtime_umos=whitelist_runtime_umos,
        delay_tasks=delay_tasks,
        running_check_tasks=running_check_tasks,
        background_tasks=background_tasks,
    )
    return scheduler_mod, models, scheduler, state_map, checks


async def _cancel_and_await(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ============================================================================
# 延迟检查：注册与取消
# ============================================================================


async def test_schedule_registers_in_shared_delay_tasks(tmp_path: Path) -> None:
    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    umo = "s1"
    scheduler.schedule_delayed_check(umo, delay_sec=0, trigger="message_delay", force=False)
    task = scheduler._delay_tasks.get(umo)
    assert task is not None and not task.done()
    scheduler.cancel_delay(umo)
    assert umo not in scheduler._delay_tasks
    await _cancel_and_await(task)


def test_schedule_skips_when_stale_generation(tmp_path: Path) -> None:
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    umo = "s1"
    stale = scheduler._gate.advance(umo)
    scheduler._gate.advance(umo)  # 抬代次使 stale 过期
    scheduler.schedule_delayed_check(
        umo, delay_sec=0, trigger="message_delay", force=False, generation=stale
    )
    assert umo not in scheduler._delay_tasks


def test_schedule_skips_when_should_run_false(tmp_path: Path) -> None:
    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler._should_run = lambda: False
    umo = "s1"
    scheduler.schedule_delayed_check(umo, delay_sec=0, trigger="message_delay", force=False)
    assert umo not in scheduler._delay_tasks


async def test_cancel_delay_non_force_leaves_running_check_alive(tmp_path: Path) -> None:
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    umo = "s1"
    scheduler._gate.mark_running(umo)
    running = scheduler._spawn(_noop_coro())
    scheduler._running_check_tasks[umo] = running
    try:
        scheduler.cancel_delay(umo)
        assert not running.cancelled(), "运行中的检查不得被非强制取消打断"
    finally:
        scheduler._gate.unmark_running(umo)
        await _cancel_and_await(running)


async def _noop_coro():
    await asyncio.sleep(10)


async def test_cancel_delay_force_cancels_running_check(tmp_path: Path) -> None:
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    umo = "s1"
    running = scheduler._spawn(_noop_coro())
    scheduler._running_check_tasks[umo] = running
    scheduler.cancel_delay(umo, force=True)
    try:
        await running
    except asyncio.CancelledError:
        pass
    assert running.cancelled()


# ============================================================================
# 延迟检查：运行等待与运行表登记
# ============================================================================


async def test_delayed_check_waits_for_running_release_event(tmp_path: Path) -> None:
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    umo = "s1"
    scheduler._gate.mark_running(umo)
    try:
        task = asyncio.create_task(
            scheduler.delayed_check(
                umo,
                delay_sec=0,
                trigger="patrol",
                force=True,
                generation=scheduler._gate.advance(umo),
            )
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "运行集被占用时延迟检查必须等待"
        scheduler._gate.unmark_running(umo)
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done, "释放后延迟检查应完成"
    finally:
        scheduler._gate.unmark_running(umo)


async def test_delayed_check_drops_desynced_release_after_bounded_wait(
    tmp_path: Path, monkeypatch
) -> None:
    scheduler_mod, _, scheduler, _, checks = _make_scheduler(tmp_path)
    umo = "s1"
    generation = scheduler._gate.advance(umo)
    scheduler._gate.mark_running(umo)
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(scheduler_mod, "RELEASE_WAIT_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(scheduler_mod, "MAX_RELEASE_WAIT_ROUNDS", 2)
    monkeypatch.setattr(scheduler_mod.logger, "warning", lambda *args: warnings.append(args))

    task = asyncio.create_task(
        scheduler.delayed_check(
            umo,
            delay_sec=0,
            trigger="patrol",
            force=True,
            generation=generation,
        )
    )
    try:
        deadline = asyncio.get_event_loop().time() + 0.2
        while not task.done() and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert task.done(), "失同步 release 闸门必须在有限轮次后主动结束"
        assert not checks, "失同步时不得越过互斥闸门执行检查"
        assert any(args and "release gate desynced" in str(args[0]) for args in warnings)
    finally:
        scheduler._gate.unmark_running(umo)
        if not task.done():
            task.cancel()
        await task


async def test_delayed_check_registers_and_cleans_running_table(tmp_path: Path) -> None:
    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    umo = "s1"
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_check(umo_, *, trigger, force, expected_generation):
        started.set()
        await release.wait()
        return "ok"

    scheduler._check_session = blocking_check
    task = asyncio.create_task(
        scheduler.delayed_check(
            umo,
            delay_sec=0,
            trigger="message_delay",
            force=True,
            generation=scheduler._gate.advance(umo),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    assert scheduler._running_check_tasks.get(umo) is task
    release.set()
    await task
    assert umo not in scheduler._running_check_tasks, "运行检查表必须自清理"


# ============================================================================
# 静默等待事件化（ticket 11）：消息到达立即唤醒，唤醒后必复查代次与静默
# ============================================================================


async def test_silence_interrupted_aborts_when_session_invalidated(
    tmp_path: Path,
) -> None:
    """静默等待被新消息打断：会话已失效（代次推进）时任务必须立即退出。

    不产生检查（旧任务不得复活）；通知丢失或未事件化时任务要睡满旧周期，
    1s 断言窗口内不会结束——捕获"唤醒后必复查代次"语义。
    """
    _, models, scheduler, state_map, checks = _make_scheduler(
        tmp_path, {"min_silence_sec": 5, "message_delay_sec": 0}
    )
    umo = "s1"
    state = models.SessionState()
    state_map[umo] = state
    state.last_active_at = scheduler_mod_now()
    scheduler.schedule_delayed_check(umo, delay_sec=0, trigger="message_delay", force=False)
    await asyncio.sleep(0.3)  # 已进入静默等待
    scheduler._gate.advance(umo)  # 新消息使会话失效（代次推进）
    scheduler.notify_activity(umo)
    task = scheduler._delay_tasks.get(umo)
    assert task is not None
    # 不用 wait_for 收尾：其超时取消会被任务吸收（3.14 下 cancelled() 失真），
    # 掩盖"任务未被唤醒"——纯轮询观测任务是否主动退出。
    deadline = asyncio.get_event_loop().time() + 1.0
    while not task.done() and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert task.done(), "任务应被唤醒后主动退出（通知丢失/未复查代次则等待被拖延）"
    assert not checks, "失效后旧任务不得产生检查"


async def test_silence_interrupted_restarts_full_silence_cycle(
    tmp_path: Path,
) -> None:
    """静默等待被新消息打断：唤醒后按最新状态重新计时（不得沿用旧剩余静默）。

    静默期从最后一条消息起算满 min_silence：检查不得早于 最后消息 + 静默期。
    """
    _, models, scheduler, state_map, checks = _make_scheduler(
        tmp_path, {"min_silence_sec": 5, "message_delay_sec": 0}
    )
    umo = "s1"
    state = models.SessionState()
    state_map[umo] = state
    state.last_active_at = scheduler_mod_now()
    scheduler.schedule_delayed_check(umo, delay_sec=0, trigger="message_delay", force=False)
    await asyncio.sleep(1.0)  # 已进入静默等待；静默已消耗 1s
    state.last_active_at = scheduler_mod_now()  # 新消息：静默期重置
    t_last_msg = asyncio.get_event_loop().time()
    scheduler.notify_activity(umo)
    deadline = t_last_msg + 5.0 + 1.5
    while not checks and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert checks, "静默打断后应完成检查"
    assert asyncio.get_event_loop().time() - t_last_msg >= 4.9, (
        "检查不得早于最后消息后的完整静默期（沿用旧剩余会提前检查）"
    )


# ============================================================================
# 延迟与静默计算
# ============================================================================


def test_message_trigger_delay_computation(tmp_path: Path) -> None:
    _, models, scheduler, _, _ = _make_scheduler(tmp_path, {"min_silence_sec": 30})
    assert scheduler.message_trigger_delay("reply_request") == 30
    scheduler.settings.message_delay_sec = 60
    assert scheduler.message_trigger_delay("message_delay") == 60
    scheduler.settings.min_silence_sec = 90
    assert scheduler.message_trigger_delay("message_delay") == 90, "静默不小于消息延迟"


def test_remaining_silence_sec_computation(tmp_path: Path) -> None:
    _, models, scheduler, _, _ = _make_scheduler(tmp_path, {"min_silence_sec": 45})
    fresh = models.SessionState(last_active_at=scheduler_mod_now() - 10)
    assert 34 <= scheduler.remaining_silence_sec(fresh) <= 35
    old = models.SessionState(last_active_at=scheduler_mod_now() - 100)
    assert scheduler.remaining_silence_sec(old) == 0.0
    assert scheduler.remaining_silence_sec(models.SessionState()) == 0.0


# ============================================================================
# 事件与运行时 UMO 回收
# ============================================================================


def test_cleanup_events_interval_gate_and_stale_reaping(tmp_path: Path) -> None:
    _, models, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler.last_cleanup_at = 0.0
    stale_umo = "g1:user:a"
    fresh_umo = "g2:user:c"
    scheduler._last_events[stale_umo] = object()
    scheduler._last_event_at[stale_umo] = 100.0
    scheduler._last_events[fresh_umo] = object()
    scheduler._last_event_at[fresh_umo] = scheduler_mod_now()
    scheduler._whitelist_runtime_umos["g1"] = {stale_umo}
    scheduler._whitelist_runtime_umos["g2"] = {fresh_umo}

    scheduler.cleanup_events_if_needed()
    assert stale_umo not in scheduler._last_events, "陈旧事件必须回收"
    assert fresh_umo in scheduler._last_events, "新鲜事件必须保留"
    assert "g1" not in scheduler._whitelist_runtime_umos
    assert scheduler._whitelist_runtime_umos["g2"] == {fresh_umo}

    # 间隔门：刚清理过立即再触发不得重复执行
    scheduler._last_events[stale_umo] = object()
    scheduler._last_event_at[stale_umo] = 100.0
    scheduler.cleanup_events_if_needed()
    assert stale_umo in scheduler._last_events, "间隔内不得重复清理"


def scheduler_mod_now() -> float:
    return _scheduler_module().now_ts()


def test_cleanup_events_live_session_protection(tmp_path: Path) -> None:
    _, models, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler.last_cleanup_at = 0.0
    umo = "g1:user:a"
    scheduler._last_events[umo] = object()
    scheduler._last_event_at[umo] = 100.0
    scheduler._gate.mark_running(umo)
    try:
        scheduler.cleanup_events_if_needed()
        assert umo in scheduler._last_events, "运行中的会话事件不得回收"
    finally:
        scheduler._gate.unmark_running(umo)


# ============================================================================
# 巡检与图片清理
# ============================================================================


def test_patrol_disabled_does_not_spawn(tmp_path: Path) -> None:
    _, _, scheduler, _, _ = _make_scheduler(tmp_path, {"enabled_patrol_trigger": False})
    scheduler.ensure_patrol()
    assert scheduler.patrol_task is None


async def test_stop_patrol_quarantines_noncooperative_task(tmp_path: Path) -> None:
    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    release = asyncio.Event()
    quarantined: dict[str, object] = {}

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    def quarantine(task: asyncio.Task, reason: str) -> None:
        quarantined["task"] = task
        quarantined["reason"] = reason

    task = asyncio.create_task(stubborn())
    scheduler._patrol_task = task
    scheduler._stop_timeout = lambda: 0.01
    scheduler._quarantine_task = quarantine
    stopping = asyncio.create_task(scheduler.stop_patrol())
    await asyncio.sleep(0.05)
    assert stopping.done()
    assert scheduler.patrol_task is None
    assert quarantined["task"] is task
    assert quarantined["reason"] == "patrol stop deadline exceeded"

    release.set()
    await asyncio.wait_for(task, timeout=1)


async def test_patrol_enabled_spawns_and_stop_cancels(tmp_path: Path) -> None:
    _, _, scheduler, _, _ = _make_scheduler(
        tmp_path, {"enabled_patrol_trigger": True, "check_interval_sec": 86400}
    )
    scheduler.ensure_patrol()
    task = scheduler.patrol_task
    assert task is not None and not task.done()
    await scheduler.stop_patrol()
    assert task.cancelled()
    assert scheduler.patrol_task is None
    await _cancel_and_await(task)


def test_run_image_cleanup_removes_expired_files(tmp_path: Path) -> None:
    _, models, scheduler, _, _ = _make_scheduler(tmp_path, {"vision_image_age_sec": 60})
    root = tmp_path / "image_cache"
    root.mkdir()
    expired = root / "e.png"
    expired.write_bytes(b"x")
    os.utime(expired, (100.0, 100.0))
    removed = asyncio.run(scheduler.run_image_cleanup())
    assert removed == 1
    assert not expired.exists()


def test_run_image_cleanup_uses_configured_age_window(tmp_path: Path) -> None:
    """过期源按 vision_image_age_sec 删除，窗口内源和受保护源都留下。"""
    scheduler_mod, _models, scheduler, _, _ = _make_scheduler(
        tmp_path, {"vision_image_age_sec": 60}
    )
    _, image, _ = _load_modules()
    root = tmp_path / "image_cache"
    root.mkdir()
    now = 10_000.0
    expired = root / "expired.png"
    fresh = root / "fresh.png"
    protected = root / "protected.png"
    for path in (expired, fresh, protected):
        path.write_bytes(b"x")
    os.utime(expired, (now - 200.0, now - 200.0))
    os.utime(fresh, (now - 10.0, now - 10.0))
    os.utime(protected, (now - 200.0, now - 200.0))
    scheduler._recent_image_events["s1"] = deque(
        [(now - 10.0, [image.ImageInfo(prepared_source=str(protected))])]
    )
    original_now = scheduler_mod.now_ts
    scheduler_mod.now_ts = lambda: now
    try:
        removed = asyncio.run(scheduler.run_image_cleanup())
    finally:
        scheduler_mod.now_ts = original_now
    assert removed == 1
    assert not expired.exists()
    assert fresh.exists()
    assert protected.exists()
