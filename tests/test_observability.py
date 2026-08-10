"""可观测性契约（ticket 14）：日志分级纪律 + 泄漏告警 + 调试面板导出。

- 日志分级：INFO 仅允许异常与运维状态（高频成功路径必须 DEBUG）——
  新增 INFO 消息模板即红，白名单条目消失也红（防僵尸条目）
- 泄漏告警：后台任务数/代次表规模超阈值发出 warning，回落前不重复告警
- 调试面板：/status 导出代次、运行中集合、任务数、缓存规模、最近裁决原因
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import logging
from pathlib import Path

from .host_stubs import ROOT, install_astrbot_stubs, load_package

PACKAGE_NAME = "selfreply_observability_test_package"

# INFO 白名单（异常与运维状态语义）：新增 INFO 必须先在语义上站得住并
# 登记于此（注释标注场景）；条目消失表示代码已移除，同样红（提示清理）。
# 高频成功路径（生成成功/发送成功/巡检通过/清理例程）一律走 DEBUG。
_INFO_WHITELIST = {
    # 代次失效抑制：竞态异常路径，运维需要可见
    "[%s] suppress stale reply before hooks session=%s",
    "[%s] suppress stale reply after decorating hook session=%s",
    "[%s] suppress stale reply before event send session=%s",
    "[%s] suppress stale reply before context send session=%s",
    "[%s] suppress tool direct send session=%s reason=%s",
    "[%s] suppress duplicate final text after tool direct send session=%s",
    # 未确认/过期代次记录：异常状态
    "[%s] record unconfirmed proactive send session=%s (submission status unknown)",
    "[%s] record delivered stale generation without advancing observation session=%s",
    # 运维状态：资源清理结果
    "[%s] cleaned up %d expired frozen images",
    "[%s] cleaned up %d cached events (total: %d)",
    # 运维状态：白名单管理操作
    "[%s] whitelist add session=%s existed=%s total=%d",
    "[%s] whitelist remove session=%s existed=%s total=%d",
    # 运维状态：判断结果（用户要求可见，每会话检查收敛点，非逐条消息高频）
    "[%s] check result session=%s trigger=%s result=%s",
    # 运维状态：判断模型的最终裁决（0.9.5 用户要求可见）。与上一条同频——都在一次
    # check_session 的收敛点各打一次——但携带 should_reply / elapsed / reason 三个
    # 上一条没有的字段：上一条在「决定回复」时只报投递结果，看不到模型的理由。
    # 这是排查「插件为什么没说话／为什么说话了」唯一需要的一行。
    "[%s] decision session=%s trigger=%s should_reply=%s elapsed=%.2fs reason=%s",
    # 运维状态：启动/终止横幅
    "[%s] v%s enabled=%s whitelist=%d message_trigger=%s patrol_trigger=%s pipeline_mode=true",
    "[%s] vision judge=%s main=%s skip_stickers=%s provider=%s judge_provider=%s",
    "[%s] terminated",
}

_CHECKED_MODULES = [
    "adapters.py",
    "decision.py",
    "delivery.py",
    "generation.py",
    "main.py",
    "outbound.py",
    "scheduler.py",
    "session_coordinator.py",
    "session_gate.py",
    "storage.py",
    "whitelist.py",
]


def _info_templates(rel: str) -> set[str]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "info"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def test_info_logs_are_whitelisted_only() -> None:
    """新增 INFO 即红：全部 info 模板必须 ∈ 白名单。"""
    offenders: list[tuple[str, str]] = []
    for rel in _CHECKED_MODULES:
        for template in _info_templates(rel):
            if template not in _INFO_WHITELIST:
                offenders.append((rel, template))
    assert not offenders, "新增 INFO 未登记白名单：\n" + "\n".join(
        f"  {rel}: {t}" for rel, t in offenders
    )


def test_info_whitelist_has_no_zombie_entries() -> None:
    """白名单条目消失即红：每条登记必须在代码中真实存在。"""
    actual: set[str] = set()
    for rel in _CHECKED_MODULES:
        actual |= _info_templates(rel)
    zombies = sorted(_INFO_WHITELIST - actual)
    assert not zombies, "白名单僵尸条目（代码已移除但未清理）：\n" + "\n".join(zombies)


def _load_modules():
    install_astrbot_stubs()
    scheduler = load_package(PACKAGE_NAME, "scheduler")
    models = load_package(PACKAGE_NAME, "models")
    return scheduler, models


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
        task = asyncio.create_task(coro)
        return task

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
        clear_cached_event=lambda umo: None,
        last_events={},
        last_event_at={},
        recent_image_events={},
        whitelist_runtime_umos={},
        delay_tasks=delay_tasks,
        running_check_tasks=running_check_tasks,
        background_tasks=background_tasks,
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
