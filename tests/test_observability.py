"""泄漏告警契约：后台任务数/代次表规模超阈值发出 warning，回落前不重复告警。

另含日志纪律契约（BEHAVIOR_CONTRACT §9）：判断结果必须落在 INFO——它是
§9 里唯一的非例外、非运维 INFO 条目，也是 README 排障步骤直接引用的字段。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path

from .host_stubs import capture_logs, install_astrbot_stubs, load_modules

PACKAGE_NAME = "selfreply_observability_test_package"


def _load_modules():
    return load_modules(PACKAGE_NAME, "scheduler", "models")


def _new_scheduler(tmp_path: Path):
    scheduler_mod, models = _load_modules()
    instance, gate, delay_tasks, background_tasks = _make_scheduler(tmp_path, scheduler_mod, models)
    return instance, scheduler_mod, models, gate, delay_tasks, background_tasks


def _make_scheduler(tmp_path: Path, scheduler, models):
    gate_mod = importlib.import_module(f"{PACKAGE_NAME}.session_gate")
    gate = gate_mod.SessionGate()
    delay_tasks: dict[str, asyncio.Task] = {}
    running_check_tasks: dict[str, asyncio.Task] = {}
    background_tasks: set[asyncio.Task] = set()

    def spawn(coro):
        return asyncio.create_task(coro)

    def check_session(
        umo, *, trigger, force, expected_generation, target_event=None, target_text=""
    ):
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
        delay_tasks=delay_tasks,
        running_check_tasks=running_check_tasks,
        background_tasks=background_tasks,
        last_events={},
        last_event_at={},
        last_decisions={},
        recent_image_events={},
        whitelist_runtime_umos={},
    )
    # 生产侧构造即起 1h 节流（SessionScheduler.__init__），这里清零让首轮清理立即触发。
    instance._last_cleanup = 0.0
    return instance, gate, delay_tasks, background_tasks


def _done_task() -> asyncio.Task:
    task = asyncio.create_task(asyncio.sleep(0))
    task.cancel()
    return task


def _restart_cleanup(instance) -> None:
    """让同一实例的下一轮清理重新触发：只跳过 1h 节流。

    刻意**不**动 ``_leak_warned``：回落去重是 ``_warn_leaks_if_needed`` 自己的职责
    （规模回落后它自己 discard），测试替它清就把被测行为绕过去了。
    """
    instance._last_cleanup = 0.0


def _threshold_records(caplog: object) -> list:
    return [record for record in caplog.records if "threshold" in record.getMessage()]


async def test_leak_warning_task_threshold(tmp_path: Path, caplog: object) -> None:
    """任务数超阈值发出**WARNING** 级告警。"""
    scheduler, scheduler_mod, models, gate, delay_tasks, background_tasks = _new_scheduler(tmp_path)
    for i in range(models.LEAK_WARN_TASK_THRESHOLD + 1):
        delay_tasks[f"s{i}"] = _done_task()
    with capture_logs(caplog, scheduler_mod.logger, logging.WARNING):
        scheduler.cleanup_events_if_needed()
    records = [
        r for r in caplog.records if "task" in r.getMessage() and "threshold" in r.getMessage()
    ]
    assert records, "超阈值未告警"
    assert all(r.levelno == logging.WARNING for r in records), (
        "泄漏告警必须落在 WARNING：BEHAVIOR_CONTRACT §9 把「不记则无从定位」的运维"
        "状态钉在这一级，降到 INFO 会被噪音淹没，升到 ERROR 会触发无关告警通道"
    )


async def test_leak_warning_session_threshold(tmp_path: Path, caplog: object) -> None:
    scheduler, scheduler_mod, models, gate, delay_tasks, background_tasks = _new_scheduler(tmp_path)
    for i in range(models.LEAK_WARN_SESSION_THRESHOLD):
        gate.advance(f"s{i}")
    with capture_logs(caplog, scheduler_mod.logger, logging.WARNING):
        scheduler.cleanup_events_if_needed()
    records = [
        r for r in caplog.records if "session" in r.getMessage() and "threshold" in r.getMessage()
    ]
    assert records, "会话规模超阈值未告警"
    assert all(r.levelno == logging.WARNING for r in records)


async def test_leak_warning_no_repeat_until_recovered(tmp_path: Path, caplog: object) -> None:
    """回落前不重复告警；回落后可再次告警。"""
    scheduler, scheduler_mod, models, gate, delay_tasks, background_tasks = _new_scheduler(tmp_path)
    for i in range(models.LEAK_WARN_TASK_THRESHOLD + 1):
        delay_tasks[f"s{i}"] = _done_task()
    with capture_logs(caplog, scheduler_mod.logger, logging.WARNING):
        scheduler.cleanup_events_if_needed()
        assert len(_threshold_records(caplog)) == 1
        _restart_cleanup(scheduler)
        scheduler.cleanup_events_if_needed()
        assert len(_threshold_records(caplog)) == 1, "回落前重复告警"
        delay_tasks.clear()
        _restart_cleanup(scheduler)
        scheduler.cleanup_events_if_needed()
        assert len(_threshold_records(caplog)) == 1, "回落清除标记后应可再告警"
        delay_tasks["again"] = _done_task()
        for i in range(models.LEAK_WARN_TASK_THRESHOLD):
            delay_tasks[f"a{i}"] = _done_task()
        _restart_cleanup(scheduler)
        scheduler.cleanup_events_if_needed()
        assert len(_threshold_records(caplog)) == 2, "回落后未重新告警"


async def test_leak_warning_silent_below_threshold(tmp_path: Path, caplog: object) -> None:
    scheduler, scheduler_mod, models, gate, delay_tasks, background_tasks = _new_scheduler(tmp_path)
    delay_tasks["s0"] = _done_task()
    with capture_logs(caplog, scheduler_mod.logger, logging.WARNING):
        scheduler.cleanup_events_if_needed()
    assert not any("threshold" in r.getMessage() for r in caplog.records), "正常规模不应告警"


async def test_decision_result_log_stays_at_info(tmp_path: Path, caplog: object) -> None:
    """判断结果必须落在 INFO：README 排障步骤直接引用 should_reply 与理由。

    BEHAVIOR_CONTRACT §9 把 INFO 限定为"异常与运维状态"，判断结果是其中唯一的
    例外。这个例外必须留痕：降到 DEBUG 会让「机器人为什么不接话」这个最高频问题
    失去默认可见性——而它恰好是用户最常来问的那个。契约原文已写明理由，本条测试
    是它的执行侧锚点。
    """
    install_astrbot_stubs()
    pipeline_mod = load_modules(PACKAGE_NAME, "session_pipeline", "models", "session_gate")[0]
    models = importlib.import_module(f"{PACKAGE_NAME}.models")
    gate_mod = importlib.import_module(f"{PACKAGE_NAME}.session_gate")

    class FakeDecision:
        async def decide(self, _umo, _state, *, trigger, force):
            return {"should_reply": True, "reason": "群友在聊有趣话题", "elapsed_sec": 0.5}

    pipeline = object.__new__(pipeline_mod.SessionPipeline)
    pipeline._decision = FakeDecision()
    pipeline._last_decisions = {}
    pipeline._gate = gate_mod.SessionGate()

    with capture_logs(caplog, pipeline_mod.logger, logging.DEBUG):
        result = await pipeline._decide_session_reply(
            "s1",
            models.SessionState(),
            trigger="message_delay",
            force=False,
            expected_generation=None,
        )

    assert isinstance(result, dict) and result["should_reply"] is True, "前提失效：判断未通过"
    records = [r for r in caplog.records if "decision session=" in r.getMessage()]
    assert records, "未打出判断结果日志"
    assert all(r.levelno == logging.INFO for r in records), (
        f"判断结果日志落在 {records[0].levelname}：README 排障步骤引用的 "
        "should_reply 与理由将从默认日志里消失"
    )
    assert "should_reply=True" in records[0].getMessage()
    assert "群友在聊有趣话题" in records[0].getMessage()
