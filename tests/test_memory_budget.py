"""每会话内存基准的公式锁定（ticket 12）。

把"拍脑袋常数"改为可证明的关系：图片索引张数上限与历史消息条数上限
由常量组合推导（公式与实测数据见 docs/MEMORY_BUDGET.md）。
"""

from __future__ import annotations

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
