"""白名单变更管理。

负责白名单的增删与替换：内存替换（移出会话的失效、代次清理、会话状态
回收）、配置与状态的双写持久化，以及增删共用的单一回滚路径（双写失败 →
恢复内存 → 重写 → 仍失败则告警上抛）。

增删命令的权限校验与写操作取消语义保持在 main.py 的调用壳（锁与停止
检查），本模块只做白名单集合本身的变更。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from .models import PLUGIN_ID, Settings
from .utils import session_group_id, session_whitelisted, whitelist_storage_key


class WhitelistManager:
    """白名单集合的替换与增删，双写失败走单一回滚路径。

    会话内存回收契约（0.8.8 起）：被移出会话的完整回收由注入的 ``prune``
    回调承担——必须从与 ``sessions`` 同一 dict 弹掉 umo 与其群组键（生产
    注入 main._prune_session，含代次/裁决/sessions 单点回收）。本类只做
    白名单集合本身与双写回滚，不再自行 pop。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        sync_whitelist: Callable[[], bool],
        save_storage: Callable[[], Awaitable[None]],
        ensure_state: Callable[[str], Any],
        invalidate: Callable[[str], int],
        # 回收契约：须从 sessions 同一 dict 弹掉 umo 与群组键（见类 docstring）
        prune: Callable[[str], None],
        sessions: dict[str, Any],
        tracked_umos: Callable[[], set[str]],
        runtime_umos: dict[str, set[str]],
    ) -> None:
        self.settings = settings
        self._sync_whitelist = sync_whitelist
        self._save_storage = save_storage
        self._ensure_state = ensure_state
        self._invalidate = invalidate
        self._prune = prune
        self._sessions = sessions
        self._session_group_id = session_group_id
        self._tracked_umos = tracked_umos
        self._runtime_umos = runtime_umos

    def replace(self, whitelist: set[str]) -> dict[str, Any]:
        """整表替换白名单，并回收被移出会话的内存状态。

        返回被移出会话的 SessionState 快照（含群组键），供 ``commit_change``
        失败回滚时恢复（B2）：``_prune`` 是单向销毁、不幂等，第一次 replace
        已 pop 的会话状态若不快照，回滚后白名单回来了、配额与冷却却清零了。
        """
        normalized = {str(item).strip() for item in whitelist if str(item).strip()}
        tracked = set(self._tracked_umos())
        tracked.update(self._sessions)
        tracked.update(
            umo
            for values in self._runtime_umos.values()
            for umo in (values if isinstance(values, set) else {str(values)})
            if ":" in umo
        )
        self.settings.whitelist = normalized
        invalid_sessions = {
            umo for umo in tracked if umo and not session_whitelisted(umo, normalized)
        }
        pruned: dict[str, Any] = {}
        for umo in invalid_sessions:
            # 先快照再销毁：_prune 从 sessions 弹掉 umo 与群组键，回滚需要复活。
            group_key = self._session_group_id(umo)
            for key in (umo, group_key):
                if key in self._sessions:
                    pruned[key] = self._sessions[key]
            self._invalidate(umo)
            # 代次表按 UMO 累积且从不回收；移出白名单时清理内存（含会话锁
            # 与运行标记）。全局单调 token 保证即使会话重新加入，旧任务
            # 持有的旧 token 也必然失效。prune 同时唤醒仍在等待运行释放的
            # 挂起任务，由代次门使其退出，避免悬挂。
            self._prune(umo)
        for key, raw_values in list(self._runtime_umos.items()):
            values = raw_values if isinstance(raw_values, set) else {str(raw_values)}
            values = {
                value
                for value in values
                if value not in invalid_sessions and session_whitelisted(value, normalized)
            }
            if values:
                self._runtime_umos[key] = values
            else:
                self._runtime_umos.pop(key, None)
        return pruned

    async def commit_change(
        self, old_whitelist: set[str], label: str, pruned: dict[str, Any]
    ) -> None:
        """双写持久化一次变更；失败回滚内存并重写，仍失败则告警上抛。"""
        try:
            self._sync_whitelist()
            await self._save_storage()
        except Exception:
            self.replace(old_whitelist)
            # 恢复被 _prune 销毁的 SessionState（B2）：成功路径按契约回收，
            # 失败回滚必须复活，否则日配额/冷却时间戳被静默清零。
            self._sessions.update(pruned)
            try:
                self._sync_whitelist()
                await self._save_storage()
            except Exception as rollback_exc:
                logger.error(
                    "[%s] whitelist %s rollback persistence failed: %s",
                    PLUGIN_ID,
                    label,
                    rollback_exc,
                )
            raise

    async def add(self, umo: str) -> bool:
        """把当前会话加入白名单；返回是否为新加入（True）或已存在（False）。"""
        existed = session_whitelisted(umo, self.settings.whitelist)
        old_whitelist = set(self.settings.whitelist)
        pruned = self.replace(old_whitelist | {umo})
        self._ensure_state(whitelist_storage_key(umo))
        await self.commit_change(old_whitelist, "add", pruned)
        logger.info(
            "[%s] whitelist add session=%s existed=%s total=%d",
            PLUGIN_ID,
            umo,
            existed,
            len(self.settings.whitelist),
        )
        return not existed

    async def remove(self, umo: str) -> bool:
        """把当前会话（含其群组键）移出白名单；返回是否确实移出了（True）。"""
        existed = session_whitelisted(umo, self.settings.whitelist)
        old_whitelist = set(self.settings.whitelist)
        targets = {str(umo or "").strip()}
        group_id = self._session_group_id(umo)
        if group_id:
            targets.add(group_id)
        pruned = self.replace(old_whitelist - targets)
        await self.commit_change(old_whitelist, "remove", pruned)
        logger.info(
            "[%s] whitelist remove session=%s existed=%s total=%d",
            PLUGIN_ID,
            umo,
            existed,
            len(self.settings.whitelist),
        )
        return existed
