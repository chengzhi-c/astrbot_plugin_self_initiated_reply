from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "selfreply_vision_test_package"


def _install_astrbot_stubs() -> None:
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    event = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    star = sys.modules.setdefault("astrbot.api.star", types.ModuleType("astrbot.api.star"))
    components = sys.modules.setdefault(
        "astrbot.api.message_components", types.ModuleType("astrbot.api.message_components")
    )

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class At:
        pass

    api.logger = logging.getLogger("selfreply-vision-test")
    event.AstrMessageEvent = AstrMessageEvent
    star.Context = Context
    components.At = At
    setattr(astrbot, "api", api)


def _load_modules():
    _install_astrbot_stubs()
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package
    adapters = importlib.import_module(f"{PACKAGE_NAME}.adapters")
    image = importlib.import_module(f"{PACKAGE_NAME}.image")
    models = importlib.import_module(f"{PACKAGE_NAME}.models")
    return adapters, image, models


def test_image_extractor_preserves_remote_url_and_local_path() -> None:
    _, image, _ = _load_modules()

    class Image:
        url = "https://cdn.example.test/cat.png"
        local_path = r"C:\media\cat.png"

    class Event:
        message_id = "message-42"

        @staticmethod
        def get_messages():
            return [Image()]

    extracted = image.ImageExtractor.extract_images(Event(), sender_id="u1", timestamp=123.0)

    assert len(extracted) == 1
    assert extracted[0].url == "https://cdn.example.test/cat.png"
    assert extracted[0].file_path == r"C:\media\cat.png"
    assert extracted[0].message_id == "message-42"
    assert extracted[0].sender_id == "u1"


def test_sticker_images_can_be_skipped_by_platform_metadata() -> None:
    _, image, _ = _load_modules()

    class NormalImage:
        type = "image"
        subType = 0
        url = "https://cdn.example.test/photo.png"

    class StickerImage:
        type = "image"
        subType = 1
        url = "https://cdn.example.test/sticker.gif"

    class RawSticker:
        type = "image"
        data = {
            "subType": 1,
            "url": "https://cdn.example.test/raw-sticker.gif",
        }

    raw_sticker = {
        "type": "image",
        "data": {
            "subType": 1,
            "url": "https://cdn.example.test/raw-dict-sticker.gif",
        },
    }

    class Event:
        message_id = "message-43"

        @staticmethod
        def get_messages():
            return [NormalImage(), StickerImage(), RawSticker(), raw_sticker]

    event = Event()
    assert image.ImageExtractor.has_images(event)
    assert image.ImageExtractor.has_images(event, skip_stickers=True)
    assert image.ImageExtractor.is_sticker(StickerImage())
    assert image.ImageExtractor.is_sticker(RawSticker())
    assert image.ImageExtractor.is_sticker(raw_sticker)

    extracted = image.ImageExtractor.extract_images(event, skip_stickers=True)
    assert [item.url for item in extracted] == ["https://cdn.example.test/photo.png"]
    assert extracted[0].is_sticker is False


def test_onebot_sticker_only_message_is_excluded_before_vision_cache() -> None:
    """OneBot subType=1 must be filtered before the Vision event cache entry."""
    _, image, _ = _load_modules()

    class Event:
        message_id = "onebot-sticker-only"

        @staticmethod
        def get_messages():
            # 与 aiocqhttp OneBot 原始消息段形状一致。
            return [
                {
                    "type": "image",
                    "data": {
                        "file": "EF99ED5B76CBE7B88D3C0439B616A28C.jpg",
                        "subType": 1,
                        "url": "https://multimedia.nt.qq.com.cn/download?fileid=sticker",
                        "file_size": "30241",
                    },
                }
            ]

    event = Event()
    assert image.ImageExtractor.has_images(event) is True
    assert image.ImageExtractor.has_images(event, skip_stickers=True) is False
    assert image.ImageExtractor.extract_images(event, skip_stickers=True) == []


def test_onebot_mixed_message_keeps_normal_image_and_skips_sticker() -> None:
    """A normal image in the same chain must remain eligible for Vision."""
    _, image, _ = _load_modules()

    class Event:
        @staticmethod
        def get_messages():
            return [
                {
                    "type": "image",
                    "data": {
                        "file": "normal.jpg",
                        "subType": 0,
                        "url": "https://multimedia.nt.qq.com.cn/download?fileid=normal",
                    },
                },
                {
                    "type": "image",
                    "data": {
                        "file": "sticker.jpg",
                        "subType": 1,
                        "url": "https://multimedia.nt.qq.com.cn/download?fileid=sticker",
                    },
                },
            ]

    extracted = image.ImageExtractor.extract_images(Event(), skip_stickers=True)
    assert len(extracted) == 1
    assert extracted[0].url.endswith("fileid=normal")
    assert extracted[0].is_sticker is False


def test_normalized_image_falls_back_to_raw_onebot_subtype() -> None:
    """AstrBot Image may drop subType; raw_message must remain authoritative."""
    _, image, _ = _load_modules()

    class NormalizedImage:
        type = "image"
        file = "sticker.jpg"
        url = "https://multimedia.nt.qq.com.cn/download?fileid=sticker"
        # AstrBot 4.26.8 Image has no OneBot subType field, while raw_message
        # keeps the authoritative subType=1 marker.

    class Event:
        message_obj = SimpleNamespace(
            raw_message={
                "message": [
                    {
                        "type": "image",
                        "data": {
                            "file": "sticker.jpg",
                            "subType": 1,
                            "url": "https://multimedia.nt.qq.com.cn/download?fileid=sticker",
                        },
                    }
                ]
            }
        )

        @staticmethod
        def get_messages():
            return [NormalizedImage()]

    event = Event()
    assert image.ImageExtractor.has_images(event) is True
    assert image.ImageExtractor.has_images(event, skip_stickers=True) is False
    assert image.ImageExtractor.extract_images(event, skip_stickers=True) == []


def test_on_message_keeps_image_eligibility_out_of_generic_ignore_gate() -> None:
    """The generic event gate must not duplicate Vision image parsing."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    start = source.index("    async def on_message(")
    end = source.index("\n    def _is_command_entry(", start)
    handler = source[start:end]
    ignore_start = source.index("    def _should_ignore_event(")
    ignore_end = source.index("\n    def _advance_session_generation(", ignore_start)
    ignore_method = source[ignore_start:ignore_end]

    marker = "skip_stickers=self.settings.vision_skip_stickers"
    assert handler.count(marker) >= 2, (
        "on_message 必须在 Vision 资格判断和实际提取处使用同一表情包过滤设置"
    )
    assert "ImageExtractor.has_images" in handler
    assert "ImageExtractor.has_images" not in ignore_method, (
        "通用忽略门不应再次解析图片；图片资格应由 on_message 统一计算"
    )


def test_image_parser_uses_direct_vision_call_and_caches_caption() -> None:
    _, image, _ = _load_modules()

    class Bridge:
        def __init__(self):
            self.calls = []

        async def resolve_provider_id(self, umo, preferred):
            assert umo == "qq:GroupMessage:1"
            assert preferred == "vision-provider"
            return "vision-provider"

        async def llm_generate_direct(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(completion_text="一只橘猫坐在窗边")

    bridge = Bridge()
    parser = image.ImageParser(bridge, provider_id="vision-provider")

    async def fake_resolve(_image_info):
        return "data:image/png;base64,AA=="

    parser._resolve_image_url = fake_resolve
    image_info = image.ImageInfo(url="https://cdn.example.test/cat.png")

    first = asyncio.run(parser.parse(image_info, umo="qq:GroupMessage:1"))
    second = asyncio.run(parser.parse(image_info, umo="qq:GroupMessage:1"))

    assert first == "一只橘猫坐在窗边"
    assert second == first
    assert len(bridge.calls) == 1
    assert bridge.calls[0]["provider_id"] == "vision-provider"
    assert bridge.calls[0]["image_urls"] == ["data:image/png;base64,AA=="]


def test_image_parser_freezes_remote_image_before_delayed_parse(tmp_path: Path) -> None:
    _, image, _ = _load_modules()

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            return SimpleNamespace(completion_text="一张图片")

    parser = image.ImageParser(
        Bridge(), provider_id="vision", source_cache_dir=tmp_path / "image_cache"
    )
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    async def fake_fetch(_url):
        return "data:image/png;base64," + __import__("base64").b64encode(payload).decode()

    parser._fetch_image_data_url = fake_fetch
    info = image.ImageInfo(url="https://multimedia.nt.qq.com.cn/expired.png")

    assert asyncio.run(parser.prepare(info)) is True
    assert info.prepared_source
    assert Path(info.prepared_source).is_file()
    assert info.file_path == info.prepared_source
    assert asyncio.run(parser.parse(info, umo="qq:GroupMessage:1")) == "一张图片"


def test_image_parser_source_cache_cleanup_preserves_active_sources(tmp_path: Path) -> None:
    """Expired frozen files are removed while active session sources survive."""
    _, image, _ = _load_modules()

    root = tmp_path / "image_cache"
    old = root / "aa" / "old.png"
    protected = root / "bb" / "protected.png"
    fresh = root / "cc" / "fresh.png"
    for path in (old, protected, fresh):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached-image")

    os.utime(old, (100.0, 100.0))
    os.utime(protected, (100.0, 100.0))
    os.utime(fresh, (4900.0, 4900.0))

    removed = image.ImageParser.cleanup_source_cache(
        root,
        protected_sources={str(protected)},
        max_age_sec=1000.0,
        now=5000.0,
    )

    assert removed == 1
    assert not old.exists()
    assert protected.exists()
    assert fresh.exists()


def test_image_parser_does_not_fallback_to_expiring_raw_url() -> None:
    _, image, _ = _load_modules()

    class Bridge:
        async def resolve_provider_id(self, _umo, preferred):
            return preferred

        async def llm_generate_direct(self, **_kwargs):
            raise AssertionError("不可访问的图片不应继续传给 Provider")

    parser = image.ImageParser(Bridge(), provider_id="vision")

    async def failed_fetch(_url):
        return None

    parser._fetch_image_data_url = failed_fetch
    info = image.ImageInfo(url="https://multimedia.nt.qq.com.cn/expired.png")

    assert asyncio.run(parser.parse(info, umo="qq:GroupMessage:1")) is None


def test_image_parser_rejects_private_network_targets() -> None:
    _, image, _ = _load_modules()

    assert not asyncio.run(image.ImageParser._is_safe_url("http://127.0.0.1/private.png"))
    assert not asyncio.run(image.ImageParser._is_safe_url("http://[::1]/private.png"))
    assert not asyncio.run(image.ImageParser._is_safe_url("file:///etc/passwd"))


def test_bridge_direct_vision_call_forwards_images_without_context_hooks() -> None:
    adapters, _, _ = _load_modules()

    class Provider:
        def __init__(self):
            self.calls = []

        async def text_chat(self, *, prompt, contexts, system_prompt, image_urls):
            self.calls.append(
                {
                    "prompt": prompt,
                    "contexts": contexts,
                    "system_prompt": system_prompt,
                    "image_urls": image_urls,
                }
            )
            return SimpleNamespace(completion_text="图片说明")

    class ProviderManager:
        def __init__(self, provider):
            self.provider = provider

        async def get_provider_by_id(self, provider_id):
            assert provider_id == "vision"
            return self.provider

    class Context:
        def __init__(self, provider):
            self.provider_manager = ProviderManager(provider)

        async def llm_generate(self, **_kwargs):
            raise AssertionError("图片识别不应调用 context.llm_generate")

    provider = Provider()
    bridge = adapters.AstrBotBridge(Context(provider))
    result = asyncio.run(
        bridge.llm_generate_direct(
            provider_id="vision",
            prompt="描述图片",
            system_prompt="只描述图片",
            image_urls=["data:image/png;base64,AA=="],
        )
    )

    assert result.completion_text == "图片说明"
    assert provider.calls == [
        {
            "prompt": "描述图片",
            "contexts": [],
            "system_prompt": "只描述图片",
            "image_urls": ["data:image/png;base64,AA=="],
        }
    ]


def test_vision_settings_are_bounded_and_persisted() -> None:
    _, _, models = _load_modules()

    settings = models.Settings.from_config(
        {
            "vision_judge_enabled": True,
            "vision_main_enabled": True,
            "vision_provider_id": "vision",
            "vision_skip_stickers": True,
            "vision_max_images": 999,
            "vision_image_age_sec": 999999,
            "vision_timeout_sec": 0,
        }
    )

    assert settings.vision_enabled is True
    assert settings.vision_provider_id == "vision"
    assert settings.vision_max_images == models.MAX_VISION_IMAGES
    assert settings.vision_image_age_sec == models.MAX_VISION_IMAGE_AGE_SEC
    assert settings.vision_timeout_sec > 0
    persisted = settings.to_config_dict()
    assert persisted["vision_judge_enabled"] is True
    assert persisted["vision_main_enabled"] is True
    assert persisted["vision_provider_id"] == "vision"
    assert settings.vision_skip_stickers is True
    assert persisted["vision_skip_stickers"] is True
    assert "vision_enabled" not in persisted, (
        "聚合开关不得写回配置：它不在 _conf_schema.json 里，"
        "AstrBot 配置页会把它渲染成无效的裸文本框"
    )


def test_judge_and_main_vision_toggles_are_independent() -> None:
    """两个识图开关必须能单独开关。"""
    _, _, models = _load_modules()

    judge_only = models.Settings.from_config({"vision_judge_enabled": True})
    assert judge_only.vision_judge_enabled is True
    assert judge_only.vision_main_enabled is False
    assert judge_only.vision_enabled is True, "任一开启时聚合开关应为 True"

    main_only = models.Settings.from_config({"vision_main_enabled": True})
    assert main_only.vision_judge_enabled is False
    assert main_only.vision_main_enabled is True
    assert main_only.vision_enabled is True

    both_off = models.Settings.from_config({})
    assert both_off.vision_enabled is False, "两个都关时不应缓存含图事件"


def test_legacy_vision_enabled_migrates_to_both_toggles() -> None:
    """旧配置只有 vision_enabled，应迁移为两个开关同值。"""
    _, _, models = _load_modules()

    migrated = models.Settings.from_config({"vision_enabled": True})
    assert migrated.vision_judge_enabled is True
    assert migrated.vision_main_enabled is True

    off = models.Settings.from_config({"vision_enabled": False})
    assert off.vision_judge_enabled is False
    assert off.vision_main_enabled is False

    # 新开关显式给值时优先于旧聚合开关
    override = models.Settings.from_config(
        {"vision_enabled": True, "vision_judge_enabled": False}
    )
    assert override.vision_judge_enabled is False
    assert override.vision_main_enabled is True


def test_persisted_config_keys_all_exist_in_schema() -> None:
    """写回配置的每个键都必须在 schema 里有声明。

    否则 AstrBot 配置页会把未声明的键渲染成带原始键名的裸文本框。
    """
    import json

    _, _, models = _load_modules()

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    persisted = models.Settings.from_config({}).to_config_dict()

    undeclared = sorted(set(persisted) - set(schema))
    assert not undeclared, f"以下键被写回配置但不在 schema 里: {undeclared}"


def test_image_cache_is_lru_bounded() -> None:
    _, image, _ = _load_modules()
    cache = image.ImageCache(max_size=2)
    cache.put("a", "desc-a")
    cache.put("b", "desc-b")
    assert cache.get("a") == "desc-a"
    cache.put("c", "desc-c")
    assert cache.get("b") is None
    assert cache.get("a") == "desc-a"
    assert cache.get("c") == "desc-c"


def test_image_info_cache_key_prefers_url_then_file() -> None:
    _, image, _ = _load_modules()
    url_info = image.ImageInfo(url="https://x/y.png", file_path="/tmp/y.png")
    file_info = image.ImageInfo(file_path="/tmp/z.png")
    empty_info = image.ImageInfo(message_id="m1")
    assert url_info.cache_key() == "url:https://x/y.png"
    assert file_info.cache_key() == "file:/tmp/z.png"
    assert empty_info.cache_key() == "id:m1"
    assert not empty_info.has_any_source


def test_judge_vision_provider_falls_back_to_main_vision_provider() -> None:
    """判断阶段识图 Provider 留空时必须回落到主识图 Provider。

    这保证旧配置升级后行为不变：以前只有一个 vision_provider_id，
    两个阶段共用；新字段不填时应继续共用。
    """
    _, _, models = _load_modules()

    # 留空 → 回落主识图 Provider
    inherited = models.Settings.from_config({"vision_provider_id": "vision-main"})
    assert inherited.vision_judge_provider_id == ""
    assert inherited.vision_judge_provider_resolved == "vision-main"

    # 显式指定 → 不受主识图 Provider 影响
    overridden = models.Settings.from_config(
        {"vision_provider_id": "vision-main", "vision_judge_provider_id": "vision-cheap"}
    )
    assert overridden.vision_judge_provider_resolved == "vision-cheap"
    assert overridden.vision_provider_id == "vision-main"

    # 两者都留空 → 交由 adapter 回落到当前会话模型
    both_empty = models.Settings.from_config({})
    assert both_empty.vision_judge_provider_resolved == ""

    # 只填判断阶段，主阶段仍然走会话默认
    judge_only = models.Settings.from_config({"vision_judge_provider_id": "vision-cheap"})
    assert judge_only.vision_judge_provider_resolved == "vision-cheap"
    assert judge_only.vision_provider_id == ""


def test_judge_vision_provider_is_persisted_and_whitespace_stripped() -> None:
    _, _, models = _load_modules()

    settings = models.Settings.from_config({"vision_judge_provider_id": "  vision-cheap  "})
    assert settings.vision_judge_provider_id == "vision-cheap"
    assert settings.to_config_dict()["vision_judge_provider_id"] == "vision-cheap"


def test_parsers_with_different_providers_do_not_share_descriptions() -> None:
    """不同 Provider 的 parser 不得共用描述缓存。

    缓存键只根据图片计算（不含 provider），所以两个 Provider 必须各自
        持有缓存；否则判断阶段廉价模型的描述会被主阶段读到。
    """
    _, image, _ = _load_modules()

    def make_parser(provider_id: str, caption: str):
        class Bridge:
            def __init__(self):
                self.calls = 0

            async def resolve_provider_id(self, umo, preferred):
                return preferred

            async def llm_generate_direct(self, **kwargs):
                self.calls += 1
                return SimpleNamespace(completion_text=caption)

        bridge = Bridge()
        parser = image.ImageParser(bridge, provider_id=provider_id)

        async def fake_resolve(_image_info):
            return "data:image/png;base64,AA=="

        parser._resolve_image_url = fake_resolve
        return bridge, parser

    image_info = image.ImageInfo(url="https://cdn.example.test/same.png")

    cheap_bridge, cheap_parser = make_parser("vision-cheap", "一只猫")
    good_bridge, good_parser = make_parser("vision-good", "一只橘猫坐在窗边晓着眼睛")

    cheap = asyncio.run(cheap_parser.parse(image_info, umo="qq:GroupMessage:1"))
    good = asyncio.run(good_parser.parse(image_info, umo="qq:GroupMessage:1"))

    assert cheap == "一只猫"
    assert good == "一只橘猫坐在窗边晓着眼睛", "不得读到另一个 Provider 的缓存结果"
    assert cheap_bridge.calls == 1
    assert good_bridge.calls == 1

    # 同一个 parser 重复解析同一张图仍然命中缓存
    assert asyncio.run(cheap_parser.parse(image_info, umo="qq:GroupMessage:1")) == "一只猫"
    assert cheap_bridge.calls == 1


def test_skip_sticker_vision_setting_is_declared_in_schema() -> None:
    import json

    _, _, _ = _load_modules()
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    entry = schema.get("vision_skip_stickers")
    assert entry is not None
    assert entry["type"] == "bool"
    assert entry["default"] is False


def test_judge_vision_provider_is_declared_in_schema() -> None:
    """新增配置项必须在 schema 里声明，否则 AstrBot 配置页无法正常渲染。"""
    import json

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    entry = schema.get("vision_judge_provider_id")
    assert entry is not None, "vision_judge_provider_id 未在 _conf_schema.json 声明"
    assert entry["type"] == "string"
    assert entry["default"] == ""
    assert entry.get("_special") == "select_provider", (
        "应使用 select_provider 以便在配置页直接选择 Provider"
    )

