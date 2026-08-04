"""资源泄漏回归测试：功能测试断言"做对了什么"，泄漏测试断言"什么都没剩下"。

- 多会话消息循环后取消收敛：后台任务/延迟任务/运行检查表/释放事件表必须回基线
- terminate 幂等：连续两次调用不抛、不卡

变异锚定（先红后绿）：
- ``_discard_delay_task`` 不 pop ``_delay_tasks`` → delay 表残留
- ``_delayed_check`` finally 不 pop ``_running_check_tasks`` → 运行检查表残留
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .host_stubs import with_plugin
from .test_main_runtime import UMO, _make_event


def test_multi_session_cancel_converges_all_tables(tmp_path: Path) -> None:
    """5 个会话各调度一个慢检查后全部取消，五张表必须回到基线。

    只取消会话级任务（_cancel_delay_task），常驻后台任务（image cleanup）
    不得被误杀。
    """

    async def scenario(plugin, main):
        baseline_tasks = len(plugin._background_tasks)
        assert baseline_tasks == 1  # 常驻 image cleanup 任务
        umos = [f"leak{i}:group:g" for i in range(5)]
        for umo in umos:
            plugin._last_events[umo] = _make_event()
            plugin._schedule_delayed_check(
                umo, delay_sec=1, trigger="message_delay", force=False
            )
        await asyncio.sleep(0.05)
        assert len(plugin._delay_tasks) == 5

        for umo in umos:
            plugin._cancel_delay_task(umo, force=True)
        await asyncio.sleep(0.1)

        assert len(plugin._background_tasks) == baseline_tasks
        assert not plugin._delay_tasks
        assert not plugin._running_check_tasks
        assert not plugin._gate._session_release
        assert not plugin._gate._running_sessions

    with_plugin(tmp_path, scenario)


def test_completed_checks_leave_no_running_check_residue(tmp_path: Path) -> None:
    """delay=0 的检查完整跑完后，运行检查表必须自清理。"""

    async def scenario(plugin, main):
        umos = [f"leak{i}:group:g" for i in range(5)]
        for umo in umos:
            plugin._last_events[umo] = _make_event()
            plugin._schedule_delayed_check(
                umo, delay_sec=0, trigger="message_delay", force=False
            )
        for _ in range(50):
            if not plugin._delay_tasks:
                break
            await asyncio.sleep(0.05)
        assert not plugin._delay_tasks
        assert not plugin._running_check_tasks

    with_plugin(tmp_path, scenario)


def test_terminate_is_idempotent(tmp_path: Path) -> None:
    """连续两次 terminate：不抛、不卡（2s 超时包住防悬挂）。"""

    async def scenario(plugin, main):
        plugin._last_events[UMO] = _make_event()
        plugin._schedule_delayed_check(UMO, delay_sec=0, trigger="message_delay", force=False)
        await asyncio.sleep(0.05)

        await asyncio.wait_for(plugin.terminate(), timeout=2)
        await asyncio.wait_for(plugin.terminate(), timeout=2)

        assert plugin._stopping is True
        assert not plugin._delay_tasks
        assert not plugin._background_tasks

    with_plugin(tmp_path, scenario)
