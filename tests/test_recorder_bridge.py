"""MessageRecorderBridge 分支。

覆盖 image/recorder_bridge.py 的全部分支：_ensure_api 的探测回退链、
get_local_image_path / resolve_relative_path / image_to_data_url 的
拒绝路径与成功路径（含 MIME 魔数校验）。
"""

from __future__ import annotations

import base64
import importlib
import inspect
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .host_stubs import ROOT, load_package

PACKAGE_NAME = "selfreply_recorder_test_package"

# 1x1 透明 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _load_bridge():
    return load_package(PACKAGE_NAME, "image.recorder_bridge")


@pytest.fixture(scope="module")
def bridge_mod():
    from .host_stubs import install_astrbot_stubs

    install_astrbot_stubs()
    return _load_bridge()


def _api_with(record=None, resolver=None, *, record_result=None):
    """构造 recorder API 桩。"""

    class FakeRecorderApi:
        async def get_by_platform_message_id(self, message_id):
            if record_result is not None:
                return record_result
            return record

        def get_media_absolute_path(self, value):
            if resolver is None:
                return None
            return resolver(value)

    return FakeRecorderApi()


def _context_with(api):
    class Meta:
        star_instance = type("Star", (), {"get_api": lambda self: api})()

    class FakeContext:
        def get_registered_star(self, name):
            return Meta()

    return FakeContext()


def _record_with(*images: dict[str, Any]) -> Any:
    class Record:
        def get_message_chain_list(self):
            return list(images)

    return Record()


# ============================================================================
# _ensure_api
# ============================================================================


def test_ensure_api_false_without_context(bridge_mod) -> None:
    bridge = bridge_mod.MessageRecorderBridge(None)
    assert bridge._ensure_api() is False
    assert bridge._api is None


def test_ensure_api_false_without_get_registered_star(bridge_mod) -> None:
    ctx = type("Ctx", (), {})()
    bridge = bridge_mod.MessageRecorderBridge(ctx)
    assert bridge._ensure_api() is False


def test_ensure_api_false_when_star_missing(bridge_mod) -> None:
    def get_registered_star(name):
        return None

    ctx = type("Ctx", (), {"get_registered_star": get_registered_star})()
    bridge = bridge_mod.MessageRecorderBridge(ctx)
    assert bridge._ensure_api() is False


def test_ensure_api_false_when_no_api_method(bridge_mod) -> None:
    class Meta:
        star_instance = type("Star", (), {})()

    def get_registered_star(name):
        return Meta()

    ctx = type("Ctx", (), {"get_registered_star": get_registered_star})()
    bridge = bridge_mod.MessageRecorderBridge(ctx)
    assert bridge._ensure_api() is False


def test_ensure_api_false_on_exception(bridge_mod) -> None:
    def get_registered_star(name):
        raise OSError("boom")

    ctx = type("Ctx", (), {"get_registered_star": get_registered_star})()
    bridge = bridge_mod.MessageRecorderBridge(ctx)
    assert bridge._ensure_api() is False


def test_ensure_api_false_when_get_api_not_callable(bridge_mod) -> None:
    """探测链：star 的 get_api 不可调用时必须判负（40 行分支）。"""

    class Meta:
        star_instance = type("Star", (), {"get_api": None})()

    class Ctx:
        def get_registered_star(self, name):
            return Meta()

    bridge = bridge_mod.MessageRecorderBridge(Ctx())
    assert bridge._ensure_api() is False


def test_ensure_api_caches_result(bridge_mod) -> None:
    api = _api_with(record=None)
    ctx = _context_with(api)
    bridge = bridge_mod.MessageRecorderBridge(ctx)
    assert bridge._ensure_api() is True
    assert bridge._api is api

    # 成功 API 仍正缓存：破坏探测源后二次调用不重新探测。
    def boom(name):
        raise OSError("probe must not rerun")

    ctx.get_registered_star = boom
    assert bridge._ensure_api() is True


def test_ensure_api_retries_after_initial_miss(bridge_mod) -> None:
    api = _api_with(record=None)

    class Context:
        available = False

        def get_registered_star(self, name):
            return _context_with(api).get_registered_star(name) if self.available else None

    context = Context()
    bridge = bridge_mod.MessageRecorderBridge(context)

    assert bridge._ensure_api() is False
    context.available = True
    assert bridge._ensure_api() is True
    assert bridge._api is api


# ============================================================================
# get_local_image_path
# ============================================================================


async def test_get_local_image_path_empty_message_id(bridge_mod) -> None:
    bridge = bridge_mod.MessageRecorderBridge(_context_with(_api_with(record=None)))
    assert await bridge.get_local_image_path("") is None


async def test_get_local_image_path_no_record(bridge_mod) -> None:
    bridge = bridge_mod.MessageRecorderBridge(_context_with(_api_with(record=None)))
    assert await bridge.get_local_image_path("m1") is None


async def test_get_local_image_path_record_without_images(bridge_mod) -> None:
    record = _record_with({"type": "text", "content": "hi"})
    bridge = bridge_mod.MessageRecorderBridge(_context_with(_api_with(record=record)))
    assert await bridge.get_local_image_path("m1") is None


async def test_get_local_image_path_no_local_path(bridge_mod) -> None:
    record = _record_with({"type": "image", "url": "u1", "local_path": ""})
    bridge = bridge_mod.MessageRecorderBridge(_context_with(_api_with(record=record)))
    assert await bridge.get_local_image_path("m1") is None


async def test_get_local_image_path_url_match(bridge_mod, tmp_path) -> None:
    target = tmp_path / "target.png"
    target.write_bytes(PNG_BYTES)

    def resolver(value):
        return str(target)

    record = _record_with(
        {"type": "image", "url": "other", "local_path": "ignored"},
        {"type": "image", "url": "u1", "local_path": str(target)},
    )
    bridge = bridge_mod.MessageRecorderBridge(
        _context_with(_api_with(record=record, resolver=resolver))
    )
    result = await bridge.get_local_image_path("m1", "u1")
    assert result == target


async def test_get_local_image_path_first_image_fallback(bridge_mod, tmp_path) -> None:
    """无匹配 URL 时取第一个 image 组件。"""

    target = tmp_path / "first.png"
    target.write_bytes(PNG_BYTES)

    def resolver(value):
        return str(target)

    record = _record_with({"type": "image", "url": "a", "local_path": str(target)})
    bridge = bridge_mod.MessageRecorderBridge(
        _context_with(_api_with(record=record, resolver=resolver))
    )
    result = await bridge.get_local_image_path("m1")
    assert result == target


async def test_get_local_image_path_resolution_failure(bridge_mod) -> None:
    record = _record_with({"type": "image", "url": "u1", "local_path": "missing.png"})
    bridge = bridge_mod.MessageRecorderBridge(
        _context_with(_api_with(record=record, resolver=lambda value: None))
    )
    assert await bridge.get_local_image_path("m1") is None


async def test_get_local_image_path_exception(bridge_mod) -> None:
    async def boom(message_id):
        raise OSError("db down")

    api = type("FakeRecorderApi", (), {"get_by_platform_message_id": boom})()
    bridge = bridge_mod.MessageRecorderBridge(_context_with(api))
    assert await bridge.get_local_image_path("m1") is None


# ============================================================================
# resolve_relative_path
# ============================================================================


async def test_resolve_relative_path_empty(bridge_mod) -> None:
    bridge = bridge_mod.MessageRecorderBridge(_context_with(_api_with(record=None)))
    assert bridge.resolve_relative_path("") is None


async def test_resolve_relative_path_without_resolver(bridge_mod) -> None:
    api = _api_with(record=None, resolver=None)
    bridge = bridge_mod.MessageRecorderBridge(_context_with(api))
    assert bridge.resolve_relative_path("x.png") is None


async def test_resolve_relative_path_missing_file(bridge_mod) -> None:
    api = _api_with(record=None, resolver=lambda value: str(ROOT / "no_such.png"))
    bridge = bridge_mod.MessageRecorderBridge(_context_with(api))
    assert bridge.resolve_relative_path("no_such.png") is None


async def test_resolve_relative_path_success(bridge_mod, tmp_path) -> None:
    target = tmp_path / "ok.png"
    target.write_bytes(PNG_BYTES)
    api = _api_with(record=None, resolver=lambda value: str(target))
    bridge = bridge_mod.MessageRecorderBridge(_context_with(api))
    assert bridge.resolve_relative_path("ok.png") == target


async def test_resolve_relative_path_exception(bridge_mod) -> None:
    def boom(value):
        raise OSError("boom")

    api = _api_with(record=None, resolver=boom)
    bridge = bridge_mod.MessageRecorderBridge(_context_with(api))
    assert bridge.resolve_relative_path("x.png") is None


def test_resolve_relative_path_resolver_not_callable(bridge_mod) -> None:
    """探测链：api 的 get_media_absolute_path 不可调用时判负（84 行分支）。"""

    class StubApi:
        get_media_absolute_path = None

    bridge = bridge_mod.MessageRecorderBridge(_context_with(StubApi()))
    assert bridge.resolve_relative_path("x.png") is None


# ============================================================================
# image_to_data_url
# ============================================================================


def test_image_to_data_url_missing_file(bridge_mod, tmp_path) -> None:
    assert bridge_mod.MessageRecorderBridge.image_to_data_url(tmp_path / "none.png") is None


def test_image_to_data_url_empty_or_oversized(bridge_mod, tmp_path) -> None:
    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    assert bridge_mod.MessageRecorderBridge.image_to_data_url(empty) is None

    huge = tmp_path / "huge.png"
    huge.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    assert bridge_mod.MessageRecorderBridge.image_to_data_url(huge) is None


def test_image_to_data_url_rejects_non_image(bridge_mod, tmp_path) -> None:
    text = tmp_path / "secret.txt"
    text.write_bytes(b"not an image at all")
    assert bridge_mod.MessageRecorderBridge.image_to_data_url(text) is None


def test_image_to_data_url_success(bridge_mod, tmp_path) -> None:
    img = tmp_path / "img.png"
    img.write_bytes(PNG_BYTES)
    data_url = bridge_mod.MessageRecorderBridge.image_to_data_url(img)
    assert data_url is not None
    assert data_url.startswith("data:image/png;base64,")
    assert data_url.split(",", 1)[1] == base64.b64encode(PNG_BYTES).decode("ascii")


def test_image_to_data_url_os_error(bridge_mod, tmp_path, monkeypatch) -> None:
    # read_bytes 抛 OSError（模拟不可读文件）必须真实触发 except 分支。
    # 目录路径会被 is_file() 先行拒绝（那是拒绝路径，不是异常路径），
    # 原测试名为 os_error 实为 is_file 分支，属假断言，已改为真实触发。
    target = tmp_path / "locked.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")

    def boom(self):
        raise OSError("access denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert bridge_mod.MessageRecorderBridge.image_to_data_url(target) is None


# ============================================================================
# maybe_await（0.8.8 起统一到 utils.maybe_await，删除 recorder_bridge 私有副本）
# ============================================================================


async def test_maybe_await_both_forms(bridge_mod) -> None:
    """async 与 sync 两种形式都能处理（unified 语义，非 hasattr 判定）。"""
    utils_mod = importlib.import_module(f"{PACKAGE_NAME}.utils")

    async def async_fn():
        return 1

    def sync_fn():
        return 2

    assert await utils_mod.maybe_await(async_fn()) == 1
    assert await utils_mod.maybe_await(sync_fn()) == 2


async def test_maybe_await_handles_generator_based_coroutines(bridge_mod) -> None:
    """generator-based coroutine（@types.coroutine）必须被 await。

    0.8.8 前 recorder_bridge 的私有 _maybe_await 用 hasattr(value, "__await__")
    判定，对 CO_ITERABLE_COROUTINE 生成器（inspect.isawaitable=True 但无
    __await__ 属性）会漏 await，直接返回生成器对象。统一到 utils.maybe_await
    后此场景必须正确。
    """
    utils_mod = importlib.import_module(f"{PACKAGE_NAME}.utils")

    @types.coroutine
    def gen_coro():
        return 42
        yield  # pragma: no cover - 使函数成为生成器

    assert inspect.isawaitable(gen_coro())
    assert not hasattr(gen_coro(), "__await__")
    assert await utils_mod.maybe_await(gen_coro()) == 42


def test_image_parser_recorder_bridge_is_scoped_to_plugin_context(bridge_mod, tmp_path) -> None:
    vision_runtime = importlib.import_module(f"{PACKAGE_NAME}.image.vision_runtime")

    def service(context, cache_name):
        return vision_runtime.VisionService(
            settings=SimpleNamespace(vision_enabled=True, vision_timeout_sec=20),
            bridge=object(),
            context=context,
            source_cache_dir=tmp_path / cache_name,
            data_root=tmp_path,
            coordinator=object(),
            gate=object(),
            is_stopping=lambda: False,
            track_background_task=lambda task: task.close(),
        )

    first_context = object()
    second_context = object()
    first = service(first_context, "a").get_image_parser()
    second = service(second_context, "b").get_image_parser()

    assert first is not None and second is not None
    assert first._recorder_bridge is not second._recorder_bridge
    assert first._recorder_bridge._context is first_context
    assert second._recorder_bridge._context is second_context
