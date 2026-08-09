"""WhitelistManager 独立单测（ticket 06 验收）：注入假持久化/失效回调，脱离插件实例。

覆盖验收项：
- add/remove 共用单一回滚实现（双写失败 → 恢复内存 → 重写 → 仍失败则告警上抛）
- add 触发会话状态创建、remove 移出群组键；返回 existed 语义
- replace 回收被移出会话的内存状态（invalidate/prune/sessions pop/runtime 过滤）
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from types import SimpleNamespace

from .host_stubs import capture_logs, messages_at_least
from .test_vision import PACKAGE_NAME, _load_modules


def _whitelist_module():
    return importlib.import_module(f"{PACKAGE_NAME}.whitelist")


class FakePersistence:
    """可注入的双写（sync 配置 + save 存储）失败模式。"""

    def __init__(self) -> None:
        self.sync_calls = 0
        self.save_calls = 0
        self.sync_fail = False
        self.save_fail = False

    def sync(self) -> bool:
        self.sync_calls += 1
        if self.sync_fail:
            raise RuntimeError("config write failed")
        return True

    async def save(self) -> None:
        self.save_calls += 1
        if self.save_fail:
            raise RuntimeError("storage write failed")


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
            # 0.8.8 起会话回收契约收敛到 prune 回调（与生产 main._prune_session
            # 语义一致：代次/裁决 + sessions 条目）；WhitelistManager 不再自行 pop。
            prune=lambda umo: (
                self.pruned.append(umo)
                or self.sessions.pop(umo, None)
                or self.sessions.pop(_group_key(umo), None)
            ),
            sessions=self.sessions,
            tracked_umos=lambda: set(self.tracked),
            runtime_umos=self.runtime_umos,
        )


def _umo_with_group() -> str:
    return "qq:GroupMessage:12345"


def _group_key(umo: str) -> str:
    return umo.split(":")[2]


# ============================================================================
# 验收项 1：add/remove 共用单一回滚（双写失败 → 恢复内存 → 重写 → 仍失败告警上抛）
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


async def test_add_existing_returns_false(tmp_path: Path) -> None:
    _, _, models = _load_modules()
    umo = _umo_with_group()
    ctx = FakeCtx(models, whitelist={umo})
    manager = ctx.make_manager()

    assert await manager.add(umo) is False
    assert ctx.settings.whitelist == {umo}
    assert ctx.persistence.save_calls == 1  # 幂等也走双写（配置内容不变）


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
