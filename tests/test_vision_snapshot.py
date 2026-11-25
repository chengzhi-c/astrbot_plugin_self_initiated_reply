"""补读历史期间的新图片不能改变本轮回复的图像上下文。"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

from ._support import PNG_BYTES
from .test_decision_maker import PACKAGE_NAME, _make_decision


async def test_decision_vision_keeps_target_when_history_read_receives_new_image(tmp_path):
    _, models, maker, *_ = _make_decision(
        tmp_path, {"vision_judge_enabled": True, "vision_max_images": 1}
    )
    coordinator_mod = importlib.import_module(f"{PACKAGE_NAME}.session_coordinator")
    vision_mod = importlib.import_module(f"{PACKAGE_NAME}.image.vision_runtime")
    support = importlib.import_module(f"{PACKAGE_NAME}.image._support")
    umo = "qq:GroupMessage:1"
    target_at = models.now_ts() - 2
    state = models.SessionState()
    state.last_active_at = target_at
    state.recent.append(models.MessageRecord("user", "Alice", "看看我的猫", target_at))
    source_a = support.to_data_url("image/png", PNG_BYTES + b"A")
    source_b = support.to_data_url("image/png", PNG_BYTES + b"B")
    inspected_sources = []
    gate = SimpleNamespace(is_current=lambda *_: True)

    class VisionBridge:
        async def resolve_provider_id(self, *_args):
            return "vision-test"

        async def llm_generate_direct(self, *, image_urls, **_kwargs):
            inspected_sources.extend(image_urls)
            description = "Alice 的猫" if image_urls == [source_a] else "Bob 的狗"
            return SimpleNamespace(completion_text=description)

    coordinator = coordinator_mod.SessionCoordinator(
        last_events={},
        last_event_at={},
        recent_image_events={},
        gate=gate,
        cancel_delay=lambda *_: None,
        notify_silence=lambda *_: None,
    )
    service = vision_mod.VisionService(
        settings=maker.settings,
        bridge=VisionBridge(),
        context=object(),
        source_cache_dir=tmp_path / "images",
        data_root=tmp_path,
        coordinator=coordinator,
        gate=gate,
        is_stopping=lambda: False,
        track_background_task=asyncio.create_task,
    )
    coordinator.capture_images(
        umo, target_at, [support.ImageInfo(url=source_a, prepared_source=source_a)]
    )

    async def read_history(_umo, *, limit):
        state.last_active_at = target_at + 1
        state.recent.append(models.MessageRecord("user", "Bob", "我的狗呢", target_at + 1))
        coordinator.capture_images(
            umo, target_at + 1, [support.ImageInfo(url=source_b, prepared_source=source_b)]
        )
        await asyncio.sleep(0)
        return []

    maker._read_history = read_history
    maker._build_image_context = service.build_context
    context = await maker.build_decision_prompt(umo, state, trigger="message_delay")

    assert "Alice 的猫" in context
    assert "Bob 的狗" not in context
    assert inspected_sources == [source_a]
    assert "Bob 的狗" in await service.build_context(umo, enabled=True)
