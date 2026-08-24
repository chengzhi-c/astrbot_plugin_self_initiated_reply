"""SessionScheduler 覆盖率补盲（0.9.0 轴 D）：巡检循环、清理循环与溢出分支。

盲区背景（补盲前 scheduler.py 76%，_patrol_loop L425-L476 整段未覆盖）：
本文件以注入假 gate/回调模拟完整巡检周期，覆盖白名单迭代（含裸群组 ID
展开）、无事件跳过、无活动跳过、运行中跳过、会话异常继续、外层异常退避、
图片清理循环与事件缓存溢出回收。捕获力经 mutation 抽查核验（见交付记录）。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path

from .host_stubs import capture_logs, messages_at_least, until
from .test_session_scheduler import _make_scheduler


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


async def test_patrol_loop_outer_backoff_on_cleanup_error(tmp_path: Path) -> None:
    """循环级异常（清理阶段）：走外层 except 的退避重试路径。"""
    _, _, scheduler, _, _ = _make_scheduler(tmp_path, {"enabled_patrol_trigger": True})
    scheduler.settings.check_interval_sec = 0
    flags = _run_flags(scheduler)
    scheduler.settings.whitelist = {"qq:GroupMessage:1"}

    failures = {"n": 0}

    def boom() -> None:
        failures["n"] += 1
        raise RuntimeError("cleanup broken")

    scheduler.cleanup_events_if_needed = boom
    task = asyncio.create_task(scheduler._patrol_loop())
    try:
        # ≥2 次失败证明退避后重新进入循环体（外层 except + backoff 生效）
        await until(lambda: failures["n"] >= 2)
    finally:
        flags["run"] = False
        await asyncio.wait_for(task, timeout=5)


async def test_ensure_patrol_spawns_and_stops(tmp_path: Path) -> None:
    """ensure_patrol 在触发器启用时拉起循环，stop_patrol 干净收敛。"""
    _, _, scheduler, _, _ = _make_scheduler(tmp_path, {"enabled_patrol_trigger": True})
    scheduler.settings.check_interval_sec = 0
    flags = _run_flags(scheduler)
    scheduler.ensure_patrol()
    task = scheduler.patrol_task
    assert task is not None and not task.done()
    flags["run"] = False
    await scheduler.stop_patrol()
    assert scheduler.patrol_task is None


# ============================================================================
# 图片清理循环与事件缓存溢出
# ============================================================================


async def test_image_cleanup_loop_cycle_and_error_retry(tmp_path: Path) -> None:
    """清理循环：首轮失败走 60s 退避重试，次轮正常执行后随开关退出。"""
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    orig_sleep = scheduler_mod.asyncio.sleep
    sleeps: list[float] = []

    async def fast_sleep(delay):
        sleeps.append(delay)

    scheduler_mod.asyncio.sleep = fast_sleep
    seq = iter([True, True, True, True, True, False])
    scheduler._should_run = lambda: next(seq, False)
    cleanups = {"n": 0}

    async def run_cleanup() -> int:
        cleanups["n"] += 1
        if cleanups["n"] == 1:
            raise RuntimeError("cleanup broken")
        return 0

    scheduler.run_image_cleanup = run_cleanup
    try:
        await asyncio.wait_for(scheduler._image_cleanup_loop(), timeout=5)
    finally:
        scheduler_mod.asyncio.sleep = orig_sleep
    assert cleanups["n"] == 2  # 失败重试后第二轮正常执行
    assert 60.0 in sleeps  # 错误退避延迟


async def test_ensure_image_cleanup_spawns_loop(tmp_path: Path) -> None:
    """ensure_image_cleanup 拉起周期清理任务并随开关自然退出。"""
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)
    assert scheduler.image_cleanup_task is None
    orig_sleep = scheduler_mod.asyncio.sleep

    async def fast_sleep(delay):
        pass

    scheduler_mod.asyncio.sleep = fast_sleep
    seq = iter([True, True, False])
    scheduler._should_run = lambda: next(seq, False)
    try:
        scheduler.ensure_image_cleanup()
        task = scheduler.image_cleanup_task
        assert task is not None
        await asyncio.wait_for(task, timeout=5)
    finally:
        scheduler_mod.asyncio.sleep = orig_sleep


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
        return None

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


async def test_delayed_check_warns_on_check_error(tmp_path: Path, caplog: object) -> None:
    """检查回调抛错：记 warning 后吞掉，不向上传播。

    异常必须留痕。它由 ``asyncio.Task`` 驱动，抛出只会变成无人接管的任务异常；
    若降级为 debug 或静默 pass，线上就再也看不到检查失败。
    """
    scheduler_mod, _, scheduler, _, _ = _make_scheduler(tmp_path)

    async def boom(umo, *, trigger, force, expected_generation):
        raise RuntimeError("check broken")

    scheduler._check_session = boom
    with capture_logs(caplog, scheduler_mod.logger, logging.WARNING):
        await scheduler.delayed_check("s1", delay_sec=0)  # 异常应被内部吞掉

    warnings = messages_at_least(caplog, logging.WARNING)
    assert any("check broken" in msg or "delayed check" in msg for msg in warnings), (
        f"检查失败未记 warning，异常被静默吞掉：{warnings}"
    )
