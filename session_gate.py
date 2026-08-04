"""会话代次与运行集闸门：每会话单调代次、运行中集合与并发锁。

从主插件类拆出的第一刀（0.9.0 结构目标）：数据与语义方法内聚于此，
主插件经 property 只读视图访问字段、经委托方法访问语义，测试引用面不变。
"""

import asyncio
import itertools
from collections.abc import Iterator


class SessionGate:
    """维护每会话单调代次计数、运行中会话集合与并发锁。

    全局单调代次计数器：白名单移除/重加不会再产生 ABA，旧任务持有的
    token 永远小于会话当前 token，任何 check 点都会拒绝它。
    """

    def __init__(self) -> None:
        self._generation_counter: Iterator[int] = itertools.count(1)
        self._session_generation: dict[str, int] = {}
        self._running_sessions: set[str] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}

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

    def unmark_running(self, umo: str) -> None:
        self._running_sessions.discard(umo)

    def is_running(self, umo: str) -> bool:
        return umo in self._running_sessions

    def prune(self, umo: str) -> None:
        """会话移出白名单后回收全部映射与运行标记。"""
        self._session_generation.pop(umo, None)
        self._session_locks.pop(umo, None)
        self._running_sessions.discard(umo)
