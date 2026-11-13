"""资源泄漏回归测试：功能测试断言"做对了什么"，泄漏测试断言"什么都没剩下"。

- 多会话消息循环后取消收敛：后台任务/延迟任务/运行检查表/释放事件表必须回基线
- terminate 幂等：连续两次调用不抛、不卡
- 被放过的关键落盘任务必须放弃后续写入权限，不得覆盖新实例的状态

变异锚定（先红后绿）：
- ``_discard_delay_task`` 不 pop ``_delay_tasks`` → delay 表残留
- ``_delayed_check`` finally 不 pop ``_running_check_tasks`` → 运行检查表残留
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ._support import UMO, make_event, shorten_shutdown_windows
from .host_stubs import until, with_plugin


def test_multi_session_cancel_converges_all_tables(tmp_path: Path) -> None:
    """5 个会话各调度一个慢检查后全部取消，五张表必须回到基线。

        只取消会话级任务，常驻后台任务（image cleanup）
    不得被误杀。
    """

    async def scenario(plugin, main):
        # 启动图片清理是短命后台任务（rglob 不跑事件循环），等它退出后再取基线；
        # 常驻的只有 image cleanup 循环，基线恒为 1。
        await until(lambda: len(plugin._background_tasks) <= 1)
        baseline_tasks = len(plugin._background_tasks)
        assert baseline_tasks == 1  # 常驻 image cleanup 任务
        umos = [f"leak{i}:group:g" for i in range(5)]
        for umo in umos:
            plugin._last_events[umo] = make_event()
            plugin._scheduler.schedule_delayed_check(
                umo, delay_sec=1, trigger="message_delay", force=False
            )
        # 事件驱动等待任务注册完成：固定 sleep 在慢机器上会早于 spawn 断言，
        # 看到 5 个会话只注册了一部分（假红）。
        await until(lambda: len(plugin._delay_tasks) == 5)
        assert len(plugin._delay_tasks) == 5

        for umo in umos:
            plugin._scheduler.cancel_delay(umo, force=True)
        # 收敛条件即下方断言本身：取消后的 done 回调与门卫释放都由事件循环
        # 驱动，固定 sleep 在慢机器上会读到未收敛的中间态。delay 任务同时登记
        # 在 _background_tasks 里，故基线条数也要一起等。
        await until(
            lambda: (
                len(plugin._background_tasks) == baseline_tasks
                and not plugin._delay_tasks
                and not plugin._running_check_tasks
                and not plugin._gate._session_release
                and not plugin._gate.running_sessions_view
            )
        )

        assert len(plugin._background_tasks) == baseline_tasks
        assert not plugin._delay_tasks
        assert not plugin._running_check_tasks
        assert not plugin._gate._session_release  # release 表无公开视图，读侧断言残留即可
        assert not plugin._gate.running_sessions_view

    with_plugin(tmp_path, scenario)


def test_completed_checks_leave_no_running_check_residue(tmp_path: Path) -> None:
    """delay=0 的检查完整跑完后，运行检查表必须自清理。"""

    async def scenario(plugin, main):
        umos = [f"leak{i}:group:g" for i in range(5)]
        for umo in umos:
            plugin._last_events[umo] = make_event()
            plugin._scheduler.schedule_delayed_check(
                umo, delay_sec=0, trigger="message_delay", force=False
            )
        # 事件驱动等待自清理完成，替代固定 50×0.05s 轮询（flaky 修复）
        await until(lambda: not plugin._delay_tasks and not plugin._running_check_tasks)
        assert not plugin._delay_tasks
        assert not plugin._running_check_tasks

    with_plugin(tmp_path, scenario)


def test_terminate_is_idempotent(tmp_path: Path) -> None:
    """terminate 等待任务收尾、清空图片索引，且连续调用不抛不挂。"""

    async def scenario(plugin, main):
        finished = asyncio.Event()
        started = asyncio.Event()

        async def background() -> None:
            # 必须先真正跑起来再 terminate：任务还没跨过第一行就被 cancel 时，
            # 协程体从不进入 try，finally 不执行，finished 永不到位（固定 sleep
            # 只是在赌它已经启动）。事件化后这个前提由断言自己保证。
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                finished.set()

        plugin._last_events[UMO] = make_event()
        plugin._coordinator.capture_images(UMO, 1.0, [object()])
        plugin._track_background_task(background())
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=0, trigger="message_delay", force=False
        )
        await started.wait()

        await asyncio.wait_for(plugin.terminate(), timeout=2)
        await asyncio.wait_for(plugin.terminate(), timeout=2)

        assert plugin._stopping is True
        assert not plugin._delay_tasks
        assert not plugin._background_tasks
        assert not plugin._recent_image_events
        assert finished.is_set()

    with_plugin(tmp_path, scenario)


def test_spared_critical_task_abandons_later_state_writes(tmp_path: Path) -> None:
    """被放过的关键落盘任务必须放弃后续写入，但最终落盘必须照常写。

    缺陷形态：``_wait_background_tasks`` 对超出硬窗口的 critical task 只放过不
    取消，却没有相应的放弃机制。宿主不等待隔离任务就构造新实例，这个慢写随后
    落地会用陈旧快照覆盖新实例刚写出的 ``state.json``——与最终落盘超时是同一个
    hazard，只是入口不同。

    放弃判据按**任务身份**而不是全局标志：被豁免的任务自己放弃发布，而
    ``terminate()`` 里随后执行的最终落盘是另一个任务、必须照常写（它在
    ``terminate()`` 返回之前完成，不暴露在那个跨实例竞态里）。用全局标志会把
    最终落盘一起挡掉，静默丢掉「最后一次记录之后的内存变更」。

    变异锚定：删掉 ``_wait_background_tasks`` 里 critical 分支的
    ``self._spared_critical_tasks.add(task)``，或让 ``save_storage`` 的
    ``abandoned()`` 不再看该集合，本用例红。

    注意 ``release.set()`` 之前必须**再造一处内存变更**：被豁免任务的 payload
    若与磁盘逐字相同，``save_storage`` 的跳写判据会代为满足"没有发布"，放弃
    机制从未被执行——那条 mtime 断言绿得空洞（实测只删 spare 注册仍全绿）。
    """

    async def scenario(plugin, main):
        restore_windows = shorten_shutdown_windows(main)
        entered = asyncio.Event()
        release = asyncio.Event()
        state_path = plugin._storage_path
        # 构造期的启动写盘是同批后台任务：先等它落地，否则下面的 unlink 会与它
        # 竞争（平台同款理由见 test_main_runtime）。
        await until(state_path.exists)

        async def slow_persist() -> None:
            entered.set()
            await release.wait()
            # 唤醒后由本任务自己落盘：它已越过关停窗口被豁免，必须放弃发布
            await plugin._save_storage()

        critical = plugin._track_critical_task(slow_persist())
        await entered.wait()
        # 制造一处真实内存变更，最终落盘才有东西可写（跳写判据会挡掉空快照）
        plugin._state_for(UMO).last_active_at += 1.0
        try:
            # 删掉旧文件：这样 terminate() 里最终落盘是否发布可直接观察
            state_path.unlink(missing_ok=True)
            await plugin.terminate()
            assert state_path.exists(), (
                "terminate() 的最终落盘没有发布：被豁免任务把放弃判据传染给了它，"
                "关停静默丢掉「最后一次记录之后的内存变更」"
            )
            # 被豁免任务自己的慢写仍不得发布（覆盖新实例状态的那个 hazard）
            published_at = state_path.stat().st_mtime_ns
            # 再造一处变更：让被豁免任务的 payload 与磁盘不同。否则跳写判据
            # 会先返回，放弃机制根本走不到，mtime 断言恒真。
            plugin._state_for(UMO).last_active_at += 2.0
            release.set()
            await asyncio.wait_for(critical, timeout=1.0)
            await until(lambda: critical.done())
            assert state_path.stat().st_mtime_ns == published_at, (
                "被豁免任务的慢写落地了：会用陈旧快照覆盖新实例已写出的 state.json"
            )
        finally:
            release.set()
            if not critical.done():
                critical.cancel()
            await asyncio.wait_for(critical, timeout=1.0)
            restore_windows()

    with_plugin(tmp_path, scenario)
