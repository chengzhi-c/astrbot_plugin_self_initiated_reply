from __future__ import annotations

import asyncio
import importlib
import logging
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

