"""DebouncedStateSaver 独立单测（ticket 12 验收）。

覆盖验收项：
- 连续多次记录（脏标记）只触发一次落盘（去重 + 合并写）
- 间隔触发：窗口静默后自动落盘
- flush 强制落盘（进程终止/插件重载路径），失败保持脏状态并自动重试
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .test_vision import PACKAGE_NAME, _load_modules


def _saver_module():
    _load_modules()  # 先创建测试包再导入
    import importlib

    return importlib.import_module(f"{PACKAGE_NAME}.state_saver")


def _make_saver(debounce_sec: float = 0.05, save: object | None = None):
    mod = _saver_module()
    calls: list[str] = []
    outcomes: list[bool] = []

    async def do_save() -> bool:
        calls.append("save")
        outcome = outcomes.pop(0) if outcomes else True
        if not outcome:
            raise OSError("disk full")
        return True

    saver = mod.DebouncedStateSaver(
        do_save=do_save,
        debounce_sec=debounce_sec,
    )
    return mod, saver, calls, outcomes


async def test_multiple_marks_schedule_single_task(tmp_path: Path) -> None:
    """窗口内连续多次置脏：不重建调度任务（去重），最终只落盘一次。"""
    _, saver, calls, _ = _make_saver()
    saver.mark_dirty()
    first_task = saver._task
    for _ in range(3):
        saver.mark_dirty()
        assert saver._task is first_task, "窗口内重复置脏不得重建调度任务"
    await saver.flush()
    assert calls == ["save"], "合并写：连续多次记录只触发一次落盘"


async def test_auto_flush_after_debounce_window(tmp_path: Path) -> None:
    """窗口静默后自动落盘（间隔触发，无需手动 flush）。"""
    _, saver, calls, _ = _make_saver(debounce_sec=0.05)
    saver.mark_dirty()
    await asyncio.sleep(0.2)
    assert calls == ["save"]
    assert not saver.pending


async def test_flush_pending_zeroes_and_saves(tmp_path: Path) -> None:
    """flush 强制落盘并清零脏标记。"""
    _, saver, calls, _ = _make_saver()
    saver.mark_dirty()
    ok = await saver.flush()
    assert ok is True
    assert calls == ["save"]
    assert not saver.pending


async def test_flush_failure_keeps_dirty_and_retries(tmp_path: Path) -> None:
    """落盘失败保持脏标记；窗口后自动重试成功。"""
    _, saver, calls, outcomes = _make_saver(debounce_sec=0.05)
    outcomes.append(False)  # 首次失败
    saver.mark_dirty()
    ok = await saver.flush()
    assert ok is False
    assert saver.pending is True, "失败必须保持脏状态"
    await asyncio.sleep(0.2)
    assert calls == ["save", "save"], "失败后窗口内自动重试"
    assert not saver.pending


async def test_mark_after_flush_reschedules(tmp_path: Path) -> None:
    """flush 后的新置脏重新调度，各自落盘一次。"""
    _, saver, calls, _ = _make_saver()
    saver.mark_dirty()
    await saver.flush()
    saver.mark_dirty()
    await saver.flush()
    assert calls == ["save", "save"]


async def test_cancel_then_flush_still_saves(tmp_path: Path) -> None:
    """调度任务被取消（终止路径）后 flush 仍强制落盘（最终落盘保证）。"""
    _, saver, calls, _ = _make_saver(debounce_sec=10.0)
    saver.mark_dirty()
    saver.cancel()
    assert saver.pending is True
    ok = await saver.flush()
    assert ok is True
    assert calls == ["save"]
