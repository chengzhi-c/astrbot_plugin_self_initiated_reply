"""图片解析链路覆盖补盲（ticket 09）：parser 错误分支与边界行为。

与 test_vision.py 复用同一宿主桩与包加载方式；新增用例只补分支，不改源。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_vision import PACKAGE_NAME, _load_modules

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
PNG_DIGEST = hashlib.sha256(PNG_BYTES).hexdigest()
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _parser_module():
    return sys.modules[f"{PACKAGE_NAME}.image.parser"]


# ============================================================================
# VISION_PROMPT_VERSION 变更门禁（0.8.8）
# ============================================================================


# 锚定对：(VISION_PROMPT_VERSION, 模板指纹)。prompt 模板改动必须同时 bump
# 版本号并更新本锚定；只改其一测试即红。指纹取自已提为模块常量的
# VISION_PROMPT_TEXT / VISION_SYSTEM_PROMPT_TEXT（真实模板单源），
# 防止缓存键（digest+version）复用旧描述。
# 更新流程：改模板常量 → 调 version → 用下方 hash 计算方式刷新指纹。
_VISION_PROMPT_ANCHOR = (
    "v1",
    "f96b9cedd3d09684e556d707fc112b69d5a38a8a102780a9cc66e17d3a3d0dde",
)


def test_vision_prompt_version_tracks_template() -> None:
    """prompt 模板与 VISION_PROMPT_VERSION 必须双改同步（缓存键语义守卫）。"""
    _load_modules()
    parser_mod = _parser_module()
    fingerprint = hashlib.sha256(
        (parser_mod.VISION_PROMPT_TEXT + parser_mod.VISION_SYSTEM_PROMPT_TEXT).encode("utf-8")
    ).hexdigest()
    assert (parser_mod.VISION_PROMPT_VERSION, fingerprint) == _VISION_PROMPT_ANCHOR, (
        "prompt 模板或 VISION_PROMPT_VERSION 已变更但未同步：模板改动必须"
        "bump VISION_PROMPT_VERSION（缓存键语义）并更新 _VISION_PROMPT_ANCHOR"
    )


def _make_parser(image, tmp_path: Path, **kwargs):
    return image.ImageParser(object(), source_cache_dir=tmp_path / "image_cache", **kwargs)


def _png_file(tmp_path: Path, name: str = "photo.png") -> Path:
    source = tmp_path / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(PNG_BYTES)
    return source


def _make_response(status_code: int = 200, headers: dict | None = None, chunks=()):
    async def aiter_bytes():
        for chunk in chunks:
            yield chunk

    return SimpleNamespace(
        status_code=status_code,
        headers=headers or {},
        aiter_bytes=aiter_bytes,
    )


# ============================================================================
# _host_all_global / _GlobalOnlyTransport（DNS 与传输层守卫）
# ============================================================================


def test_host_all_global_dns_resolution_branches(monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser_mod = _parser_module()

    monkeypatch.setattr(
        parser_mod.socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    assert parser_mod._host_all_global("public.example") is True

    monkeypatch.setattr(
        parser_mod.socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))],
    )
    assert parser_mod._host_all_global("private.example") is False

    monkeypatch.setattr(
        parser_mod.socket,
        "getaddrinfo",
        lambda *args: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0)),
        ],
    )
    assert parser_mod._host_all_global("mixed.example") is False

    def raise_oserror(*_args):
        raise OSError("nxdomain")

    monkeypatch.setattr(parser_mod.socket, "getaddrinfo", raise_oserror)
    assert parser_mod._host_all_global("nx.example") is False

    monkeypatch.setattr(
        parser_mod.socket,
        "getaddrinfo",
        lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 0))],
    )
    assert parser_mod._host_all_global("bad-addr.example") is False


def test_global_only_transport_forwards_safe_host_and_rejects_unsafe(monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser_mod = _parser_module()
    calls: list[tuple[str, object]] = []

    class Wrapped:
        async def handle_async_request(self, request: object) -> str:
            calls.append(("handle", request))
            return "ok"

        async def aclose(self) -> None:
            calls.append(("aclose", None))

    transport = parser_mod._GlobalOnlyTransport(Wrapped())
    request = SimpleNamespace(url=SimpleNamespace(host="8.8.8.8"))

    monkeypatch.setattr(parser_mod, "_host_all_global", lambda host: True)
    assert asyncio.run(transport.handle_async_request(request)) == "ok"
    assert calls == [("handle", request)]

    monkeypatch.setattr(parser_mod, "_host_all_global", lambda host: False)
    with pytest.raises(parser_mod.httpx.ConnectError):
        asyncio.run(transport.handle_async_request(request))
    assert len(calls) == 1, "非公网主机必须拒绝，不得转发"

    asyncio.run(transport.aclose())
    assert calls[-1] == ("aclose", None)


def test_image_parser_init_tolerates_unwritable_cache_dir(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    blocked = tmp_path / "occupied"
    blocked.write_text("not a directory")
    parser = image.ImageParser(object(), source_cache_dir=blocked)
    assert parser._source_cache_dir == blocked.resolve()


# ============================================================================
# prepare()：冻结分支
# ============================================================================


def test_prepare_no_source_is_false(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    assert asyncio.run(parser.prepare(image.ImageInfo())) is False


def test_prepare_already_frozen_is_true(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    info = image.ImageInfo(url="https://x/y.png")
    info.prepared_source = PNG_DATA_URL
    assert asyncio.run(parser.prepare(info)) is True


def test_prepare_unresolvable_source_is_false(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)

    async def no_source(_info):
        return ""

    parser._resolve_image_url = no_source
    info = image.ImageInfo(url="https://x/y.png")
    assert asyncio.run(parser.prepare(info)) is False


def test_prepare_data_url_without_cache_dir_stays_in_memory(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = image.ImageParser(object())

    async def data_url(_info):
        return PNG_DATA_URL

    parser._resolve_image_url = data_url
    info = image.ImageInfo(url="https://x/y.png")
    assert asyncio.run(parser.prepare(info)) is True
    assert info.prepared_source == PNG_DATA_URL


def test_prepare_refuses_unmaterialized_remote_url(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)

    async def raw_url(_info):
        return "https://cdn.example/x.png"

    parser._resolve_image_url = raw_url
    info = image.ImageInfo(url="https://x/y.png")
    assert asyncio.run(parser.prepare(info)) is False
    assert not info.prepared_source


def test_prepare_surfaces_resolve_exception_as_false(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)

    async def boom(_info):
        raise RuntimeError("boom")

    parser._resolve_image_url = boom
    info = image.ImageInfo(url="https://x/y.png")
    assert asyncio.run(parser.prepare(info)) is False


# ============================================================================
# _snapshot_local_source()：宿主临时文件快照分支
# ============================================================================


def test_snapshot_skips_prepared_source(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    info = image.ImageInfo(file_path=str(_png_file(tmp_path)), trusted_local_path=True)
    info.prepared_source = PNG_DATA_URL
    assert asyncio.run(parser._snapshot_local_source(info)) is True


def test_snapshot_requires_trusted_local_path(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    info = image.ImageInfo(file_path=str(_png_file(tmp_path)))
    assert asyncio.run(parser._snapshot_local_source(info)) is False


def test_snapshot_rejects_http_scheme_file_path(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    info = image.ImageInfo(file_path="https://cdn.example/x.png", trusted_local_path=True)
    assert asyncio.run(parser._snapshot_local_source(info)) is False


def test_snapshot_rejects_relative_path(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    info = image.ImageInfo(file_path="relative/photo.png", trusted_local_path=True)
    assert asyncio.run(parser._snapshot_local_source(info)) is False


def test_snapshot_no_data_url_falls_through(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    info = image.ImageInfo(file_path=str(_png_file(tmp_path)), trusted_local_path=True)
    monkeypatch.setattr(parser, "_file_to_data_url", lambda path, **kw: None)
    assert asyncio.run(parser._snapshot_local_source(info)) is False


def test_snapshot_materialize_failure_is_false(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    info = image.ImageInfo(file_path=str(_png_file(tmp_path)), trusted_local_path=True)
    monkeypatch.setattr(parser, "_file_to_data_url", lambda path, **kw: PNG_DATA_URL)
    monkeypatch.setattr(parser, "_materialize_data_url", lambda url: None)
    assert asyncio.run(parser._snapshot_local_source(info)) is False


def test_snapshot_exception_is_handled(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    info = image.ImageInfo(file_path=str(_png_file(tmp_path)), trusted_local_path=True)

    def raise_runtime(path: Path, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(parser, "_file_to_data_url", raise_runtime)
    assert asyncio.run(parser._snapshot_local_source(info)) is False


# ============================================================================
# prepare_batch() / parse_batch()：并发批处理保序
# ============================================================================


def test_prepare_batch_preserves_input_order(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)

    async def fake_resolve(_info):
        return PNG_DATA_URL

    parser._resolve_image_url = fake_resolve
    infos = [image.ImageInfo(url=f"https://x/{i}.png") for i in range(3)]
    results = asyncio.run(parser.prepare_batch(infos, max_concurrent=1))
    assert results == [True, True, True]
    assert all(info.prepared_source for info in infos)


def test_parse_batch_preserves_input_order_and_results() -> None:
    _, image, _ = _load_modules()
    payloads = [b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, b"\x89PNG\r\n\x1a\n" + b"\x01" * 32]
    encoded = [base64.b64encode(payload).decode("ascii") for payload in payloads]

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **kwargs):
            return SimpleNamespace(completion_text="desc:" + kwargs["image_urls"][0][-6:])

    parser = image.ImageParser(Bridge(), provider_id="p")

    async def fake_resolve(info):
        index = int(info.url[-5])
        return "data:image/png;base64," + encoded[index]

    parser._resolve_image_url = fake_resolve
    infos = [image.ImageInfo(url=f"https://x/{i}.png") for i in range(2)]
    results = asyncio.run(parser.parse_batch(infos, umo="s", max_concurrent=1))
    assert results == ["desc:" + encoded[0][-6:], "desc:" + encoded[1][-6:]]


# ============================================================================
# parse()：降级与截断分支
# ============================================================================


def test_parse_no_source_returns_none(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    assert asyncio.run(parser.parse(image.ImageInfo())) is None


def test_parse_no_provider_available_returns_none(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    class Bridge:
        async def resolve_provider_id(self, _umo, _preferred):
            return ""

    parser = image.ImageParser(Bridge(), provider_id="")
    info = image.ImageInfo(url="https://x/y.png")
    assert asyncio.run(parser.parse(info, umo="s")) is None


def _parse_with_bridge(image, bridge, info):
    parser = image.ImageParser(bridge, provider_id="p")

    async def fake_resolve(_info):
        return PNG_DATA_URL

    parser._resolve_image_url = fake_resolve
    return asyncio.run(parser.parse(info, umo="s"))


def test_parse_empty_description_not_cached(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    class Bridge:
        def __init__(self):
            self.calls = 0

        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(completion_text="")

    bridge = Bridge()
    info = image.ImageInfo(url="https://x/y.png")
    assert _parse_with_bridge(image, bridge, info) is None
    assert _parse_with_bridge(image, bridge, info) is None
    assert bridge.calls == 2, "空描述不得写缓存"


def test_parse_unable_description_not_cached(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            return SimpleNamespace(completion_text="无法识别这张图片的内容")

    info = image.ImageInfo(url="https://x/y.png")
    assert _parse_with_bridge(image, Bridge(), info) is None


def test_parse_truncates_long_description(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            return SimpleNamespace(completion_text="字" * 350)

    info = image.ImageInfo(url="https://x/y.png")
    result = _parse_with_bridge(image, Bridge(), info)
    assert result is not None
    assert len(result) <= 303 and result.endswith("...")


def test_parse_timeout_returns_none(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            raise asyncio.TimeoutError()

    info = image.ImageInfo(url="https://x/y.png")
    assert _parse_with_bridge(image, Bridge(), info) is None


def test_parse_provider_exception_returns_none(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            raise ValueError("provider down")

    info = image.ImageInfo(url="https://x/y.png")
    assert _parse_with_bridge(image, Bridge(), info) is None


def test_parse_falls_back_to_result_chain_plain_text(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            return SimpleNamespace(
                completion_text="",
                result_chain=SimpleNamespace(get_plain_text=lambda: " 来自chain "),
            )

    info = image.ImageInfo(url="https://x/y.png")
    assert _parse_with_bridge(image, Bridge(), info) == "来自chain"


def test_parse_result_chain_getter_failure_returns_none(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    def broken_getter():
        raise RuntimeError("chain broken")

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            return SimpleNamespace(
                completion_text="",
                result_chain=SimpleNamespace(get_plain_text=broken_getter),
            )

    info = image.ImageInfo(url="https://x/y.png")
    assert _parse_with_bridge(image, Bridge(), info) is None


# ============================================================================
# cleanup_source_cache()：清理守卫与异常降级
# ============================================================================


def test_cleanup_none_root_returns_zero() -> None:
    _, image, _ = _load_modules()
    assert image.ImageParser.cleanup_source_cache(None) == 0


def test_cleanup_missing_root_returns_zero(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    assert image.ImageParser.cleanup_source_cache(tmp_path / "nope") == 0


def test_cleanup_invalid_max_age_returns_zero(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    assert image.ImageParser.cleanup_source_cache(root, max_age_sec="bad") == 0


def test_cleanup_ignores_data_url_and_outside_protected(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    expired = root / "expired.png"
    expired.write_bytes(b"old")
    os.utime(expired, (100.0, 100.0))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")

    removed = image.ImageParser.cleanup_source_cache(
        root,
        protected_sources={"data:image/png;base64,AA==", str(outside)},
        max_age_sec=60,
        now=5000.0,
    )
    assert removed == 1
    assert not expired.exists()


def test_cleanup_skips_symlinks(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    target = tmp_path / "real.png"
    target.write_bytes(b"x")
    os.utime(target, (100.0, 100.0))
    link = root / "link.png"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return
    os.utime(link, (100.0, 100.0))

    removed = image.ImageParser.cleanup_source_cache(root, max_age_sec=60, now=5000.0)
    assert removed == 0
    assert target.exists()


def test_cleanup_unlink_failure_is_ignored(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    expired = root / "expired.png"
    expired.write_bytes(b"old")
    os.utime(expired, (100.0, 100.0))
    original_unlink = Path.unlink

    def blocked_unlink(self, *args, **kwargs):
        if self.name == "expired.png":
            raise OSError("permission denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    assert image.ImageParser.cleanup_source_cache(root, max_age_sec=60, now=5000.0) == 0


def test_cleanup_invalid_quota_falls_back_to_age_only(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    expired = root / "e.png"
    expired.write_bytes(b"x")
    os.utime(expired, (100.0, 100.0))
    fresh = root / "f.png"
    fresh.write_bytes(b"y")
    os.utime(fresh, (5000.0, 5000.0))
    removed = image.ImageParser.cleanup_source_cache(
        root, max_age_sec=60, max_total_bytes="bad", now=5000.0
    )
    assert removed == 1
    assert not expired.exists()
    # 若 "bad" 被误当配额 0，配额分支会连新鲜文件一起删 → 断言红
    assert fresh.exists()


def test_cleanup_quota_skips_protected_when_nothing_else_left(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    protected = root / "p.png"
    protected.write_bytes(b"1234")
    os.utime(protected, (100.0, 100.0))
    removed = image.ImageParser.cleanup_source_cache(
        root,
        protected_sources={str(protected)},
        max_age_sec=100000.0,
        max_total_bytes=2,
        now=5000.0,
    )
    assert removed == 0
    assert protected.exists()


def test_cleanup_quota_unlink_failure_is_ignored(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    old = root / "old.png"
    old.write_bytes(b"1234")
    os.utime(old, (100.0, 100.0))
    original_unlink = Path.unlink

    def blocked_unlink(self, *args, **kwargs):
        if self.name == "old.png":
            raise OSError("busy")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    removed = image.ImageParser.cleanup_source_cache(
        root, max_age_sec=100000.0, max_total_bytes=0, now=5000.0
    )
    assert removed == 0
    assert old.exists()


def test_cleanup_quota_stat_failure_is_ignored(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    old = root / "old.png"
    old.write_bytes(b"1234")
    os.utime(old, (100.0, 100.0))
    original_stat = Path.stat

    def blocked_stat(self, *args, **kwargs):
        if self.name == "old.png":
            raise OSError("gone")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", blocked_stat)
    removed = image.ImageParser.cleanup_source_cache(
        root, max_age_sec=60, max_total_bytes=0, now=5000.0
    )
    assert removed == 0


def test_cleanup_expired_files_are_not_counted_against_quota(tmp_path: Path) -> None:
    """过期阶段已删除的文件不得计入配额总量。

    单遍采集把整棵树读进一张表，过期删除与配额计账都用它。若配额阶段直接拿
    采集表计账，已被删掉的文件仍占着字节数，配额就会误判超限并继续删本该
    存活的新鲜文件——用户侧表现为刚发的图片描述缓存被连带清掉。
    """
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    root.mkdir()
    expired = root / "expired.png"
    expired.write_bytes(b"1234")
    os.utime(expired, (100.0, 100.0))
    fresh = root / "fresh.png"
    fresh.write_bytes(b"5678")
    os.utime(fresh, (4900.0, 4900.0))

    # 配额恰好等于 fresh 的大小：只有把 expired 从账上剔除才刚好不超限
    removed = image.ImageParser.cleanup_source_cache(
        root, max_age_sec=1000.0, max_total_bytes=4, now=5000.0
    )

    assert removed == 1
    assert not expired.exists()
    assert fresh.exists(), "已删除文件仍被计入配额，连带删掉了新鲜文件"


def test_cleanup_walks_tree_once(tmp_path: Path, monkeypatch) -> None:
    """整轮清理只遍历目录一次：三阶段共享同一时刻的目录视图。"""
    _, image, _ = _load_modules()
    root = tmp_path / "cache"
    (root / "aa").mkdir(parents=True)
    fresh = root / "aa" / "fresh.png"
    fresh.write_bytes(b"1234")
    os.utime(fresh, (4900.0, 4900.0))

    rglob_calls: list[str] = []
    original_rglob = Path.rglob

    def counting_rglob(self, pattern, *args, **kwargs):
        rglob_calls.append(pattern)
        return original_rglob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", counting_rglob)
    removed = image.ImageParser.cleanup_source_cache(
        root, max_age_sec=1000.0, max_total_bytes=1024, now=5000.0
    )

    assert removed == 0
    assert fresh.exists()
    assert len(rglob_calls) == 1, f"目录被遍历 {len(rglob_calls)} 遍，单遍契约退化"


# ============================================================================
# _resolve_image_url()：源解析分支
# ============================================================================


def test_resolve_uses_prepared_data_url(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = image.ImageParser(object())
    info = image.ImageInfo(url="https://x/y.png")
    info.prepared_source = PNG_DATA_URL
    assert asyncio.run(parser._resolve_image_url(info)) == PNG_DATA_URL


def test_resolve_uses_recorder_local_path(tmp_path: Path) -> None:
    # 生产装配等价（阶段 1.1 复审）：recorder 的媒体目录在
    # <data>/plugin_data/astrbot_plugin_message_recorder/ 下，故注入 data_root。
    # recorder 交回的路径同样要过 allowlist——它的入参 local_path 来自对端可控
    # 的消息组件，resolver 又是第三方插件函数，不能无条件当可信。
    _, image, _ = _load_modules()
    source = _png_file(tmp_path)

    class Recorder:
        async def get_local_image_path(self, _message_id, _image_url):
            return source

    parser = image.ImageParser(object(), recorder_bridge=Recorder(), data_root=tmp_path)
    info = image.ImageInfo(url="https://x/y.png", message_id="m1")
    assert asyncio.run(parser._resolve_image_url(info)) == PNG_DATA_URL


def test_resolve_rejects_recorder_path_outside_data_root(tmp_path: Path) -> None:
    """recorder 解析出的越界路径必须被拒（阶段 1.1 复审补漏）。

    攻击链：对端把 ``local_path`` 设成 ``../../../secrets/x.png``（相对路径，
    绕过 ``is_absolute`` 检查）→ 若第三方 recorder 的 resolver 是朴素
    ``root / value``，就会交回 <data> 之外的绝对路径。修复前该分支直接
    ``trusted=True`` 全量放行，与阶段 1.1 要关的攻击面同型。
    """
    _, image, _ = _load_modules()
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    outside = _png_file(tmp_path / "secrets", "private.png")

    class TraversalRecorder:
        async def get_local_image_path(self, _message_id, _image_url):
            return outside

        def resolve_relative_path(self, _value):
            return outside

    parser = image.ImageParser(object(), recorder_bridge=TraversalRecorder(), data_root=data_root)
    # 两个 recorder 入口都必须拒：按 message_id 查回的路径……
    by_id = image.ImageInfo(message_id="m1")
    assert asyncio.run(parser._resolve_image_url(by_id)) is None
    # ……以及相对路径解析升级来的路径
    by_relative = image.ImageInfo(file_path="../../../secrets/private.png")
    assert asyncio.run(parser._resolve_image_url(by_relative)) is None


def test_resolve_recorder_miss_falls_through(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()

    class Recorder:
        async def get_local_image_path(self, _message_id, _image_url):
            return None

    parser = image.ImageParser(object(), recorder_bridge=Recorder())

    async def fake_fetch(_url):
        return None

    monkeypatch.setattr(parser, "_fetch_image_data_url", fake_fetch)
    info = image.ImageInfo(url="https://x/y.png", message_id="m1")
    assert asyncio.run(parser._resolve_image_url(info)) is None


def test_resolve_http_file_path_fetches(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)

    async def fake_fetch(url):
        return PNG_DATA_URL if url == "https://x/y.png" else None

    monkeypatch.setattr(parser, "_fetch_image_data_url", fake_fetch)
    info = image.ImageInfo(file_path="https://x/y.png")
    assert asyncio.run(parser._resolve_image_url(info)) == PNG_DATA_URL


def test_resolve_http_file_path_fetch_failure_returns_none(tmp_path: Path, monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)

    async def fake_fetch(_url):
        return None

    monkeypatch.setattr(parser, "_fetch_image_data_url", fake_fetch)
    info = image.ImageInfo(file_path="https://x/y.png")
    assert asyncio.run(parser._resolve_image_url(info)) is None


def test_resolve_relative_path_via_recorder(tmp_path: Path) -> None:
    # 同上：合法的 recorder 媒体文件在 <data> 下，注入 data_root 后照常放行。
    _, image, _ = _load_modules()
    source = _png_file(tmp_path, "media.png")

    class Recorder:
        def resolve_relative_path(self, value):
            return source if value == "media/photo.png" else None

    parser = image.ImageParser(object(), recorder_bridge=Recorder(), data_root=tmp_path)
    info = image.ImageInfo(file_path="media/photo.png")
    assert asyncio.run(parser._resolve_image_url(info)) == PNG_DATA_URL


def test_recorder_resolved_path_outside_roots_is_rejected(tmp_path: Path) -> None:
    """录制桥交回的路径也必须过 allowlist（阶段 1.1 复审补漏）。

    这条曾被我判为"不由消息内容决定"而放行，是错的：``resolve_relative_path``
    的入参就是对端可控的 OneBot ``file`` / ``local_path``，而 resolver 是第三方
    插件函数（``recorder_bridge.py:86``）。若它做朴素的 ``root / value`` 拼接，
    ``../`` 就能逃出媒体目录并拿到无条件放行——与本轮要关的攻击面同型。
    """
    _, image, _ = _load_modules()
    data_root = tmp_path / "data"
    (data_root / "plugin_data").mkdir(parents=True, exist_ok=True)
    # <data> 之外的真实图片（魔数合法，只有 allowlist 能拦）
    outside = tmp_path / "elsewhere" / "leaked.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(PNG_BYTES)

    class NaiveRecorder:
        """模拟未做路径收敛的第三方 resolver（../ 逃出媒体根）。"""

        def resolve_relative_path(self, value):
            return outside

        async def get_local_image_path(self, _message_id, _image_url):
            return outside

    parser = image.ImageParser(object(), recorder_bridge=NaiveRecorder(), data_root=data_root)

    # 相对路径分支
    relative = image.ImageInfo(file_path="../../elsewhere/leaked.png")
    assert asyncio.run(parser._resolve_image_url(relative)) is None
    # message_id 查回分支
    by_id = image.ImageInfo(url="https://x/y.png", message_id="m1")
    parser2 = image.ImageParser(object(), recorder_bridge=NaiveRecorder(), data_root=data_root)

    async def no_fetch(_url):
        return None

    parser2._fetch_image_data_url = no_fetch  # 断掉远程回退，只看本地分支结论
    assert asyncio.run(parser2._resolve_image_url(by_id)) is None


# ============================================================================
# _materialize_data_url()：内容寻址写入分支
# ============================================================================


def test_materialize_requires_base64_data_url(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    assert parser._materialize_data_url("data:image/png;base64") is None


def test_materialize_rejects_unknown_mime(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    assert parser._materialize_data_url("data:image/svg+xml;base64," + encoded) is None


def test_materialize_rejects_bad_base64(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    assert parser._materialize_data_url("data:image/png;base64,!!!") is None


def test_materialize_rejects_mismatched_payload(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    encoded = base64.b64encode(b"not an image at all").decode("ascii")
    assert parser._materialize_data_url("data:image/png;base64," + encoded) is None


def test_materialize_requires_cache_root(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = image.ImageParser(object())
    assert parser._materialize_data_url(PNG_DATA_URL) is None


def test_materialize_rejects_existing_non_file_target(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    target = tmp_path / "image_cache" / PNG_DIGEST[:2] / f"{PNG_DIGEST}.png"
    target.mkdir(parents=True)
    assert parser._materialize_data_url(PNG_DATA_URL) is None


def test_materialize_write_failure_returns_none(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    occupied = tmp_path / "image_cache" / PNG_DIGEST[:2]
    occupied.write_bytes(b"occupied")
    assert parser._materialize_data_url(PNG_DATA_URL) is None


# ============================================================================
# _file_to_data_url() / _is_safe_url()：本地读取与 URL 守卫
# ============================================================================


def test_file_to_data_url_rejects_missing_path(tmp_path: Path) -> None:
    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    assert parser._file_to_data_url(tmp_path / "missing.png", trusted=True) is None


def test_is_safe_url_tolerates_urlparse_failure(monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser_mod = _parser_module()

    def boom(value):
        raise ValueError("malformed url")

    monkeypatch.setattr(parser_mod, "urlparse", boom)
    assert asyncio.run(image.ImageParser._is_safe_url("http://x")) is False


# ============================================================================
# _fetch_image_data_url()：远程下载全分支（不触真实网络）
# ============================================================================


def _make_fetch_env(monkeypatch, response):
    _, image, _ = _load_modules()
    parser_mod = _parser_module()
    monkeypatch.setattr(parser_mod, "_host_all_global", lambda host: True)

    class FakeAsyncHTTPTransport:
        def __init__(self, **_kwargs):
            pass

        async def handle_async_request(self, _request):
            raise AssertionError("fake client 不应走到真实传输")

        async def aclose(self):
            pass

    monkeypatch.setattr(
        parser_mod.httpx, "AsyncHTTPTransport", lambda **kw: FakeAsyncHTTPTransport()
    )

    class FakeStream:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *_exc):
            return False

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def stream(self, method, url):
            return FakeStream(response)

    monkeypatch.setattr(parser_mod.httpx, "AsyncClient", FakeClient)
    return image


def _fetch_result(image, url: str = "https://cdn.example/x.png"):
    parser = image.ImageParser(object())
    return asyncio.run(parser._fetch_image_data_url(url))


def test_fetch_http_error_status_returns_none(monkeypatch) -> None:
    image = _make_fetch_env(monkeypatch, _make_response(status_code=500))
    assert _fetch_result(image) is None


def test_fetch_content_length_too_big_returns_none(monkeypatch) -> None:
    image = _make_fetch_env(
        monkeypatch,
        _make_response(headers={"content-length": str(MAX_IMAGE_BYTES + 1)}, chunks=[PNG_BYTES]),
    )
    assert _fetch_result(image) is None


def test_fetch_bad_content_length_header_ignored(monkeypatch) -> None:
    image = _make_fetch_env(
        monkeypatch, _make_response(headers={"content-length": "abc"}, chunks=[PNG_BYTES])
    )
    result = _fetch_result(image)
    assert result == PNG_DATA_URL


def test_fetch_stream_exceeds_limit_returns_none(monkeypatch) -> None:
    image = _make_fetch_env(
        monkeypatch,
        _make_response(chunks=[b"x" * (MAX_IMAGE_BYTES + 1)]),
    )
    assert _fetch_result(image) is None


def test_fetch_empty_payload_returns_none(monkeypatch) -> None:
    image = _make_fetch_env(monkeypatch, _make_response(chunks=[]))
    assert _fetch_result(image) is None


def test_fetch_unrecognized_mime_returns_none(monkeypatch) -> None:
    image = _make_fetch_env(monkeypatch, _make_response(chunks=[b"hello world"]))
    assert _fetch_result(image) is None


def test_fetch_success_returns_data_url(monkeypatch) -> None:
    image = _make_fetch_env(monkeypatch, _make_response(chunks=[PNG_BYTES]))
    assert _fetch_result(image) == PNG_DATA_URL


def test_fetch_client_exception_returns_none(monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser_mod = _parser_module()
    monkeypatch.setattr(parser_mod, "_host_all_global", lambda host: True)

    class ExplodingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def stream(self, _method, _url):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(parser_mod.httpx, "AsyncClient", ExplodingClient)
    parser = image.ImageParser(object())
    assert asyncio.run(parser._fetch_image_data_url("https://cdn.example/x.png")) is None


def test_fetch_without_httpx_returns_none(monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser_mod = _parser_module()
    monkeypatch.setattr(parser_mod, "httpx", None)
    parser = image.ImageParser(object())
    assert asyncio.run(parser._fetch_image_data_url("https://cdn.example/x.png")) is None


def test_fetch_unsafe_url_returns_none(monkeypatch) -> None:
    _, image, _ = _load_modules()
    parser_mod = _parser_module()
    monkeypatch.setattr(parser_mod, "_host_all_global", lambda host: False)
    parser = image.ImageParser(object())
    assert asyncio.run(parser._fetch_image_data_url("https://cdn.example/x.png")) is None
