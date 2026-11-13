"""白名单变更管理。

负责白名单的增删与替换：内存替换（移出会话的失效、代次清理、会话状态
回收）、配置与状态的双写持久化，以及增删共用的单一回滚路径（双写失败 →
恢复内存 → 重写 → 仍失败则告警上抛）。

增删命令的权限校验与写操作取消语义保持在 main.py 的调用壳（锁与停止
检查），本模块只做白名单集合本身的变更。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from .models import (
    CONFIG_SPEC_BY_KEY,
    MAX_WHITELIST_SIZE,
    PLUGIN_ID,
    Settings,
    normalize_string_list,
)
from .utils import await_rollback, is_full_umo, session_group_id, session_whitelisted


class WhitelistManager:
    """白名单集合的替换与增删，双写失败走单一回滚路径。

    会话内存回收契约：被移出会话的完整回收由注入的 ``prune`` 回调承担
    （生产注入 main._prune_session，含代次/裁决/sessions 单点回收），
    本类只做白名单集合本身与双写回滚，不再自行 pop。
    """

    def __init__(
        self,
        *,
        settings: Settings,
        sync_whitelist: Callable[[], Awaitable[bool]],
        save_storage: Callable[[], Awaitable[None]],
        ensure_state: Callable[[str], Any],
        invalidate: Callable[[str], int],
        # 回收契约：须从 sessions 同一 dict 弹掉 umo 与群组键（见类 docstring）
        prune: Callable[[str], None],
        sessions: dict[str, Any],
        whitelist_runtime_umos: dict[str, set[str]],
        tracked_umos: Callable[[], set[str]],
    ) -> None:
        self.settings = settings
        self._sync_whitelist = sync_whitelist
        self._save_storage = save_storage
        self._ensure_state = ensure_state
        self._invalidate = invalidate
        self._prune = prune
        self._sessions = sessions
        self._tracked_umos = tracked_umos
        self._runtime_umos = whitelist_runtime_umos

    def replace(self, whitelist: set[str]) -> dict[str, Any]:
        """整表替换白名单，并回收被移出会话的内存状态。

        返回被移出会话的 SessionState 快照（含群组键），供回滚时恢复：
        ``_prune`` 是单向销毁、不幂等，不快照则回滚后配额与冷却清零。
        """
        # 走规格表的同一条规范化：max_items / item_max_len / item_pattern 若只在
        # from_config 的读侧生效，``/selfreply add`` 就能把超长或超量的白名单写进
        # 内存与磁盘，直到下次加载才被静默截断——会话在重启后莫名失效。
        normalized = normalize_string_list(
            CONFIG_SPEC_BY_KEY["whitelist_sessions"], whitelist, mode="disk"
        )
        tracked = set(self._tracked_umos())
        tracked.update(self._sessions)
        tracked.update(
            umo for values in self._runtime_umos.values() for umo in values if is_full_umo(umo)
        )
        self.settings.whitelist = normalized
        invalid_sessions = {
            umo for umo in tracked if umo and not session_whitelisted(umo, normalized)
        }
        pruned: dict[str, Any] = {}
        for umo in invalid_sessions:
            # 先快照再销毁：_prune 从 sessions 弹掉 umo 与群组键，回滚需要复活。
            group_key = session_group_id(umo)
            for key in (umo, group_key):
                if key in self._sessions:
                    pruned[key] = self._sessions[key]
            self._invalidate(umo)
            # 代次表按 UMO 累积且从不回收；移出白名单时清理内存。全局单调
            # token 保证即使会话重新加入，旧任务持有的旧 token 也必然失效。
            self._prune(umo)
        for key, raw_values in list(self._runtime_umos.items()):
            values = {
                value
                for value in raw_values
                if value not in invalid_sessions and session_whitelisted(value, normalized)
            }
            if values:
                self._runtime_umos[key] = values
            else:
                self._runtime_umos.pop(key, None)
        return pruned

    def _snapshot_runtime_umos(self) -> dict[str, set[str]]:
        """运行期 UMO 映射的快照，供 ``commit_change`` 失败回滚原地恢复。

        与 ``pruned`` 完全同一个理由：``replace`` 对被移出会话的映射是**单向
        销毁**（整键 pop，不是过滤后留空集）。回滚时再跑一遍 ``replace`` 也拿
        不回来——那时表已经空了。缺了它，一次双写失败会让裸群号条目失去运行时
        UMO：``_runtime_umos_for_whitelist_item`` 对裸群号返回空集，巡检从此
        不再访问该群，而下一条消息才会重新注册。全程无日志，表现为"该群的主动
        回复莫名停了"。
        """
        return {key: set(values) for key, values in self._runtime_umos.items()}

    def _restore_memory(
        self,
        old_whitelist: set[str],
        pruned: dict[str, Any],
        runtime_umos: dict[str, set[str]],
    ) -> None:
        """把 ``replace`` 的单向销毁原地还原（add/remove 与 webapi 共用）。"""
        self.replace(old_whitelist)
        # 恢复被 _prune 销毁的 SessionState：失败回滚必须复活，
        # 否则日配额/冷却时间戳被静默清零。
        self._sessions.update(pruned)
        # 恢复被 replace 整键 pop 掉的运行期 UMO 映射。必须原地 clear+update
        # 而不是属性重绑定（契约 §11 B1）：scheduler 持同一 dict 引用，
        # 换容器会让它继续写孤儿表。
        self._runtime_umos.clear()
        self._runtime_umos.update(runtime_umos)

    async def commit_change(
        self,
        old_whitelist: set[str],
        label: str,
        pruned: dict[str, Any],
        runtime_umos: dict[str, set[str]],
    ) -> None:
        """双写持久化一次变更；失败回滚内存并重写，仍失败则告警上抛。"""

        async def rollback() -> None:
            self._restore_memory(old_whitelist, pruned, runtime_umos)
            try:
                await self._sync_whitelist()
                await self._save_storage()
            except Exception as rollback_exc:
                logger.error(
                    "[%s] whitelist %s rollback persistence failed: %s",
                    PLUGIN_ID,
                    label,
                    rollback_exc,
                )

        try:
            await self._sync_whitelist()
            await self._save_storage()
        except BaseException:
            # 取消与普通失败必须同一形状。CancelledError 不落 except Exception，
            # 而 replace 早已单向前进（代次已推进、会话状态已摘下、磁盘未写）：
            # 少了一次回滚，内存白名单与会话配额永久偏离磁盘（契约 §11 B2）。
            # 反过来，裸 await rollback() 又会被取消打断，磁盘上留下被驳回的
            # 白名单，重启后 load_config_data 直接把它复活（契约 §6）。
            #
            # 回滚必须**跑完**才算数：单次 shield 在取消瞬间就抛，rollback 成
            # 游离任务在 _config_lock 之外继续，能拿到锁并完整落盘的一次后续
            # add/remove 或 POST /config 随后被它用旧值撤销。
            #
            # restore.done() 是退出条件而不是 break：已被取消过一次之后每次
            # await 都可能立刻再抛 CancelledError，包括回滚已跑完的那一刻。
            await await_rollback(asyncio.ensure_future(rollback()))
            raise

    async def _commit_and_audit(
        self,
        *,
        label: str,
        umo: str,
        old_whitelist: set[str],
        pruned: dict[str, Any],
        runtime_umos: dict[str, set[str]],
        existed: bool,
    ) -> bool:
        """add/remove 共用尾段：双写持久化（失败回滚在 commit_change 内）+ 审计日志。"""
        await self.commit_change(old_whitelist, label, pruned, runtime_umos)
        logger.info(
            "[%s] whitelist %s session=%s existed=%s total=%d",
            PLUGIN_ID,
            label,
            umo,
            existed,
            len(self.settings.whitelist),
        )
        return existed

    async def add(self, umo: str) -> bool:
        """把当前会话加入白名单；返回是否为新加入（True）或已存在（False）。

        条目规则（长度/字符集/容量）由 ``replace`` 经 ``CONFIG_SPECS`` 统一执行，
        与磁盘加载同一表达式。长度与字符集越界时目标会被静默规范掉而本方法
        仍回报已加入——真实平台的 UMO 远低于 200 字且不含非法字符，不为这条
        不可达路径改指令回显契约。容量上限是另一回事：面板或反复 add 都能
        真实堆到 ``MAX_WHITELIST_SIZE``，故在下方显式拒绝（见该处注释）。
        """
        existed = session_whitelisted(umo, self.settings.whitelist)
        if existed and umo not in self.settings.whitelist:
            # 仅被同群号通配符覆盖（白名单里是裸群号、没有本 UMO 的精确条目）：
            # 再追加完整 UMO 是冗余写入——回显说「已在白名单中」，磁盘却多长
            # 一个条目，每群执行一次 add 就多一条，UI 计数与内存/磁盘不一致。
            # 精确条目已存在时仍走完整双写：那次写是幂等的自愈（磁盘被外部
            # 改动改坏时，add 把内存实况重新落盘）。
            self._ensure_state(umo)
            logger.info(
                "[%s] whitelist add session=%s existed=%s total=%d",
                PLUGIN_ID,
                umo,
                True,
                len(self.settings.whitelist),
            )
            return False
        old_whitelist = set(self.settings.whitelist)
        if not existed and len(old_whitelist) >= MAX_WHITELIST_SIZE:
            # replace 的规范化是 sorted()[:max_items]：满员后再 add，新条目
            # 字典序最大时被静默切掉（回显却说「已加入」），字典序居中时反过
            # 来把一个正常会话挤出去——它的日配额/冷却时间戳被清零、代次表项
            # 被删，表现为「该群主动回复莫名停了」。两种都只留一条不带条目名
            # 的计数 WARNING。显式拒绝优于静默挤掉一个正常会话。
            logger.warning(
                "[%s] whitelist add rejected: capacity reached session=%s total=%d limit=%d",
                PLUGIN_ID,
                umo,
                len(old_whitelist),
                MAX_WHITELIST_SIZE,
            )
            return False
        # 快照必须取在 replace **之前**：replace 会 pop 掉被移出会话的映射。
        runtime_umos = self._snapshot_runtime_umos()
        pruned = self.replace(old_whitelist | {umo})
        self._ensure_state(umo)
        return not await self._commit_and_audit(
            label="add",
            umo=umo,
            old_whitelist=old_whitelist,
            pruned=pruned,
            runtime_umos=runtime_umos,
            existed=existed,
        )

    async def remove(self, umo: str) -> bool:
        """把当前会话（含其群组键）移出白名单；返回是否确实移出了（True）。"""
        existed = session_whitelisted(umo, self.settings.whitelist)
        old_whitelist = set(self.settings.whitelist)
        targets = {str(umo or "").strip()}
        group_id = session_group_id(umo)
        if group_id:
            targets.add(group_id)
        # 同 add：快照取在 replace 之前，否则回滚时表已空。
        runtime_umos = self._snapshot_runtime_umos()
        pruned = self.replace(old_whitelist - targets)
        return await self._commit_and_audit(
            label="remove",
            umo=umo,
            old_whitelist=old_whitelist,
            pruned=pruned,
            runtime_umos=runtime_umos,
            existed=existed,
        )
