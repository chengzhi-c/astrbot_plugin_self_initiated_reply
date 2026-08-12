"""消息入口：指令分流 → 白名单 → 记上下文 → 延迟检查。

顺序是安全边界，不可重排。从 ``main.on_message`` 抽出以缩短入口文件。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .commands import parse_command_text
from .image import ImageExtractor
from .models import COMMAND_HANDLED_KEY, PLUGIN_ID, MessageRecord, now_ts
from .utils import (
    clean_chat_text,
    event_extra,
    event_sender_id,
    event_sender_name,
    event_text,
    event_umo,
    is_explicit_direct_call,
    is_self_message,
    looks_like_reply_request,
    session_group_id,
    session_whitelisted,
    whitelist_storage_key,
)


async def handle_incoming_message(plugin: Any, event: AstrMessageEvent) -> None:
    text = event_text(event).strip()
    if event_extra(event, COMMAND_HANDLED_KEY, False):
        return
    parsed = parse_command_text(text)
    if parsed is not None and plugin._is_command_entry(event, text):
        await plugin._handle_inline_command(event, parsed)
        return

    if plugin._stopping or not plugin.runtime_enabled or event.is_stopped():
        return
    umo = event_umo(event)

    if not session_whitelisted(umo, plugin.settings.whitelist):
        return
    state_key = whitelist_storage_key(umo)
    plugin._whitelist_runtime_umos.setdefault(state_key, set()).add(umo)
    group_id = session_group_id(umo)
    if group_id:
        plugin._whitelist_runtime_umos.setdefault(group_id, set()).add(umo)

    clean_text = clean_chat_text(text)
    has_images = plugin.settings.vision_enabled and ImageExtractor.has_images(
        event,
        skip_stickers=plugin.settings.vision_skip_stickers,
    )
    if plugin._should_ignore_event(event, text, vision_has_images=has_images):
        plugin._invalidate_session(umo)
        if not is_self_message(event) and is_explicit_direct_call(event, text):
            state = plugin._state_for(state_key)
            state.last_active_at = now_ts()
            state.last_active_sender_id = event_sender_id(event)
        return

    if not clean_text and not has_images:
        plugin._invalidate_session(umo)
        return
    if not clean_text:
        clean_text = "[图片]"

    generation = plugin._gate.advance(umo)
    active_at = now_ts()
    state = plugin._state_for(state_key)
    state.last_active_at = active_at
    state.last_active_sender_id = event_sender_id(event)
    state.recent.append(
        MessageRecord(
            role="user",
            name=event_sender_name(event),
            sender_id=state.last_active_sender_id,
            text=clean_text,
            at=active_at,
        )
    )

    plugin._coordinator.record_event(umo, event, active_at)
    if has_images:
        images = ImageExtractor.extract_images(
            event,
            sender_id=event_sender_id(event),
            timestamp=active_at,
            skip_stickers=plugin.settings.vision_skip_stickers,
        )[: max(1, int(plugin.settings.vision_max_images))]
        if images:
            parser = plugin._get_image_parser()
            if parser is not None:
                try:
                    await parser.snapshot_local_sources(images, max_concurrent=2)
                except Exception as exc:
                    logger.debug("[%s] local image snapshot stage failed: %s", PLUGIN_ID, exc)
            plugin._track_background_task(
                plugin._prepare_images_for_session(
                    umo,
                    generation=generation,
                    active_at=active_at,
                    images=images,
                )
            )
        else:
            logger.debug(
                "[%s] has_images=True but extract_images returned empty for umo=%s",
                PLUGIN_ID,
                umo,
            )
    plugin._cleanup_old_events_if_needed()

    if plugin.settings.enabled_message_trigger:
        trigger = (
            "reply_request"
            if looks_like_reply_request(clean_text, plugin.settings.bot_aliases)
            else "message_delay"
        )
        delay = plugin._scheduler.message_trigger_delay(trigger)
        plugin._schedule_delayed_check(
            umo,
            delay_sec=delay,
            trigger=trigger,
            force=False,
            generation=generation,
        )
