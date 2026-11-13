"""SessionScheduler 独立单测：脱离主插件实例，仅注入依赖。

复用 test_vision 的宿主桩与包加载；调度器构造不依赖插件类，时序逻辑
（延迟注册/取消、清理窗口、巡检启动、静默等待）在此直接验证。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from ._support import core_loader
from .host_stubs import capture_logs, messages_at_least, until

PACKAGE_NAME = "selfreply_scheduler_test_package"
_load_modules = core_loader(PACKAGE_NAME)


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
    last_decisions: dict[str, dict[str, Any]] = {}
    recent_image_events: dict[str, object] = {}
    whitelist_runtime_umos: dict[str, set[str]] = {}
    delay_tasks: dict[str, asyncio.Task] = {}
    running_check_tasks: dict[str, asyncio.Task] = {}
    background_tasks: set[asyncio.Task] = set()
    coordinator_mod = importlib.import_module(f"{PACKAGE_NAME}.session_coordinator")
    coordinator = coordinator_mod.SessionCoordinator(
        last_events=last_events,
        last_event_at=last_event_at,
        recent_image_events=recent_image_events,
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
        last_decisions=last_decisions,
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


async def test_wait_release_gives_up_silently_when_run_eligibility_lost(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """运行资格（should_run/代次）失效时,等待立即放弃且不打脱同步告警。

    守护的承重行为:每轮等待后重验运行资格与代次,它只存在于
    ``on_round_expired`` 实参中,删掉该实参不会让既有任何用例变红。本用例
    锚定两面:立即返回 False,以及「调用方主动放弃」不得被误报为
    ``release gate desynced``（那是门与运行集真脱同步才该有的告警）。
    """
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    monkeypatch.setattr(scheduler_mod, "RELEASE_WAIT_TIMEOUT_SEC", 0.05)
    gate = scheduler._gate
    umo = "s1"
    generation = gate.advance(umo)
    gate.mark_running(umo)
    # 等待进行中插件被停用:上一检查仍占用运行标记,但本协作者已无资格运行。
    scheduler._should_run = lambda: False

    started = time.monotonic()
    with capture_logs(caplog, scheduler_mod.logger) as logs:
        released = await scheduler._wait_for_previous_check_release(
            umo, "message_delay", generation
        )
    elapsed = time.monotonic() - started

    assert released is False, "运行资格失效后必须放弃等待"
    # 一轮 0.05s 内放弃;若 on_round_expired 判定被移除,等待会耗满
    # 20 轮上限（约 1s）才返回,时序差 20 倍,断言据此捕获该回归。
    assert elapsed < 0.5, f"运行资格失效应在一轮内放弃,实际耗时 {elapsed:.2f}s"
    assert not messages_at_least(logs, logging.WARNING), (
        "调用方主动放弃是正常路径,不得打 release gate desynced 告警"
    )


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
# 静默等待事件化：消息到达立即唤醒，唤醒后必复查代次与静默
# ============================================================================


async def test_silence_interrupted_aborts_when_session_invalidated(
    tmp_path: Path,
) -> None:
    """静默等待被新消息打断：会话已失效（代次推进）时任务必须立即退出。

    不产生检查（旧任务不得复活）；通知丢失或未事件化时任务要睡满旧周期，
    1s 断言窗口内不会结束，捕获"唤醒后必复查代次"语义。
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
    # 掩盖"任务未被唤醒"，纯轮询观测任务是否主动退出。
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
    # 等任务真正注册静默事件再打断：固定 sleep 在事件循环被阻塞时会把
    # "还没进等待"当成"已进等待"，notify 丢失即假红。setdefault 之后没有
    # await 点，键出现即代表任务已承诺进入等待。
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 1.0
    while umo not in scheduler._silence_events and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert umo in scheduler._silence_events, "任务未进入静默等待，本用例前提不成立"
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
    scheduler.settings.message_delay_sec = 60
    assert scheduler.message_trigger_delay() == 60
    scheduler.settings.min_silence_sec = 90
    assert scheduler.message_trigger_delay() == 90, "静默不小于消息延迟"


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
    scheduler._last_cleanup = 0.0
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
    scheduler._last_cleanup = 0.0
    umo = "g1:user:a"
    scheduler._last_events[umo] = object()
    scheduler._last_event_at[umo] = 100.0
    scheduler._gate.mark_running(umo)
    try:
        scheduler.cleanup_events_if_needed()
        assert umo in scheduler._last_events, "运行中的会话事件不得回收"
    finally:
        scheduler._gate.unmark_running(umo)


def test_cleanup_events_reaps_stale_last_decisions(tmp_path: Path) -> None:
    """陈旧会话的「最近裁决」必须随其事件缓存一并回收。

    此前 janitor 只回收事件/图片索引/运行时 UMO 三张表，``_last_decisions`` 无人
    认领：静默白名单会话的裁决条目一直滞留（上限只是白名单规模 1000），还被
    ``GET /status`` 整表回显。裁决是跟随事件的诊断信息，事件没了它就是孤儿。
    """
    _, models, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler._last_cleanup = 0.0
    stale_umo = "g1:user:a"
    fresh_umo = "g2:user:c"
    live_umo = "g3:user:e"
    for umo, active_at in ((stale_umo, 100.0), (fresh_umo, scheduler_mod_now()), (live_umo, 100.0)):
        scheduler._last_events[umo] = object()
        scheduler._last_event_at[umo] = active_at
        scheduler._last_decisions[umo] = {
            "at": 1.0,
            "trigger": "message_delay",
            "should_reply": False,
            "reason": "不接话",
        }
    scheduler._gate.mark_running(live_umo)
    try:
        scheduler.cleanup_events_if_needed()
    finally:
        scheduler._gate.unmark_running(live_umo)

    assert stale_umo not in scheduler._last_events, "陈旧事件必须回收"
    assert stale_umo not in scheduler._last_decisions, "陈旧会话的裁决必须一并回收"
    assert fresh_umo in scheduler._last_decisions, "有新事件的会话裁决必须保留"
    assert live_umo in scheduler._last_decisions, "运行中会话的裁决不得回收"


def test_cleanup_events_respects_a_patrol_window_wider_than_the_default(tmp_path: Path) -> None:
    """巡检不活跃窗口配得比 EVENT_CLEANUP_INTERVAL_SEC 宽时，事件不得先被回收。

    缺陷形态：陈旧阈值恒为 ``EVENT_CLEANUP_INTERVAL_SEC``（1h）。事件一旦被回收，
    ``_patrol_one_session`` 的「没有可用的最近消息事件」判据会让该会话永久跳过，
    于是 ``patrol_inactive_after_sec`` 上限 604800 中超过 1h 的部分完全无效——
    用户把窗口调到 7 天，实际行为仍是 1 小时。

    变异锚定：把阈值改回常量，本用例红。
    """
    _, _, scheduler, _, _ = _make_scheduler(tmp_path, {"patrol_inactive_after_sec": 604800})
    scheduler._last_cleanup = 0.0
    now = scheduler_mod_now()
    within_window = "g1:user:a"
    beyond_window = "g2:user:b"
    for umo, age in ((within_window, 7200.0), (beyond_window, 700000.0)):
        scheduler._last_events[umo] = object()
        scheduler._last_event_at[umo] = now - age

    scheduler.cleanup_events_if_needed()

    assert within_window in scheduler._last_events, (
        "事件在巡检不活跃窗口内就被回收：该会话将永久跳过巡检"
    )
    assert beyond_window not in scheduler._last_events, "超出巡检不活跃窗口的事件仍未回收"


def test_cleanup_events_overflow_reaps_last_decisions(tmp_path: Path) -> None:
    """溢出通道同样要回收裁决：被它摘掉的会话此后不再进陈旧通道。

    缺陷形态：只在陈旧通道 pop 裁决。溢出通道把会话从事件表摘掉后，该 umo 再也
    不会出现在 ``removable`` 里，于是它的裁决条目比"只修陈旧通道"活得更久——
    同一漏洞换个更窄的门再次漏进来。
    """
    _, models, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler._last_cleanup = 0.0
    now = scheduler_mod_now()
    for index in range(models.MAX_CACHED_EVENTS + 1):
        umo = f"g{index}:user:x"
        scheduler._last_events[umo] = object()
        scheduler._last_event_at[umo] = now
        scheduler._last_decisions[umo] = {"at": 1.0, "should_reply": False, "reason": "x"}

    scheduler.cleanup_events_if_needed()

    assert len(scheduler._last_events) == models.MAX_CACHED_EVENTS, "溢出清理未生效"
    assert len(scheduler._last_decisions) == models.MAX_CACHED_EVENTS, "裁决表未随溢出同步回收"


# ============================================================================
# 巡检与图片清理
# ============================================================================


def test_patrol_disabled_does_not_spawn(tmp_path: Path) -> None:
    _, _, scheduler, _, _ = _make_scheduler(tmp_path, {"enabled_patrol_trigger": False})
    scheduler.ensure_patrol()
    assert scheduler.patrol_task is None


async def test_stop_patrol_quarantines_noncooperative_task(tmp_path: Path, monkeypatch) -> None:
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
    # 超时单源：patch 模块常量（原为注入私有回调，参数已删）。
    monkeypatch.setattr(_scheduler_module(), "PATROL_STOP_TIMEOUT_SEC", 0.01)
    scheduler._quarantine_task = quarantine
    stopping = asyncio.create_task(scheduler.stop_patrol())
    await asyncio.sleep(0.05)
    assert stopping.done()
    assert scheduler.patrol_task is None
    assert quarantined["task"] is task
    assert quarantined["reason"] == "patrol stop deadline exceeded"

    release.set()
    await asyncio.wait_for(task, timeout=1)


async def test_stop_patrol_does_not_recancel_an_already_cancelled_task(
    tmp_path: Path,
) -> None:
    """已在取消路径上的巡检不得补第二次取消，否则合作任务被误隔离。

    缺陷形态：``stop_patrol`` 无条件 ``task.cancel()``。真实禁用路径是
    ``apply_enabled_topology`` → ``_cancel_delay_tasks()``（巡检任务在
    ``_background_tasks`` 里，已被取消一次）→ ``stop_patrol()``。第二次取消落进
    巡检内联检查自己的有界收敛窗口，被 ``_graceful_stop`` 判成"吞掉取消"，
    隔离登记 → ``_mark_degraded`` 把插件永久打成 DEGRADED，只能重载。

    变异锚定：去掉 ``if not task.cancelling()`` 守卫，本用例红。
    """
    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    cancelled_once = asyncio.Event()
    release = asyncio.Event()

    async def cooperative() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # 模拟被取消方自己的宽限收敛：让出事件循环后才收尾。
            cancelled_once.set()
            await release.wait()
            raise

    task = asyncio.create_task(cooperative())
    scheduler._patrol_task = task
    await asyncio.sleep(0)
    task.cancel()  # 第一段: _cancel_delay_tasks 已取消
    await asyncio.wait_for(cancelled_once.wait(), timeout=1)

    stopping = asyncio.create_task(scheduler.stop_patrol())
    await asyncio.sleep(0.05)
    assert task.cancelling() == 1, f"巡检被补了第二次取消：cancelling={task.cancelling()}"
    release.set()
    await asyncio.wait_for(stopping, timeout=1)
    await _cancel_and_await(task)


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


# ============================================================================
# SessionGate.prune：锁持有者保护
# ============================================================================


async def test_prune_keeps_held_lock_and_reclaims_after_release() -> None:
    """prune 不得摘走仍被旧检查持有的会话锁。

    移出白名单→立即重加时，若锁被摘除，新检查拿到新锁对象，与仍在
    await LLM 的旧检查并发；只有当代次门之外的锁互斥也成立，运行边界
    才完整。锁释放后的下一次 prune 负责回收。
    """
    gate = _gate_module().SessionGate()
    lock = gate.lock_for("s1")
    await lock.acquire()
    try:
        gate.prune("s1")
        assert gate.lock_for("s1") is lock, "仍被持有的锁被 prune 摘走"
    finally:
        lock.release()
    gate.prune("s1")
    assert gate.lock_for("s1") is not lock, "释放后的下一次 prune 应回收旧锁"


# ============================================================================
# 降级宿主分支、清理窗口边界与巡检容错
# ============================================================================


def _run_flags(scheduler) -> dict[str, bool]:
    flags = {"run": True}
    scheduler._should_run = lambda: flags["run"]
    return flags


# ============================================================================
# 巡检循环主体（原 76% 盲区的核心）
# ============================================================================


async def test_patrol_loop_full_cycle_branches(tmp_path: Path) -> None:
    """白名单迭代全分支：有事件活动会话被检查，其余三种跳过。"""
    _, models, scheduler, state_map, checks = _make_scheduler(
        tmp_path,
        {"enabled_patrol_trigger": True, "patrol_inactive_after_sec": 60},
    )
    scheduler.settings.check_interval_sec = 0  # 绕开 30s 配置下限，直接驱动循环
    flags = _run_flags(scheduler)
    _ = state_map  # 状态经 scheduler._state_for 统一创建，避免裸取空表

    active = "qq:GroupMessage:1"
    no_event = "qq:GroupMessage:2"
    inactive = "qq:GroupMessage:3"
    running = "fake:group:9"
    scheduler.settings.whitelist = {active, no_event, inactive, "12345"}
    # 裸群组 ID 经运行时映射展开为真实 UMO（_runtime_umos_for_whitelist_item 分支）
    scheduler._whitelist_runtime_umos["12345"] = {running}

    now = models.now_ts()
    event = object()
    for umo in (active, inactive, running):
        scheduler._last_events[umo] = event
        scheduler._last_event_at[umo] = now
    scheduler._state_for(active).last_active_at = now  # 活动窗口内 → 进入检查
    # inactive：有事件但 last_active_at=0 → patrol_inactive_after 跳过
    scheduler._gate.mark_running(running)  # 运行中 → is_running 跳过
    # no_event：白名单内但无缓存事件 → 直接 continue

    task = asyncio.create_task(scheduler._patrol_loop())
    try:
        await until(lambda: bool(checks))
    finally:
        flags["run"] = False
        await asyncio.wait_for(task, timeout=5)

    assert all(umo == active for umo, *_ in checks), checks
    _umo, trigger, force, expected_generation = checks[0]
    assert trigger == "patrol" and force is False
    assert expected_generation == 0  # 无代次记录会话保持 None→基线 0 语义


async def test_patrol_skips_private_when_disabled(tmp_path: Path) -> None:
    """关私聊主动回复后，巡检只碰群会话。"""
    _, models, scheduler, _, checks = _make_scheduler(
        tmp_path,
        {"enabled_patrol_trigger": True, "patrol_inactive_after_sec": 0},
    )
    scheduler.settings.check_interval_sec = 0
    scheduler.settings.enabled_private_sessions = False
    flags = _run_flags(scheduler)
    group = "qq:GroupMessage:1"
    private = "qq:FriendMessage:user-1"
    scheduler.settings.whitelist = {group, private}
    now = models.now_ts()
    event = object()
    for umo in (group, private):
        scheduler._last_events[umo] = event
        scheduler._last_event_at[umo] = now
        scheduler._state_for(umo).last_active_at = now

    task = asyncio.create_task(scheduler._patrol_loop())
    try:
        await until(lambda: bool(checks))
    finally:
        flags["run"] = False
        await asyncio.wait_for(task, timeout=5)

    assert checks
    assert all(umo == group for umo, *_ in checks), checks


async def test_patrol_loop_session_error_continues(tmp_path: Path, caplog: object) -> None:
    """单个会话检查抛错：记 warning 后继续巡检其余轮次（不终止循环）。

    循环吞掉异常继续跑，是可用性上的正确选择，但也意味着一个会话可以永久
    失败而无人知晓。warning 是唯一的暴露渠道。
    """
    scheduler_mod, models, scheduler, _, _ = _make_scheduler(
        tmp_path, {"enabled_patrol_trigger": True}
    )
    scheduler.settings.check_interval_sec = 0
    scheduler.settings.patrol_inactive_after_sec = 0
    flags = _run_flags(scheduler)
    scheduler.settings.whitelist = {"qq:GroupMessage:1"}
    scheduler._last_events["qq:GroupMessage:1"] = object()
    # 时间戳必须新鲜：否则循环开头的陈旧事件清理会先回收掉检查目标
    scheduler._last_event_at["qq:GroupMessage:1"] = models.now_ts()

    attempts = {"n": 0}

    async def boom(umo, *, trigger, force, expected_generation):
        attempts["n"] += 1
        raise RuntimeError("session check broken")

    scheduler._check_session = boom
    with capture_logs(caplog, scheduler_mod.logger, logging.WARNING):
        task = asyncio.create_task(scheduler._patrol_loop())
        try:
            # ≥2 次尝试证明异常后循环仍在推进（内层 except 生效）
            await until(lambda: attempts["n"] >= 2)
        finally:
            flags["run"] = False
            await asyncio.wait_for(task, timeout=5)

    warnings = messages_at_least(caplog, logging.WARNING)
    assert any("patrol session failed" in msg for msg in warnings), (
        f"会话巡检失败未记 warning，故障会话可永久静默失败：{warnings}"
    )


async def test_delayed_check_error_log_is_redacted_without_traceback(
    tmp_path: Path, caplog: object
) -> None:
    """delayed_check 的异常 warning 必须脱敏，且**不得**带 traceback。

    缺陷形态：``logger.warning(..., redact_exc_text(exc), exc_info=True)``。
    ``redact_exc_text`` 只作用于 ``record.getMessage()``，而 ``exc_info=True``
    会把**未脱敏的完整 traceback** 挂到同一条 record 上，traceback 末尾正是
    ``str(exc)`` 原文。provider SDK 的异常串标准形态就带签名 URL
    （``Client error '401' for url '...?api_key=...'``），于是脱敏文案与泄漏
    原文并列出现在同一行日志里，脱敏契约在巡检/延迟检查这两条最高频异常路径上
    实际不成立。口径同 delivery/generation 的失败分支（那边早就注明"不开
    exc_info，traceback 帧常持有 request"）。

    变异锚定：把该处 ``exc_info=True`` 加回来，本用例红。
    """
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    umo = "s1"
    signed_url = (
        "https://api.example.com/v1/chat/completions"
        "?api_key=sk-LIVE-SECRET-123&Signature=SECRET_SIGNATURE"
    )

    async def boom(umo_, *, trigger, force, expected_generation):
        raise RuntimeError(f"Client error '401' for url '{signed_url}'")

    scheduler._check_session = boom
    with capture_logs(caplog, scheduler_mod.logger, logging.WARNING):
        await scheduler.delayed_check(
            umo,
            delay_sec=0,
            trigger="message_delay",
            force=True,
            generation=scheduler._gate.advance(umo),
        )
    records = [r for r in caplog.records if "delayed check failed" in r.getMessage()]
    assert records, "delayed_check 异常未记 warning"
    # 脱敏必须落在**整条渲染后的日志**上，不只是 getMessage()
    rendered = "\n".join(
        caplog.handler.format(record) if hasattr(caplog, "handler") else record.getMessage()
        for record in records
    )
    for secret in ("sk-LIVE-SECRET-123", "SECRET_SIGNATURE"):
        assert secret not in rendered, f"异常日志泄漏凭证 {secret}：{rendered}"
    assert "<redacted>" in rendered, f"未标记 query 已被剥离：{rendered}"
    assert all(record.exc_info is None for record in records), (
        "delayed_check 的 warning 带了 traceback：其末尾是未脱敏的 str(exc) 原文"
    )


async def test_patrol_loop_outer_backoff_on_cleanup_error(tmp_path: Path, monkeypatch: Any) -> None:
    """循环级异常（清理阶段）：走外层 except 的退避重试路径。

    变异锚定：删掉 ``scheduler.py`` 外层 except 里的退避 ``sleep``，本用例红。

    只断言「≥2 次失败」是假绿：``check_interval_sec=0`` 时退避实参
    ``min(60, 0) == 0``，与循环顶部的实参同为 0，**有没有那行都能立刻重试**。
    这里把 ``check_interval_sec`` 设为 300（> ``PATROL_BACKOFF_DELAY_SEC``），
    使顶部长睡与退避长睡**取值可分辨**（300 vs 60），再把 ``asyncio.sleep`` 换
    成只记录、不真等的替身，于是「出现过一次 60 秒的 sleep」就是退避行存在的
    直接证据，删除即红。
    """
    _, models, scheduler, _, _ = _make_scheduler(tmp_path, {"enabled_patrol_trigger": True})
    scheduler.settings.check_interval_sec = 300
    flags = _run_flags(scheduler)
    scheduler.settings.whitelist = {"qq:GroupMessage:1"}

    failures = {"n": 0}
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        # 替身一律不真等：300s 的巡检间隔与 60s 退避都只做记录，用例仍秒级完成。
        await real_sleep(0)

    def boom() -> None:
        failures["n"] += 1
        raise RuntimeError("cleanup broken")

    scheduler.cleanup_events_if_needed = boom
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    task = asyncio.create_task(scheduler._patrol_loop())
    try:
        # ≥2 次失败证明异常后循环仍在推进（外层 except 未终止循环）
        await until(lambda: failures["n"] >= 2)
    finally:
        flags["run"] = False
        await asyncio.wait_for(task, timeout=5)

    expected_backoff = min(models.PATROL_BACKOFF_DELAY_SEC, scheduler.settings.check_interval_sec)
    assert expected_backoff != scheduler.settings.check_interval_sec, (
        "夹具要求退避与巡检间隔可分辨，否则本用例退化为『有无 sleep』的空断言"
    )
    assert expected_backoff in sleeps, (
        f"重试之间没有 {expected_backoff}s 的退避 sleep，外层退避已被删除（假绿）sleeps={sleeps!r}"
    )


# ============================================================================
# 图片清理循环与事件缓存溢出
# ============================================================================


async def test_image_cleanup_loop_cycle_and_error_retry(tmp_path: Path, monkeypatch: Any) -> None:
    """清理循环：首轮失败走 60s 退避重试，次轮正常执行后随开关退出。"""
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    sleeps: list[float] = []

    async def fast_sleep(delay):
        sleeps.append(delay)

    # 经 monkeypatch 打桩：scheduler_mod.asyncio 就是 stdlib 模块本身，直接
    # 赋值会把 asyncio.sleep 进程级换掉，本用例之后跑的一切 await 都吃替身。
    monkeypatch.setattr(scheduler_mod.asyncio, "sleep", fast_sleep)
    seq = iter([True, True, True, True, True, False])
    scheduler._should_run = lambda: next(seq, False)
    cleanups = {"n": 0}

    async def run_cleanup() -> int:
        cleanups["n"] += 1
        if cleanups["n"] == 1:
            raise RuntimeError("cleanup broken")
        return 0

    scheduler.run_image_cleanup = run_cleanup
    await asyncio.wait_for(scheduler._image_cleanup_loop(), timeout=5)
    assert cleanups["n"] == 2  # 失败重试后第二轮正常执行
    assert 60.0 in sleeps  # 错误退避延迟


async def test_ensure_image_cleanup_spawns_loop(tmp_path: Path, monkeypatch: Any) -> None:
    """ensure_image_cleanup 拉起周期清理任务并随开关自然退出。"""
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    assert scheduler.image_cleanup_task is None

    async def fast_sleep(delay):
        pass

    monkeypatch.setattr(scheduler_mod.asyncio, "sleep", fast_sleep)
    seq = iter([True, True, False])
    scheduler._should_run = lambda: next(seq, False)
    scheduler.ensure_image_cleanup()
    task = scheduler.image_cleanup_task
    assert task is not None
    await asyncio.wait_for(task, timeout=5)


async def test_ensure_image_cleanup_restarts_cancelling_loop(tmp_path: Path) -> None:
    """取消已交付但任务尚未收敛时，ensure 必须重启清理循环。

    缺陷形态：判据只有 ``is None or done()``，取消在途（cancelling）两者都
    不满足 → 跳过重启。runtime_enabled=True 而清理循环已经没了，此后再没有
    磁盘巡检（webapi 回滚路径「取消后不 await 直接 ensure」正对窗口）。
    """
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    release = asyncio.Event()

    async def stubborn() -> None:
        # 吞掉取消继续睡：这才有「取消已交付、任务未收敛」的中间态
        while not release.is_set():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                continue

    scheduler._should_run = lambda: True
    scheduler._image_cleanup_task = asyncio.ensure_future(stubborn())
    task = scheduler._image_cleanup_task
    await asyncio.sleep(0)  # 先让任务真正跑起来，否则取消的是未启动的任务
    task.cancel()
    await asyncio.sleep(0)  # 取消已交付，任务未收敛
    assert not task.done() and task.cancelling()

    scheduler.ensure_image_cleanup()
    restarted = scheduler._image_cleanup_task
    assert restarted is not task, "取消在途的清理循环没有被重启"
    # 收尾：放行后取消，两个任务都干净退出（stubborn 吞取消但不吞退出条件）
    release.set()
    task.cancel()
    restarted.cancel()
    await asyncio.gather(task, restarted, return_exceptions=True)


def test_cleanup_image_sources_drops_expired_entries(tmp_path: Path) -> None:
    """过期图片事件条目出队；会话条目清空后整键回收。"""
    _, models, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler.settings.vision_image_age_sec = 300
    now = models.now_ts()
    scheduler._recent_image_events["s1"] = deque([(now - 400.0, [])])
    removed = scheduler.cleanup_image_sources(now=now)
    assert "s1" not in scheduler._recent_image_events
    assert removed == 0  # 缓存目录不存在时无文件可清


def test_cleanup_events_overflow_prunes_oldest(tmp_path: Path) -> None:
    """超过 MAX_CACHED_EVENTS 的缓存按最旧优先回收（无任务会话）。"""
    _, models, scheduler, _, _ = _make_scheduler(tmp_path)
    now = models.now_ts()
    scheduler._last_cleanup = now - 4000.0  # 跨过清理间隔门槛
    for i in range(105):
        umo = f"plat:GroupMessage:{i}"
        scheduler._last_events[umo] = object()
        scheduler._last_event_at[umo] = now + i * 0.001  # 全部新鲜，仅比先后
    scheduler.cleanup_events_if_needed()
    assert len(scheduler._last_events) == 100
    for i in range(5):  # 最旧的 5 个被回收
        assert f"plat:GroupMessage:{i}" not in scheduler._last_events


# ============================================================================
# 延迟检查的残余分支
# ============================================================================


def test_schedule_spawn_none_skips_registration(tmp_path: Path) -> None:
    """spawn 返回 None（插件停止中）：不登记延迟任务。"""
    _, _, scheduler, _, _ = _make_scheduler(tmp_path)

    def no_spawn(coro):
        coro.close()

    scheduler._spawn = no_spawn
    scheduler.schedule_delayed_check("s1", delay_sec=0, trigger="message_delay", force=False)
    assert "s1" not in scheduler._delay_tasks


async def test_delayed_check_exits_when_disabled_after_sleep(tmp_path: Path) -> None:
    """延迟结束后插件已停用：直接退出，不得再落到会话检查。"""
    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler._should_run = lambda: False

    calls: list[str] = []

    async def record(umo, *, trigger, force, expected_generation):
        calls.append(umo)

    scheduler._check_session = record
    await scheduler.delayed_check("s1", delay_sec=0)
    assert calls == [], "插件已停用仍执行了会话检查：停用后的旧延迟任务会复活发送"


async def test_cancel_delay_spares_running_check_and_yields_queued_one(tmp_path: Path) -> None:
    """``cancel_delay`` 三态：运行中的登记保留、force 真取消、排队中的必须让位。

    运行中的检查不能被新消息取消：它的 await 链还要走到代次闸门（否则在途结论
    无处推导，而且每条新消息都重排一次只会叠判断）。但同时排在后面的 delayed_check
    必须让位，否则旧任务留在表里挡住新任务登记。
    """
    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    umo = "s1"
    running = asyncio.create_task(asyncio.Event().wait())
    queued = asyncio.create_task(asyncio.Event().wait())
    other_running = asyncio.create_task(asyncio.Event().wait())
    try:
        # 1. 同一任务既在运行又在排队：非 force 取消不得动它
        silence = asyncio.Event()
        scheduler._delay_tasks[umo] = running
        scheduler._running_check_tasks[umo] = running
        scheduler._silence_events[umo] = silence
        scheduler.cancel_delay(umo)
        assert scheduler._delay_tasks.get(umo) is running, "运行中的检查不得被新消息取消登记"
        assert scheduler._silence_events.get(umo) is silence, "静默事件不得被丢弃"
        assert not running.done()

        # 2. force 才真取消
        scheduler.cancel_delay(umo, force=True)
        assert umo not in scheduler._delay_tasks
        assert umo not in scheduler._silence_events
        await asyncio.sleep(0)
        assert running.cancelled(), "force 取消失效"

        # 3. 表里是后续排队的任务：必须让位并取消它，运行中的另一个不受影响
        scheduler._delay_tasks[umo] = queued
        scheduler._running_check_tasks[umo] = other_running
        scheduler._silence_events[umo] = asyncio.Event()
        scheduler.cancel_delay(umo)
        assert umo not in scheduler._delay_tasks, "排队的 delayed_check 必须让位"
        await asyncio.sleep(0)
        assert queued.cancelled()
        assert not other_running.done(), "运行中的另一个检查被误取消"
    finally:
        for task in (running, queued, other_running):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def test_cleanup_events_treats_a_zero_patrol_window_as_unbounded(tmp_path: Path) -> None:
    """patrol_inactive_after_sec 取 0（不截断）时事件不得按 1 小时回收。

    缺陷形态：陈旧阈值写成 max(EVENT_CLEANUP_INTERVAL_SEC, 0)，面板承诺的
    「不截断」实际只有一小时：静默一小时以上的会话事件先被回收，巡检随后因
    「没有可用的最近消息事件」永久跳过它。

    变异锚定：把 or 折算回裸 max()，本用例红。
    """
    _, _, scheduler, _, _ = _make_scheduler(tmp_path, {"patrol_inactive_after_sec": 0})
    scheduler._last_cleanup = 0.0
    now = scheduler_mod_now()
    umo = "g1:user:a"
    scheduler._last_events[umo] = object()
    scheduler._last_event_at[umo] = now - 7200.0

    scheduler.cleanup_events_if_needed()

    assert umo in scheduler._last_events, (
        "「不截断」被折成 1 小时：事件在 2h 时就被回收了，该会话将永久跳过巡检"
    )
