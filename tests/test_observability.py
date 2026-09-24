"""泄漏告警契约：后台任务数/代次表规模超阈值发出 warning，回落前不重复告警。"""

from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path

from .host_stubs import load_modules

PACKAGE_NAME = "selfreply_observability_test_package"


def _load_modules():
    return load_modules(PACKAGE_NAME, "scheduler", "models")


def _new_scheduler(tmp_path: Path):
    scheduler, models = _load_modules()
    instance, gate, delay_tasks, background_tasks = _make_scheduler(tmp_path, scheduler, models)
    return instance, models, gate, delay_tasks, background_tasks


def _make_scheduler(tmp_path: Path, scheduler, models):
    gate_mod = importlib.import_module(f"{PACKAGE_NAME}.session_gate")
    gate = gate_mod.SessionGate()
    delay_tasks: dict[str, asyncio.Task] = {}
    running_check_tasks: dict[str, asyncio.Task] = {}
    background_tasks: set[asyncio.Task] = set()

    def spawn(coro):
        return asyncio.create_task(coro)

    def check_session(umo, *, trigger, force, expected_generation):
        return "ok"

    instance = scheduler.SessionScheduler(
        settings=models.Settings.from_config({}),
        gate=gate,
        image_cache_dir=tmp_path / "image_cache",
        spawn=spawn,
        should_run=lambda: True,
        state_for=lambda umo: models.SessionState(),
        check_session=check_session,
        clear_event=lambda _umo, _active_at: None,
        drop_older_images=lambda cutoff: None,
        containers=models.SessionContainers(
            last_events={},
            last_event_at={},
            recent_image_events={},
            whitelist_runtime_umos={},
            delay_tasks=delay_tasks,
            running_check_tasks=running_check_tasks,
            background_tasks=background_tasks,
            sessions={},
        ),
    )
    return instance, gate, delay_tasks, background_tasks


def _done_task() -> asyncio.Task:
    task = asyncio.create_task(asyncio.sleep(0))
    task.cancel()
    return task


async def test_leak_warning_task_threshold(tmp_path: Path, caplog: object) -> None:
    """任务数超阈值发出告警（红灯：当前实现无告警逻辑）。"""
    scheduler, models, gate, delay_tasks, background_tasks = _new_scheduler(tmp_path)
    for i in range(models.LEAK_WARN_TASK_THRESHOLD + 1):
        delay_tasks[f"s{i}"] = _done_task()
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        scheduler.cleanup_events_if_needed()
    assert any("task" in r.message and "threshold" in r.message for r in caplog.records), (
        "超阈值未告警"
    )


async def test_leak_warning_session_threshold(tmp_path: Path, caplog: object) -> None:
    scheduler, models, gate, delay_tasks, background_tasks = _new_scheduler(tmp_path)
    for i in range(models.LEAK_WARN_SESSION_THRESHOLD):
        gate.advance(f"s{i}")
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        scheduler.cleanup_events_if_needed()
    assert any("session" in r.message and "threshold" in r.message for r in caplog.records), (
        "会话规模超阈值未告警"
    )


async def test_leak_warning_no_repeat_until_recovered(tmp_path: Path, caplog: object) -> None:
    """回落前不重复告警；回落后可再次告警。"""
    scheduler, models, gate, delay_tasks, background_tasks = _new_scheduler(tmp_path)
    for i in range(models.LEAK_WARN_TASK_THRESHOLD + 1):
        delay_tasks[f"s{i}"] = _done_task()
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        scheduler.cleanup_events_if_needed()
        assert sum("threshold" in r.message for r in caplog.records) == 1
        scheduler.last_cleanup_at = 0.0
        scheduler.cleanup_events_if_needed()
        assert sum("threshold" in r.message for r in caplog.records) == 1, "回落前重复告警"
        delay_tasks.clear()
        scheduler.last_cleanup_at = 0.0
        scheduler.cleanup_events_if_needed()
        assert sum("threshold" in r.message for r in caplog.records) == 1, (
            "回落清除标记后应可再告警"
        )
        delay_tasks["again"] = _done_task()
        for i in range(models.LEAK_WARN_TASK_THRESHOLD):
            delay_tasks[f"a{i}"] = _done_task()
        scheduler.last_cleanup_at = 0.0
        scheduler.cleanup_events_if_needed()
        assert sum("threshold" in r.message for r in caplog.records) == 2, "回落后未重新告警"


async def test_leak_warning_silent_below_threshold(tmp_path: Path, caplog: object) -> None:
    scheduler, models, gate, delay_tasks, background_tasks = _new_scheduler(tmp_path)
    delay_tasks["s0"] = _done_task()
    with caplog.at_level(logging.WARNING, logger="astrbot"):
        scheduler.cleanup_events_if_needed()
    assert not any("threshold" in r.message for r in caplog.records), "正常规模不应告警"
