"""WhitelistManager 独立单测：注入假持久化/失效回调，脱离插件实例。

覆盖：
- add/remove 共用单一回滚实现（双写失败 → 恢复内存 → 重写 → 仍失败则告警上抛）
- add 触发会话状态创建、remove 移出群组键；返回 existed 语义
- replace 回收被移出会话的内存状态（invalidate/prune/sessions pop/runtime 过滤）
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._support import core_loader
from .host_stubs import capture_logs, messages_at_least

PACKAGE_NAME = "selfreply_whitelist_test_package"
_load_modules = core_loader(PACKAGE_NAME)


def _whitelist_module():
    return importlib.import_module(f"{PACKAGE_NAME}.whitelist")


class FakePersistence:
    """可注入的双写（sync 配置 + save 存储）失败模式。"""

    def __init__(self) -> None:
        self.sync_calls = 0
        self.save_calls = 0
        self.sync_fail = False
        self.save_fail = False
        # 取消只注入一次：回滚期的重写必须能正常完成，否则测的是「回滚也失败」
        # 那条既有路径，而非「取消也要回滚」本身。
        self.sync_cancel = False
        self.save_gate: asyncio.Event | None = None
        self.gate_waiters = 0

    async def sync(self) -> bool:
        self.sync_calls += 1
        if self.sync_cancel:
            self.sync_cancel = False
            raise asyncio.CancelledError()
        if self.sync_fail:
            raise RuntimeError("config write failed")
        return True

    async def save(self) -> None:
        self.save_calls += 1
        # 只让第一次写失败：回滚的重写要能正常完成，才测得到「回滚被取消打
        # 断」本身；回滚也失败的路径由 sync_fail 那个用例覆盖。
        if self.save_fail and self.save_calls == 1:
            raise RuntimeError("storage write failed")
        if self.save_gate is not None:
            self.gate_waiters += 1
            await self.save_gate.wait()


class FakeCtx:
    def __init__(self, models, whitelist: set[str] | None = None) -> None:
        self.invalidated: list[str] = []
        self.pruned: list[str] = []
        self.ensured: list[str] = []
        self.sessions: dict[str, object] = {}
        self.runtime_umos: dict[str, set[str]] = {}
        self.persistence = FakePersistence()
        self.settings = SimpleNamespace(
            whitelist=set(whitelist or []),
        )
        self.models = models
        self.tracked: set[str] = set()

    def make_manager(self):
        return _whitelist_module().WhitelistManager(
            settings=self.settings,
            sync_whitelist=self.persistence.sync,
            save_storage=self.persistence.save,
            ensure_state=lambda key: self.ensured.append(key) or self.sessions.setdefault(key, {}),
            invalidate=lambda umo: self.invalidated.append(umo) or 0,
            # 会话回收契约收敛到 prune 回调（与生产 main._prune_session
            # 语义一致：代次/裁决 + sessions 条目）；WhitelistManager 不再自行 pop。
            prune=lambda umo: (
                self.pruned.append(umo)
                or self.sessions.pop(umo, None)
                or self.sessions.pop(_group_key(umo), None)
            ),
            sessions=self.sessions,
            whitelist_runtime_umos=self.runtime_umos,
            tracked_umos=lambda: set(self.tracked),
        )


def _umo_with_group() -> str:
    return "qq:GroupMessage:12345"


def _group_key(umo: str) -> str:
    return umo.split(":")[2]


# ============================================================================
# add/remove 共用单一回滚（双写失败 → 恢复内存 → 重写 → 仍失败告警上抛）
# ============================================================================


async def test_add_rolls_back_in_memory_on_persist_failure(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    ctx = FakeCtx(models)
    manager = ctx.make_manager()
    ctx.persistence.save_fail = True

    try:
        await manager.add(_umo_with_group())
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    # 回滚：白名单恢复原状，重写调用发生（sync ×2 + save ×2）
    assert ctx.settings.whitelist == set()
    assert ctx.persistence.sync_calls == 2
    assert ctx.persistence.save_calls == 2


async def test_add_failure_rollback_leaves_no_session_residue(tmp_path: Path) -> None:
    """add 落盘失败回滚后不得残留 ensured 空状态（契约守卫）。

    该不变量由**间接机制**维持：``_ensure_state`` 在 commit 前执行，回滚
    路径 ``replace(old_whitelist)`` 的 prune 级联恰好把它回收。若未来有人
    改动 replace 的回滚（如 B2 修复把 prune 状态改为仅快照复活），这条
    级联断裂即静默残留，此用例锁住该关系。
    """
    _, _, models = _load_modules()
    umo = _umo_with_group()
    ctx = FakeCtx(models)
    manager = ctx.make_manager()
    ctx.persistence.save_fail = True

    try:
        await manager.add(umo)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    assert ctx.settings.whitelist == set()
    assert umo not in ctx.sessions, "回滚后 ensured 空状态残留"


async def test_remove_rolls_back_in_memory_on_persist_failure(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    umo = _umo_with_group()
    ctx = FakeCtx(models, whitelist={umo, _group_key(umo)})
    manager = ctx.make_manager()
    ctx.persistence.save_fail = True

    try:
        await manager.remove(umo)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    assert ctx.settings.whitelist == {umo, _group_key(umo)}
    assert ctx.persistence.sync_calls == 2
    assert ctx.persistence.save_calls == 2


async def test_rollback_persist_failure_logs_and_rethrows(tmp_path: Path, caplog: object) -> None:
    """回滚本身也失败：告警日志 + 原始异常继续上抛（不回滚静默吞掉）。

    留痕不可省：此时配置与状态文件的一致性已无法保证，若降级为 debug 或静默
    pass，线上只会看到一次普通的写失败，看不到"连回滚都没成功"。
    """
    _, _, models = _load_modules()
    ctx = FakeCtx(models)
    manager = ctx.make_manager()
    ctx.persistence.save_fail = True
    ctx.persistence.sync_fail = True  # 回滚时的重写也失败

    with capture_logs(caplog, _whitelist_module().logger, logging.ERROR):
        try:
            await manager.add(_umo_with_group())
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass

    errors = messages_at_least(caplog, logging.ERROR)
    assert any("rollback persistence failed" in msg for msg in errors), (
        f"回滚失败未记 error，双写不一致会静默逃逸：{errors}"
    )


async def test_add_success_writes_once_and_creates_state(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    umo = _umo_with_group()
    ctx = FakeCtx(models)
    manager = ctx.make_manager()

    added = await manager.add(umo)

    assert added is True
    assert ctx.settings.whitelist == {umo}
    assert ctx.persistence.sync_calls == 1
    assert ctx.persistence.save_calls == 1
    assert ctx.ensured == [umo]  # 会话状态已创建


async def test_add_applies_whitelist_spec_bounds(tmp_path: Path) -> None:
    """``add`` 必须走规格表的同一条规范化，不能自带一套宽松规则。

    缺陷形态：``replace`` 用 ``{str(item).strip() ...}`` 自行归一，``max_items``
    与 ``item_max_len`` 都只作用于 ``from_config`` 的读侧。于是
    ``/selfreply add`` 能把超长 UMO 或超量条目写进内存与磁盘，直到下次加载才被
    静默截断——超长 UMO 的会话在重启后莫名不再命中白名单，而内存态与磁盘态
    长期超出 ``MAX_WHITELIST_SIZE`` 这个"硬边界"。
    """
    _, _, models = _load_modules()
    ctx = FakeCtx(models)
    manager = ctx.make_manager()

    # 超长 UMO：写侧就该按 item_max_len 截断，与读侧同一表达式
    long_umo = "qq:GroupMessage:" + "x" * 400
    await manager.add(long_umo)
    assert ctx.settings.whitelist == {long_umo[: models.MAX_STRING_LIST_ITEM_LEN]}, (
        "add 未按 item_max_len 截断，重启后该会话将不再命中白名单"
    )

    # 条目含非法字符：写侧直接丢弃，不落盘
    ctx2 = FakeCtx(models)
    manager2 = ctx2.make_manager()
    await manager2.add('qq:GroupMessage:1"')
    assert ctx2.settings.whitelist == set(), "add 未丢弃含非法字符的条目"

    # 条目数上限：写侧就夹住，不等到下次加载
    ctx3 = FakeCtx(
        models, whitelist={f"qq:GroupMessage:{i}" for i in range(models.MAX_WHITELIST_SIZE)}
    )
    manager3 = ctx3.make_manager()
    await manager3.add("qq:GroupMessage:overflow")
    assert len(ctx3.settings.whitelist) == models.MAX_WHITELIST_SIZE, (
        "add 未夹住 max_items，内存与磁盘长期超出声明上限"
    )


async def test_add_at_capacity_refuses_instead_of_evicting(tmp_path: Path, caplog: object) -> None:
    """白名单满员时 add 必须显式拒绝，不能静默丢条目或挤掉一个正常会话。

    缺陷形态：``replace`` 的规范化是 ``sorted()[:max_items]``。满员后再 add，
    新条目字典序最大时被切掉而 add 仍回报 True——回显说「已将当前会话加入」，
    内存与磁盘里都没有，该会话此后每条消息都被静默丢弃；字典序居中时反过来把
    一个正常会话挤出去，它的日配额/冷却时间戳被清零、代次表项被删，表现为
    「该群主动回复莫名停了」。两种都只留一条不带条目名的计数 WARNING。

    变异锚定：删掉 ``add`` 里的容量守卫，本用例红。
    """
    _, _, models = _load_modules()
    full = {f"qq:GroupMessage:{i:04d}" for i in range(models.MAX_WHITELIST_SIZE)}
    whitelist_mod = _whitelist_module()
    # 字典序最大（截断时整体丢弃）与居中（截断时挤掉别人）两种排序位置
    for newcomer in ("qq:GroupMessage:zzzz", "qq:GroupMessage:0500x"):
        ctx = FakeCtx(models, whitelist=set(full))
        manager = ctx.make_manager()

        with capture_logs(caplog, whitelist_mod.logger, logging.WARNING):
            added = await manager.add(newcomer)

        assert added is False, f"{newcomer} 未加入却回报已加入"
        assert newcomer not in ctx.settings.whitelist
        # 关键：不得挤掉任何既有会话（含字典序最大的那条）
        assert ctx.settings.whitelist == full, "满员 add 挤掉了既有会话"
        assert not ctx.pruned and not ctx.invalidated, "拒绝路径销毁了会话状态"
        # 内存没变，磁盘也不该动
        assert ctx.persistence.sync_calls == 0
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("capacity reached" in message for message in warnings), (
            f"容量拒绝未留痕：{warnings}"
        )


async def test_add_existing_returns_false(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    umo = _umo_with_group()
    ctx = FakeCtx(models, whitelist={umo})
    manager = ctx.make_manager()

    assert await manager.add(umo) is False
    assert ctx.settings.whitelist == {umo}
    assert ctx.persistence.save_calls == 1  # 幂等也走双写（配置内容不变）


async def test_add_covered_by_bare_group_id_writes_nothing(tmp_path: Path) -> None:
    """被裸群号通配符覆盖时 add 不得追加完整 UMO 条目。

    缺陷形态：existed 用通配规则判定（裸群号覆盖完整 UMO），replace 却仍拿
    ``old | {umo}`` 落盘：指令回显「已在白名单中」，磁盘却多长一个条目，
    每群执行一次 add 就多一条，UI 计数与内存/磁盘三方不一致。
    """
    _, _, models = _load_modules()
    umo = _umo_with_group()
    group = _group_key(umo)
    ctx = FakeCtx(models, whitelist={group})
    manager = ctx.make_manager()

    assert await manager.add(umo) is False
    assert ctx.settings.whitelist == {group}
    assert ctx.persistence.sync_calls == 0
    assert ctx.persistence.save_calls == 0
    # 会话状态仍确保创建（与原路径一致）
    assert umo in ctx.ensured


async def test_remove_success_removes_group_key_and_returns_true(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    umo = _umo_with_group()
    group = _group_key(umo)
    ctx = FakeCtx(models, whitelist={umo, group})
    manager = ctx.make_manager()

    removed = await manager.remove(umo)

    assert removed is True
    assert ctx.settings.whitelist == set()


async def test_remove_missing_returns_false(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    ctx = FakeCtx(models)
    manager = ctx.make_manager()

    assert await manager.remove("qq:PrivateMessage:9") is False


# ============================================================================
# replace：被移出会话的内存回收
# ============================================================================


async def test_replace_prunes_and_invalidates_removed_sessions(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    umo = _umo_with_group()
    ctx = FakeCtx(models, whitelist={umo})
    ctx.sessions[umo] = {"recent": []}
    ctx.tracked.add(umo)
    manager = ctx.make_manager()

    manager.replace(set())

    assert ctx.settings.whitelist == set()
    assert ctx.invalidated == [umo]
    assert ctx.pruned == [umo]
    assert umo not in ctx.sessions


async def test_replace_filters_runtime_umos(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    umo = _umo_with_group()
    other = "qq:PrivateMessage:8"
    ctx = FakeCtx(models, whitelist={umo, other})
    ctx.runtime_umos["qq:GroupMessage:12345"] = {umo}
    ctx.runtime_umos["qq:PrivateMessage:8"] = {other}
    manager = ctx.make_manager()

    manager.replace({other})

    assert ctx.runtime_umos == {"qq:PrivateMessage:8": {other}}


async def test_remove_rollback_restores_pruned_session_state(tmp_path: Path) -> None:
    """B2 回归：remove 落盘失败回滚时，被 _prune 销毁的 SessionState 必须复活。

    缺陷链：replace 先经 _prune 单向销毁会话状态（sessions.pop），commit_change
    失败回滚只恢复白名单集合，SessionState 永不恢复 → 当日已回 N 次的配额与
    冷却时间戳被静默清零，配额与冷却被绕过。
    """
    _, _, models = _load_modules()
    umo = _umo_with_group()
    group = _group_key(umo)
    ctx = FakeCtx(models, whitelist={umo, group})
    # 预灌运行态：日配额 7 次 + 冷却时间戳 + 最近事件
    ctx.sessions[umo] = {"daily_count": 7, "last_proactive_at": 12345.0, "recent": [1]}
    manager = ctx.make_manager()
    ctx.persistence.save_fail = True

    try:
        await manager.remove(umo)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    # 白名单已回滚
    assert ctx.settings.whitelist == {umo, group}
    # SessionState 必须存活：配额/冷却/最近事件未被清零（修复前此处为 False）
    restored = ctx.sessions.get(umo)
    assert restored is not None, "回滚后 SessionState 被 _prune 销毁"
    assert restored["daily_count"] == 7
    assert restored["last_proactive_at"] == 12345.0
    assert restored["recent"] == [1]


async def test_remove_rollback_restores_runtime_umo_map(tmp_path: Path) -> None:
    """双写失败回滚必须恢复 ``_whitelist_runtime_umos``。

    缺陷形态：``replace`` 对被移出会话的映射是**整键 pop**（不是过滤后留空集）。
    回滚时再跑一遍 ``replace`` 也拿不回来——那时表已经空了。裸群号条目因此失去
    运行时 UMO，而 ``_runtime_umos_for_whitelist_item`` 对裸群号直接返回空集，
    巡检从此不再访问该群，直到下一条消息重新注册。全程无日志，表现为"该群的
    主动回复莫名停了"。

    变异锚定：删掉 ``commit_change`` 里 ``_runtime_umos`` 的原地恢复，本用例红。
    """
    _, _, models = _load_modules()
    umo = _umo_with_group()
    group = _group_key(umo)
    ctx = FakeCtx(models, whitelist={group})
    # 裸群号条目的运行期映射：消息入口为群号键与完整 UMO 键各注册一份
    ctx.runtime_umos[group] = {umo}
    ctx.runtime_umos[umo] = {umo}
    manager = ctx.make_manager()
    ctx.persistence.save_fail = True

    try:
        await manager.remove(umo)
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass

    assert ctx.settings.whitelist == {group}, "白名单未回滚"
    assert ctx.runtime_umos == {group: {umo}, umo: {umo}}, (
        "回滚后运行期 UMO 映射丢失：该群的巡检将不再被访问"
    )


async def test_remove_rollback_runs_when_persist_is_cancelled(tmp_path: Path) -> None:
    """B2 回归：双写被**取消**时同样要走回滚，而不是被 except Exception 跳过。

    缺陷链：``asyncio.CancelledError`` 派生自 ``BaseException``，旧的
    ``except Exception`` 整段跳过它，而 ``replace`` 早已单向前进（代次已推进、
    会话状态已摘下、磁盘未写）。于是 remove 看起来成功了——内存里会话已被移出，
    磁盘却仍写着它在白名单里，重启即复活；同时该会话的配额与冷却被清零。
    add 侧则相反：内存里会话已加入，磁盘没有，重启后不再命中白名单。

    变异锚定：把 ``commit_change`` 的 ``except BaseException`` 改回
    ``except Exception``，本用例红。
    """
    _, _, models = _load_modules()
    umo = _umo_with_group()
    group = _group_key(umo)
    ctx = FakeCtx(models, whitelist={umo, group})
    # 预灌运行态：日配额 7 次 + 冷却时间戳
    state = {"daily_count": 7, "last_proactive_at": 12345.0}
    ctx.sessions[umo] = state
    ctx.sessions[group] = {"daily_count": 7}
    ctx.runtime_umos[group] = {umo}
    manager = ctx.make_manager()
    ctx.persistence.sync_cancel = True

    with pytest.raises(asyncio.CancelledError):
        await manager.remove(umo)

    # 白名单已回滚
    assert ctx.settings.whitelist == {umo, group}, "取消后白名单未回滚"
    # SessionState 必须按**身份**复活：配额/冷却未被清零（B2）
    assert ctx.sessions[umo] is state, "取消后 SessionState 被 _prune 销毁"
    assert ctx.sessions[group] == {"daily_count": 7}
    assert ctx.runtime_umos == {group: {umo}}, "取消后运行期 UMO 映射未恢复"
    # 回滚自带重写：shield 保护的那次必须跑完，否则磁盘仍留着未应用的变更
    assert ctx.persistence.sync_calls == 2
    assert ctx.persistence.save_calls == 1


async def test_replace_returns_pruned_state_snapshot(tmp_path: Path) -> None:
    """B2 契约：replace 返回被移出会话的 SessionState 快照（供回滚复活）。"""
    _, _, models = _load_modules()
    umo = _umo_with_group()
    group = _group_key(umo)
    ctx = FakeCtx(models, whitelist={umo, group})
    ctx.sessions[umo] = {"daily_count": 3}
    ctx.sessions[group] = {"daily_count": 9}
    manager = ctx.make_manager()

    pruned = manager.replace(set())

    # 快照含 umo 与群组键两份状态；内存已按契约回收
    assert pruned[umo] == {"daily_count": 3}
    assert pruned[group] == {"daily_count": 9}
    assert umo not in ctx.sessions and group not in ctx.sessions


async def test_remove_rollback_finishes_when_the_caller_is_cancelled(tmp_path: Path) -> None:
    """B2 回归：普通失败的回滚被取消打断时仍要跑完，磁盘不能停在被驳回的白名单。

    缺陷形态：``except Exception: await rollback()``。``replace`` 早已单向前进
    （代次推进、会话状态摘下），回滚只来得及恢复内存——取消落在回滚的 await 上
    就跳出，磁盘仍是驳回后的白名单，重启后 ``load_config_data`` 直接把它复活
    （契约 §6），同时该会话的配额与冷却被清零（契约 §11 B2）。

    变异锚定：把 shield 等待循环改回裸 ``await rollback()``，本用例红。
    """
    _, _, models = _load_modules()
    umo = _umo_with_group()
    group = _group_key(umo)
    ctx = FakeCtx(models, whitelist={umo, group})
    manager = ctx.make_manager()
    ctx.persistence.save_fail = True
    gate = asyncio.Event()
    ctx.persistence.save_gate = gate

    task = asyncio.ensure_future(manager.remove(umo))
    for _ in range(500):
        if ctx.persistence.gate_waiters:
            break
        await asyncio.sleep(0.01)
    assert ctx.persistence.gate_waiters, "回滚的重写没有开始，前提不成立"

    task.cancel()
    # 取消送达后本调用不得先结束：结束就意味着回滚成了游离任务，等它跑完
    # 已经与调用方无关，磁盘上留下被驳回的白名单。
    await asyncio.sleep(0.05)
    assert not task.done(), "取消已送达就让 remove() 返回：回滚被放弃"
    gate.set()
    with pytest.raises(RuntimeError):
        await task

    assert ctx.persistence.save_calls == 2, "取消打断后回滚的重写没有跑完"
    assert ctx.settings.whitelist == {umo, group}, "回滚后内存白名单未恢复"
