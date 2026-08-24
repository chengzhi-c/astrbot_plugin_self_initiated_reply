"""每会话内存基准的公式锁定。

把"拍脑袋常数"改为可证明的关系：图片索引张数上限与历史消息条数上限
由常量组合推导（公式与实测数据见 docs/MEMORY_BUDGET.md）。
"""

from __future__ import annotations

import base64
from pathlib import Path

from .test_vision import PACKAGE_NAME, _load_modules


def _models_module():
    _load_modules()
    import importlib

    return importlib.import_module(f"{PACKAGE_NAME}.models")


def test_image_index_capacity_formula(tmp_path: Path) -> None:
    """每会话图片索引张数上限 = MAX_CACHED_IMAGE_EVENTS × MAX_VISION_IMAGES。"""
    models = _models_module()
    per_session_images = models.MAX_CACHED_IMAGE_EVENTS * models.MAX_VISION_IMAGES
    assert per_session_images == 20 * 5
    assert per_session_images >= models.Settings.from_config({}).vision_max_images * (
        models.MAX_CACHED_IMAGE_EVENTS
    )


def test_recent_history_capacity_formula(tmp_path: Path) -> None:
    """每会话历史消息条数上限 = MAX_RECENT_MESSAGE_LIMIT（recent deque maxlen 顶值）。"""
    models = _models_module()
    settings = models.Settings.from_config({})
    assert settings.recent_message_limit <= models.MAX_RECENT_MESSAGE_LIMIT
    assert settings.recent_message_limit >= 3  # 下限由 Settings 校验兜底


def test_disk_cache_budget_constant(tmp_path: Path) -> None:
    """磁盘冻结缓存有独立全局上限（内存驻留仅受张数公式约束）。"""
    models = _models_module()
    assert models.MAX_IMAGE_CACHE_BYTES > 0
    assert models.MAX_CACHED_IMAGE_EVENTS * models.MAX_VISION_IMAGES <= 100


def test_runtime_containers_honor_budget_caps(tmp_path: Path) -> None:
    """公式必须落到运行容器：历史 deque 与图片索引的 maxlen。"""
    from .host_stubs import with_plugin

    async def scenario(plugin, main):
        models = _models_module()
        state = plugin._state_for("fake:group:123")
        assert state.recent.maxlen == plugin.settings.recent_message_limit
        plugin._coordinator.capture_images("fake:group:123", 1.0, [])
        assert (
            plugin._recent_image_events["fake:group:123"].maxlen == models.MAX_CACHED_IMAGE_EVENTS
        )

    with_plugin(tmp_path, scenario)


class _PreparedImage:
    def __init__(self, payload: bytes) -> None:
        self.prepared_source = "data:image/png;base64," + base64.b64encode(payload).decode()

    def cache_key(self) -> str:
        return self.prepared_source


def test_in_memory_image_index_enforces_session_and_global_byte_budgets() -> None:
    import importlib
    from types import SimpleNamespace

    _load_modules()
    module = importlib.import_module(f"{PACKAGE_NAME}.session_coordinator")
    events: dict[str, object] = {}
    event_at: dict[str, float] = {}
    images: dict[str, object] = {}
    coordinator = module.SessionCoordinator(
        events=events,
        event_at=event_at,
        images=images,
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
    assert coordinator._memory_bytes_for("s1") == 3

    accepted = coordinator.capture_images("s2", 2.0, [_PreparedImage(b"789")])
    assert len(accepted) == 1
    assert coordinator._memory_bytes_for() == 3
    assert "s1" not in images
    assert "s2" in images


def test_image_budget_recomputes_global_bytes_after_session_eviction() -> None:
    import importlib
    from types import SimpleNamespace

    _load_modules()
    module = importlib.import_module(f"{PACKAGE_NAME}.session_coordinator")
    images: dict[str, object] = {}
    coordinator = module.SessionCoordinator(
        events={},
        event_at={},
        images=images,
        gate=SimpleNamespace(advance=lambda _umo: 1),
        cancel_delay=lambda _umo, _force: None,
        notify_silence=lambda _umo: None,
        max_image_memory_bytes=10,
        max_session_image_memory_bytes=5,
    )
    coordinator.capture_images("s1", 1.0, [_PreparedImage(b"12345")])
    coordinator.capture_images("s2", 1.0, [_PreparedImage(b"67890")])
    accepted = coordinator.capture_images("s1", 2.0, [_PreparedImage(b"abc")])

    assert len(accepted) == 1
    assert coordinator._memory_bytes_for() == 8
    assert "s2" in images
