"""会话代次与运行集闸门：每会话单调代次、运行中集合与并发锁。

从主插件类拆出的第一刀（0.9.0 结构目标）：数据与语义方法内聚于此，
主插件经 property 只读视图访问字段、经委托方法访问语义，测试引用面不变。
"""

import asyncio
import itertools
from collections.abc import Iterator
from types import MappingProxyType
from typing import Any


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
        return generation is None or self._session_generation.get(umo, 0) == generation

    def lock_for(self, umo: str) -> asyncio.Lock:
        return self._session_locks.setdefault(umo, asyncio.Lock())

    def mark_running(self, umo: str) -> None:
        self._running_sessions.add(umo)
        # 新运行周期开始：清掉上一周期的 release 状态，等待者重新挂起
        self._session_release.pop(umo, None)

    def unmark_running(self, umo: str) -> None:
        self._running_sessions.discard(umo)
        release = self._session_release.get(umo)
        if release is not None:
            release.set()

    def is_running(self, umo: str) -> bool:
        return umo in self._running_sessions

    def release_event(self, umo: str) -> asyncio.Event:
        """等待该会话当前运行结束的惰性事件（等完后再查 is_running）。"""
        return self._session_release.setdefault(umo, asyncio.Event())

    def snapshot(self) -> dict[str, Any]:
        """代次/运行集/锁三张表的浅拷贝快照，供配置回滚原地恢复。

        release 表**刻意不快照**：等待者持有具体 Event 对象，按值恢复会
        制造孤儿事件（永久挂起），按身份恢复又会带回陈旧的 set 状态
        （空转饿死）。正确来源是恢复后的运行集，由 ``restore`` 反推。
        """
        return {
            "generation": dict(self._session_generation),
            "running": set(self._running_sessions),
            "locks": dict(self._session_locks),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        """原地恢复三张表，并把 release 表校正到与恢复后运行集一致。

        必须原地 clear+update、禁止属性重绑定（契约 §11 B1）：等待者与
        运行中的 ``async with`` 持有的是容器与 Event/Lock 对象本身的引用，
        换掉容器身份会让它们继续读写孤儿表。

        release 表**不做整表恢复**：等待者持有的是具体 Event 对象，替换
        即制造孤儿（与锁对象同一约束）。改为从恢复后的运行集反推应有状态：
        回滚会把运行标记恢复成快照态，而支撑它的检查任务可能已经在
        ``_save_storage()`` 的 await 窗口内结束并 ``set()`` 过事件——此时
        若保留已 set 状态，``scheduler`` 的 ``while is_running`` 循环每轮
        立即返回，紧密空转独占事件循环（整个 bot 卡死）。
        """
        self._session_generation.clear()
        self._session_generation.update(snap["generation"])
        self._session_locks.clear()
        self._session_locks.update(snap["locks"])
        self._running_sessions.clear()
        self._running_sessions.update(snap["running"])
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
        """会话移出白名单后回收全部映射与运行标记。"""
        self._session_generation.pop(umo, None)
        self._session_locks.pop(umo, None)
        self._running_sessions.discard(umo)
        release = self._session_release.pop(umo, None)
        if release is not None:
            release.set()
