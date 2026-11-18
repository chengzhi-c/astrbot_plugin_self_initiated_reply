"""SessionCoordinator 独立单测：失效级联单点与阶段投影。

覆盖：
- 失效单点：invalidate 级联推进代次、取消延迟任务、清理事件/时间/图片/阶段
- 任意清理路径（clear/terminate reset_all）都收敛经协调器，不散落 main
- FSM 状态投影（IDLE/OBSERVING/DECIDING 推导）与显式标记优先级
- 图片索引读取（过期清理/去重/sticker 过滤/数量上限）
"""

from __future__ import annotations

import importlib
from collections import deque
from types import SimpleNamespace

from ._support import core_loader

PACKAGE_NAME = "selfreply_coordinator_test_package"
_load_modules = core_loader(PACKAGE_NAME)


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
        last_events=events,
        last_event_at=event_at,
        recent_image_events=images,
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


def _bytes_for(module, images: dict, umo: str | None = None) -> int:
    """测试侧独立重算，与被测的增量记账对读提供独立预言。

    索引形状是 ``{umo: deque[(timestamp, [image, ...])]}``；这里全程重扫，
    与生产热路径的增量记账互为对照。生产类不再带这条读侧，其唯一消费者
    就是本文件。
    """
    events = images.items() if umo is None else [(umo, images.get(umo))]
    return sum(
        module._prepared_memory_size(image)
        for _key, image_events in events
        if image_events
        for _timestamp, batch in image_events
        for image in batch
    )


# ============================================================================
# 失效级联单点
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
    """活动写点 = 静默重置通知点：新消息到达唤醒静默等待的延迟检查。"""
    _, coordinator, ctx = _make_coordinator()

    coordinator.record_event("s1", object(), 100.0)

    assert ctx.notified == ["s1"]


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


class _PreparedImage:
    def __init__(self, payload: bytes) -> None:
        import base64

        self.prepared_source = "data:image/png;base64," + base64.b64encode(payload).decode()

    def cache_key(self) -> str:
        return self.prepared_source


def test_in_memory_image_index_enforces_session_and_global_byte_budgets() -> None:
    from types import SimpleNamespace

    module = _coordinator_module()
    images: dict[str, object] = {}
    coordinator = module.SessionCoordinator(
        last_events={},
        last_event_at={},
        recent_image_events=images,
        gate=SimpleNamespace(advance=lambda _umo: 1),
        cancel_delay=lambda _umo, _force: None,
        notify_silence=lambda _umo: None,
        max_image_memory_bytes=5,
        max_session_image_memory_bytes=4,
    )

    accepted = coordinator.capture_images(
        "s1", 1.0, [_PreparedImage(b"123"), _PreparedImage(b"456")]
    )
    assert len(accepted) == 1
    # 计费口径是索引实际驻留的 base64 负载（"MTIz" = 4 字节），不是解码后
    # 的 3 字节：理由见 session_coordinator._prepared_memory_size。
    assert _bytes_for(module, images, "s1") == 4

    accepted = coordinator.capture_images("s2", 2.0, [_PreparedImage(b"789")])
    assert len(accepted) == 1
    assert _bytes_for(module, images) == 4
    assert "s1" not in images
    assert "s2" in images


def test_image_budget_recomputes_global_bytes_after_session_eviction() -> None:
    from types import SimpleNamespace

    module = _coordinator_module()
    images: dict[str, object] = {}
    coordinator = module.SessionCoordinator(
        last_events={},
        last_event_at={},
        recent_image_events=images,
        gate=SimpleNamespace(advance=lambda _umo: 1),
        cancel_delay=lambda _umo, _force: None,
        notify_silence=lambda _umo: None,
        max_image_memory_bytes=20,
        max_session_image_memory_bytes=10,
    )
    coordinator.capture_images("s1", 1.0, [_PreparedImage(b"12345")])
    coordinator.capture_images("s2", 1.0, [_PreparedImage(b"67890")])
    accepted = coordinator.capture_images("s1", 2.0, [_PreparedImage(b"abc")])

    assert len(accepted) == 1
    # 负载按 base64 驻留量计费：12345/67890 各 8，abc 4；s1 追加后 12 > 10
    # 触发会话内驱逐，最终 4 + 8 = 12。
    assert _bytes_for(module, images) == 12
    assert "s2" in images


def _budget_coordinator(module, *, global_bytes: int, session_bytes: int):
    from types import SimpleNamespace

    images: dict[str, object] = {}
    coordinator = module.SessionCoordinator(
        last_events={},
        last_event_at={},
        recent_image_events=images,
        gate=SimpleNamespace(advance=lambda _umo: 1),
        cancel_delay=lambda _umo, _force: None,
        notify_silence=lambda _umo: None,
        max_image_memory_bytes=global_bytes,
        max_session_image_memory_bytes=session_bytes,
    )
    return coordinator, images


def test_zero_byte_placeholder_at_queue_head_does_not_stall_eviction() -> None:
    """空占位事件（0 字节，:172 刻意入队保时序）排在队首时不得终止驱逐。

    旧实现以 ``freed==0`` 作"无可回收"哨兵：弹出 0 字节条目即 break，
    其后真正占内存的 data: 事件无法驱逐，误判预算耗尽丢图。
    """
    module = _coordinator_module()
    coordinator, _images = _budget_coordinator(module, global_bytes=10, session_bytes=4)

    assert coordinator.capture_images("s1", 1.0, []) == []  # 空占位事件
    assert len(coordinator.capture_images("s1", 2.0, [_PreparedImage(b"123")])) == 1
    # 4 已占用 + 4 新图 > 4：必须先弹出 0 字节占位，再驱逐 4 字节事件才能接纳。
    accepted = coordinator.capture_images("s1", 3.0, [_PreparedImage(b"abc")])
    assert len(accepted) == 1


def test_zero_byte_frozen_event_at_queue_head_does_not_stall_global_eviction() -> None:
    """冻结到磁盘的图片按 0 字节记账；全局驱逐同样不得被队首 0 字节卡住。"""
    module = _coordinator_module()
    coordinator, _images = _budget_coordinator(module, global_bytes=6, session_bytes=6)

    assert coordinator.capture_images("s1", 1.0, []) == []
    assert len(coordinator.capture_images("s1", 2.0, [_PreparedImage(b"123")])) == 1
    assert len(coordinator.capture_images("s2", 3.0, [_PreparedImage(b"456")])) == 1
    # 全局 6 已满：s2 追加 4 字节需要驱逐全局最旧，先弹 0 字节占位再继续。
    accepted = coordinator.capture_images("s2", 4.0, [_PreparedImage(b"abc")])
    assert len(accepted) == 1


def test_deque_overflow_keeps_byte_counters_in_sync() -> None:
    module, coordinator, ctx = _make_coordinator()
    limit = module.MAX_CACHED_IMAGE_EVENTS

    for i in range(limit + 1):
        accepted = coordinator.capture_images("s1", float(i), [_PreparedImage(bytes([i % 256]))])
        assert len(accepted) == 1

    assert len(ctx.images["s1"]) == limit
    # 每张 1 字节载荷的 base64 驻留量是 4 字符（"AA=="），计费口径见
    # session_coordinator._prepared_memory_size。
    assert coordinator._total_bytes == _bytes_for(module, ctx.images) == 4 * limit


def test_overflow_keeps_the_index_sorted_and_counters_in_sync() -> None:
    """队列满时乱序到达：索引仍按时间有序，字节账与实际内容一致。

    缺陷形态：``_append_image_event`` 直接 ``append``。冻结是并发后台任务，
    一条时间戳更小的旧消息可能最后完成，插入到队尾会破坏
    ``drop_older_than`` 的 popleft 截断与 ``_evict_oldest_image_event`` 的
    「队首即最旧」前提；deque 的自动淘汰又按插入序摘，丢掉的是稍新条目。
    """
    module, coordinator, ctx = _make_coordinator()
    limit = module.MAX_CACHED_IMAGE_EVENTS
    for i in range(limit):
        coordinator.capture_images("s1", float(i + 1), [_PreparedImage(bytes([i % 256]))])

    # 时间戳比全队都小的旧消息最后完成冻结
    coordinator.capture_images("s1", 0.5, [_PreparedImage(b"late")])

    stamps = [timestamp for timestamp, _images in ctx.images["s1"]]
    assert len(stamps) == limit, f"队列长度失控：{len(stamps)}"
    assert stamps == sorted(stamps), f"索引未按时间有序：{stamps}"
    assert stamps[0] == 0.5, "新到达的条目未落到队首"
    assert coordinator._total_bytes == _bytes_for(module, ctx.images)


def test_reset_all_zeroes_image_byte_counters() -> None:
    module, coordinator, ctx = _make_coordinator()
    coordinator.capture_images("s1", 1.0, [_PreparedImage(b"123")])

    coordinator.reset_all()

    assert coordinator._total_bytes == 0
    assert _bytes_for(module, ctx.images) == 0


def test_clear_session_releases_image_byte_counters() -> None:
    module, coordinator, ctx = _make_coordinator()
    coordinator.capture_images("s1", 1.0, [_PreparedImage(b"123")])
    coordinator.capture_images("s2", 1.0, [_PreparedImage(b"45")])

    coordinator.clear_session("s1")

    assert coordinator._total_bytes == _bytes_for(module, ctx.images) == 4


def test_drop_older_than_releases_image_byte_counters() -> None:
    module, coordinator, ctx = _make_coordinator()
    coordinator.capture_images("s1", 1.0, [_PreparedImage(b"123")])
    coordinator.capture_images("s1", 100.0, [_PreparedImage(b"4567")])

    coordinator.drop_older_than(50.0, umo="s1")

    # 4567 的 base64 驻留 8 字符，123 的 4：丢弃旧事件后剩 8。
    assert coordinator._total_bytes == _bytes_for(module, ctx.images) == 8


def test_out_of_order_image_freeze_keeps_index_sorted() -> None:
    """冻结完成序与消息序倒置时，索引必须仍然按时间有序。

    缺陷形态：``_append_image_event`` 直接 append。图片冻结是并发后台任务
    （``vision_runtime._freeze_images``），后到的旧消息可以先完成并入队，于是
    ``drop_older_than`` 的 ``while events[0][0] < cutoff`` 在队首遇到较新条目就
    整体停止——其后所有过期事件永不回收，``_evict_oldest_image_event`` 也会把
    较新的事件当成"最旧"驱逐，``images_for`` 的 reversed 顺序失真。
    """
    module, coordinator, ctx = _make_coordinator()
    coordinator.capture_images("s1", 100.0, [_PreparedImage(b"new")])
    coordinator.capture_images("s1", 10.0, [_PreparedImage(b"old")])  # 完成序倒置

    stamps = [timestamp for timestamp, _images in ctx.images["s1"]]
    assert stamps == sorted(stamps), f"图片事件表未按时间有序：{stamps}"

    coordinator.drop_older_than(50.0, umo="s1")

    assert [timestamp for timestamp, _images in ctx.images["s1"]] == [100.0]
    assert coordinator._total_bytes == _bytes_for(module, ctx.images)
