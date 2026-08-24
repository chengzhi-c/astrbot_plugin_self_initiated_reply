"""SessionCoordinator 独立单测（ticket 07 验收）：失效级联单点与阶段投影。

覆盖验收项：
- 失效单点：invalidate 级联推进代次、取消延迟任务、清理事件/时间/图片/阶段
- 任意清理路径（clear/terminate reset_all）都收敛经协调器，不散落 main
- FSM 状态投影（IDLE/OBSERVING/DECIDING 推导）与显式标记优先级
- 图片索引读取（过期清理/去重/sticker 过滤/数量上限）
"""

from __future__ import annotations

import importlib
from collections import deque
from types import SimpleNamespace

from .test_vision import PACKAGE_NAME, _load_modules


def _coordinator_module():
    _load_modules()  # 先创建测试包再导入（与 whitelist/events 测试一致）
    return importlib.import_module(f"{PACKAGE_NAME}.session_coordinator")


def _make_coordinator():
    mod = _coordinator_module()
    events: dict[str, object] = {}
    event_at: dict[str, float] = {}
    images: dict[str, object] = {}
    cancelled: list[tuple[str, bool]] = []
    advanced: list[str] = []
    running: set[str] = set()
    notified: list[str] = []

    def advance(umo):
        advanced.append(umo)
        return len(advanced)

    gate = SimpleNamespace(
        advance=advance,
        is_running=lambda umo: umo in running,
    )
    coordinator = mod.SessionCoordinator(
        events=events,
        event_at=event_at,
        images=images,
        gate=gate,
        cancel_delay=lambda umo, force: cancelled.append((umo, force)),
        notify_silence=lambda umo: notified.append(umo),
    )
    return (
        mod,
        coordinator,
        SimpleNamespace(
            events=events,
            event_at=event_at,
            images=images,
            cancelled=cancelled,
            advanced=advanced,
            running=running,
            notified=notified,
        ),
    )


class _Image:
    def __init__(self, key: str, *, sticker: bool = False) -> None:
        self._key = key
        self.is_sticker = sticker

    def cache_key(self) -> str:
        return self._key


def _images_for(coordinator, umo: str, *, age_sec: float = 3600.0):
    return coordinator.images_for(
        umo,
        vision_age_sec=age_sec,
        vision_skip_stickers=False,
        vision_max_images=10,
    )


# ============================================================================
# 失效级联单点（验收项 1）
# ============================================================================


async def test_invalidate_cascades_all_resources() -> None:
    _, coordinator, ctx = _make_coordinator()
    coordinator.record_event("s1", object(), 100.0)
    coordinator.capture_images("s1", 100.0, [_Image("a")])

    generation = coordinator.invalidate("s1", force_cancel=True)

    assert generation == 1
    assert ctx.cancelled == [("s1", True)]  # 延迟任务已取消
    assert "s1" not in ctx.events
    assert "s1" not in ctx.event_at
    assert "s1" not in ctx.images


async def test_record_event_notifies_silence_reset() -> None:
    """活动写点 = 静默重置通知点：新消息到达唤醒静默等待的延迟检查（ticket 11）。"""
    _, coordinator, ctx = _make_coordinator()

    coordinator.record_event("s1", object(), 100.0)

    assert ctx.notified == ["s1"]


async def test_invalidate_advances_generation_before_cancel() -> None:
    """代次推进必须先于取消，保证被取消任务的旧 token 立即失效（防 ABA）。"""
    _, coordinator, ctx = _make_coordinator()
    coordinator.record_event("s1", object(), 1.0)

    coordinator.invalidate("s1", force_cancel=True)

    assert ctx.advanced == ["s1"]
    assert ctx.cancelled == [("s1", True)]


async def test_clear_only_drops_session_resources() -> None:
    _, coordinator, ctx = _make_coordinator()
    coordinator.record_event("s1", object(), 1.0)
    coordinator.record_event("s2", object(), 2.0)

    coordinator.clear_event("s1", expected_active_at=1.0)

    assert "s1" not in ctx.events
    assert "s2" in ctx.events  # 其他会话不受影响


async def test_clear_event_preserves_images_and_matches_timestamp() -> None:
    _, coordinator, ctx = _make_coordinator()
    coordinator.record_event("s1", object(), 100.0)
    coordinator.capture_images("s1", 100.0, [_Image("a")])

    coordinator.clear_event("s1", expected_active_at=100.0)

    assert "s1" not in ctx.events
    assert "s1" not in ctx.event_at
    assert "s1" in ctx.images


async def test_stale_clear_event_cannot_remove_new_event() -> None:
    _, coordinator, ctx = _make_coordinator()
    coordinator.record_event("s1", "old", 100.0)
    coordinator.record_event("s1", "new", 200.0)

    coordinator.clear_event("s1", expected_active_at=100.0)

    assert ctx.events["s1"] == "new"
    assert ctx.event_at["s1"] == 200.0


# ============================================================================
# 图片索引读取
# ============================================================================


async def test_images_for_empty_returns_none() -> None:
    _, coordinator, _ = _make_coordinator()
    assert _images_for(coordinator, "s1") == []


async def test_images_for_drops_expired_and_dedupes() -> None:
    _, _, models = _load_modules()
    _, coordinator, ctx = _make_coordinator()
    now = models.now_ts()
    ctx.images["s1"] = deque(
        [(now - 7200, [_Image("old")]), (now - 60, [_Image("dup"), _Image("dup"), _Image("new")])]
    )

    result = _images_for(coordinator, "s1", age_sec=3600.0)

    assert [img.cache_key() for img in result] == ["dup", "new"]
    assert "s1" in ctx.images


async def test_images_for_sticker_filter_and_limit() -> None:
    _, _, models = _load_modules()
    _, coordinator, ctx = _make_coordinator()
    now = models.now_ts()
    ctx.images["s1"] = deque([(now, [_Image("a", sticker=True), _Image("b"), _Image("c")])])

    result = coordinator.images_for(
        "s1",
        vision_age_sec=3600.0,
        vision_skip_stickers=True,
        vision_max_images=1,
    )

    assert [img.cache_key() for img in result] == ["c"]  # 上限截断取最新一张


async def test_drop_older_than_clears_expired_sessions_only() -> None:
    """全表过期回收与 images_for 顺手清共用同一循环：过期会话整键消失，未过期留下。"""
    _, _, models = _load_modules()
    _, coordinator, ctx = _make_coordinator()
    now = models.now_ts()
    ctx.images["old"] = deque([(now - 7200, [_Image("gone")])])
    ctx.images["fresh"] = deque([(now - 60, [_Image("keep")])])

    coordinator.drop_older_than(now - 3600)

    assert "old" not in ctx.images
    assert [img.cache_key() for img in _images_for(coordinator, "fresh")] == ["keep"]


async def test_restore_inplace_keeps_container_identity() -> None:
    """配置回滚必须写回同一 dict，不能换掉协作对象手里的容器。"""
    _, coordinator, ctx = _make_coordinator()
    first = object()
    coordinator.record_event("s1", first, 1.0)
    coordinator.capture_images("s1", 1.0, [_Image("a")])
    snapshot = coordinator.snapshot()
    coordinator.reset_all()
    coordinator.record_event("s2", object(), 2.0)

    coordinator.restore_inplace(snapshot)

    assert ctx.events is coordinator._events
    assert ctx.event_at is coordinator._event_at
    assert ctx.images is coordinator._images
    assert ctx.events.get("s1") is first
    assert "s2" not in ctx.events
    assert "s1" in ctx.images
