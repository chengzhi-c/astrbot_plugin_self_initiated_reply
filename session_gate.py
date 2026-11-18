"""会话代次与运行集闸门：每会话单调代次、运行中集合与并发锁。

数据与语义方法内聚于此：主插件经 property 只读视图访问字段、经委托方法访问语义。
"""

import asyncio
import itertools
from collections.abc import Callable, Iterator
from types import MappingProxyType
from typing import Any

from astrbot.api import logger

from .models import PLUGIN_ID, restore_container_inplace


class SessionGate:
    """维护每会话单调代次计数、运行中会话集合与并发锁。

    全局单调代次计数器：白名单移除/重加不会再产生 ABA，旧任务持有的
    token 永远小于会话当前 token，任何 check 点都会拒绝它。

    外部只读经 ``*_view`` property 访问（MappingProxyType / frozenset），
    写入一律走本类方法或 restore 整表覆盖，防误写绕过语义。
    """

    def __init__(self) -> None:
        self._generation_counter: Iterator[int] = itertools.count(1)
        self._session_generation: dict[str, int] = {}
        self._running_sessions: set[str] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_release: dict[str, asyncio.Event] = {}
        # 运行标记的租约持有人。运行标记描述的是"某个在途任务正在跑"，不是
        # 可回滚配置：``restore`` 靠它区分「检查已结束」（owner 已 pop，标记
        # 不得复活）与「检查被 prune 过但仍在跑」（owner 仍活，标记必须保留）。
        self._running_owners: dict[str, asyncio.Task[Any] | None] = {}

    @property
    def generation_view(self) -> MappingProxyType[str, int]:
        """代次表只读视图（实时映射，外部不可写）。"""
        return MappingProxyType(self._session_generation)

    @property
    def running_sessions_view(self) -> frozenset[str]:
        """运行中会话集合只读视图（快照）。"""
        return frozenset(self._running_sessions)

    @property
    def locks_view(self) -> MappingProxyType[str, asyncio.Lock]:
        """会话锁表只读视图（实时映射，外部不可写）。"""
        return MappingProxyType(self._session_locks)

    def advance(self, umo: str) -> int:
        generation = next(self._generation_counter)
        self._session_generation[umo] = generation
        return generation

    def current(self, umo: str) -> int:
        """当前会话代次（任务开始时的基线，用于防 ABA 的代次绑定）。"""
        return self._session_generation.get(umo, 0)

    def is_current(self, umo: str, generation: int | None) -> bool:
        """``generation`` 是否仍是该会话的当前代次。

        ``None`` = 显式放弃代次防护（永远判 current），仅供调用方已自证代次
        的路径使用；新增调用点必须传具体代次，不得以 ``None`` 走捷径。
        """
        return generation is None or self._session_generation.get(umo, 0) == generation

    def lock_for(self, umo: str) -> asyncio.Lock:
        return self._session_locks.setdefault(umo, asyncio.Lock())

    def mark_running(self, umo: str) -> None:
        self._running_sessions.add(umo)
        # owner 是运行标记的租约持有人，restore 靠它区分「检查已结束」与
        # 「检查被 prune 过但仍在跑」。拿不到 task（无事件循环的同步调用方）时
        # 记 None = 未知：推导时保守保留标记——误保留的代价由有界等待
        # 兜底，误丢弃的代价是该会话与在途检查真正并发。
        try:
            self._running_owners[umo] = asyncio.current_task()
        except RuntimeError:
            self._running_owners[umo] = None
        # 新运行周期开始：清掉上一周期的 release 状态，等待者重新挂起
        self._session_release.pop(umo, None)

    def transfer_running_owner(self, umo: str, owner: asyncio.Task[Any]) -> None:
        """把运行租约转交给覆盖记账窗口的任务。

        二次取消后外层检查任务随即 ``done()``，但记账任务仍在写账；不转交的话
        ``restore`` 会按 ``owner.done()`` 判"检查已结束"，把仍有记账任务的会话
        摘出运行集，新的检查就与它在途记账并发。
        """
        if umo in self._running_owners:
            self._running_owners[umo] = owner

    def unmark_running(self, umo: str, *, owner: asyncio.Task[Any] | None = None) -> None:
        if owner is not None and self._running_owners.get(umo) is not owner:
            return
        self._running_sessions.discard(umo)
        # owner 必须与标记同步消失：留着它会让 restore 把一个已结束的检查
        # 判成"仍在跑"，正是本机制要防的复活。
        self._running_owners.pop(umo, None)
        release = self._session_release.get(umo)
        if release is not None:
            release.set()

    def is_running(self, umo: str) -> bool:
        return umo in self._running_sessions

    def release_event(self, umo: str) -> asyncio.Event:
        """等待该会话当前运行结束的惰性事件（等完后再查 is_running）。"""
        return self._session_release.setdefault(umo, asyncio.Event())

    async def await_release(
        self,
        umo: str,
        *,
        trigger: str,
        timeout_sec: float,
        max_rounds: int,
        on_round_expired: Callable[[], bool] | None = None,
    ) -> bool:
        """有界等待该会话当前运行让出标记；成功释放或调用方放弃返回 True。

        ``max_rounds`` 轮都耗尽仍未释放返回 False，由调用方决定是告警丢弃还是
        静默继续（调度路径与手动指令路径的既有语义不同，日志分层见各自调用点）。

        ``on_round_expired`` 在每轮等待结束后调用：返回 False 表示调用方判定
        目标已失效（插件不再运行、代次已变），立即停止等待并返回 False；
        不传则只受运行标记与轮数约束。

        单把等待骨架收敛到此的意义：``force_cancel`` 是异步投递的，取消后旧
        任务要到下一个 await 点才 ``unmark_running``；两处调用点（调度路径与
        ``/selfreply check``）都要等同一件事，各自手写一遍必然漂移。
        """
        rounds = 0
        while self.is_running(umo):
            logger.debug(
                "[%s] wait for previous check to finish session=%s trigger=%s",
                PLUGIN_ID,
                umo,
                trigger,
            )
            event = self.release_event(umo)
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout_sec)
            except TimeoutError:
                pass
            if on_round_expired is not None and not on_round_expired():
                return False
            if not self.is_running(umo):
                return True
            rounds += 1
            if rounds >= max_rounds:
                return False
        return True

    def snapshot(self) -> dict[str, Any]:
        """锁表快照，供配置回滚恢复等待者持有的锁对象。

        运行集**刻意不进快照**：它是在途任务的租约，任务可能在任何时刻结束，
        快照值在回滚时已经过期。``restore`` 按 owner 存活推导：照抄快照会复活
        已结束检查的标记、也会抹掉窗口内新起的检查。

        release 表同样**刻意不快照**：等待者持有具体 Event 对象，按值恢复会制造
        孤儿事件或带回陈旧的 set 状态（空转饿死）。正确来源是恢复后的运行集，
        由 ``restore`` 反推。
        """
        return {"locks": dict(self._session_locks)}

    def restore(self, snap: dict[str, Any]) -> None:
        """恢复锁表并按 owner 存活推导运行集与 release 表。

        必须原地 clear+update、禁止属性重绑定：等待者与运行中的
        ``async with`` 持有容器与 Event/Lock 对象本身的引用，换掉容器身份会让
        它们继续读写孤儿表。代次不回滚，以免让已失效任务的旧 token 再次有效。

        运行集不照抄快照，改按 ``_running_owners`` 推导：owner 仍活着的会话
        才真的在跑。这同时挡住两个方向——
        - 窗口内已正常结束的检查（``unmark_running`` 已 pop owner）：照抄会把
          无人认领的标记写回去，该会话从此被"已有判断任务在运行"永久拒绝；
        - 窗口内新起、此刻仍在跑的检查（快照里没有它）：照抄会把它的标记抹掉，
          下一个检查就与它并发。

        release 表从推导后的运行集反推：仍运行的清掉陈旧的 set（否则等待循环
        紧密空转独占事件循环），已不运行的 set 唤醒等待者。
        """
        held_locks = {umo: lock for umo, lock in self._session_locks.items() if lock.locked()}
        restore_container_inplace(self._session_locks, snap["locks"])
        # 窗口内新起、此刻仍被持有的锁是活状态，不是可回滚配置：它在快照之后
        # 才由 lock_for 创建，整表恢复会把它抹掉而在途的 async with 仍持有它。
        for umo, lock in held_locks.items():
            self._session_locks[umo] = lock
        restore_container_inplace(
            self._running_sessions,
            {
                umo
                for umo, owner in self._running_owners.items()
                # owner 为 None 表示标记时拿不到 task（同步调用方）：无从判断，
                # 按保守方向保留。键不存在才是"检查已结束、owner 已随标记收走"。
                if owner is None or not owner.done()
            },
        )
        for umo, release in self._session_release.items():
            if umo in self._running_sessions:
                # 仍标记运行中：清掉陈旧的 set，让等待者重新挂起而非空转。
                # 若该会话的任务其实已结束，由 scheduler 侧的超时兜底与
                # 轮次上限把它降级为一次延迟/丢弃，而不是饿死事件循环。
                release.clear()
            else:
                # 恢复后不再运行：唤醒等待者，避免等一个不会到来的信号。
                release.set()

    def prune(self, umo: str) -> None:
        """会话移出白名单后回收全部映射与运行标记。

        ``_running_owners`` **刻意不摘**：prune 常发生在检查仍在跑的时候
        （白名单收缩的级联清理），摘掉 owner 会让随后的回滚把该会话判成
        "没有在途检查"，从而抹掉它的运行标记。owner 由检查自己的
        ``unmark_running`` 收尾，与运行标记同生同灭。
        """
        self._session_generation.pop(umo, None)
        # 仍被在途检查持有的锁**不摘**：摘走后重加白名单的新检查会拿到新锁
        # 对象，与旧检查真正并发。锁对象由该会话下一次未持锁的 prune 回收。
        held = self._session_locks.get(umo)
        if held is not None and not held.locked():
            self._session_locks.pop(umo, None)
        self._running_sessions.discard(umo)
        release = self._session_release.pop(umo, None)
        if release is not None:
            release.set()
