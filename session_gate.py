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
        """代次/运行集/锁三张表的浅拷贝快照，供配置回滚整表恢复。"""
        return {
            "generation": dict(self._session_generation),
            "running": set(self._running_sessions),
            "locks": dict(self._session_locks),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        """整表替换为快照内容。运行中的 async with 持有旧锁对象引用，
        替换表项即可，无需深拷贝。"""
        self._session_generation = snap["generation"]
        self._running_sessions = snap["running"]
        self._session_locks = snap["locks"]

    def prune(self, umo: str) -> None:
        """会话移出白名单后回收全部映射与运行标记。"""
        self._session_generation.pop(umo, None)
        self._session_locks.pop(umo, None)
        self._running_sessions.discard(umo)
        release = self._session_release.pop(umo, None)
        if release is not None:
            release.set()
