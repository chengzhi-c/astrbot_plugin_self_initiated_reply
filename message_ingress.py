"""消息入口：指令分流 → 白名单 → 记上下文 → 延迟检查。

顺序是安全边界，不可重排。从 ``main.on_message`` 抽出以缩短入口文件。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .commands import parse_command_text
from .image import ImageExtractor
from .image.vision_runtime import get_image_parser, prepare_images_for_session
from .models import COMMAND_HANDLED_KEY, PLUGIN_ID, now_ts
from .plugin_state import append_recent_user_message
from .utils import (
    clean_chat_text,
    event_extra,
    event_sender_id,
    event_text,
    event_umo,
    is_explicit_direct_call,
    is_self_message,
    looks_like_reply_request,
    session_group_id,
    session_whitelisted,
    whitelist_storage_key,
)


def _eligible_session(plugin: Any, event: AstrMessageEvent) -> tuple[str, str] | None:
    if plugin._stopping or not plugin.runtime_enabled or event.is_stopped():
        return None
    umo = event_umo(event)
    if not session_whitelisted(umo, plugin.settings.whitelist):
        return None
    state_key = whitelist_storage_key(umo)
    plugin._whitelist_runtime_umos.setdefault(state_key, set()).add(umo)
    group_id = session_group_id(umo)
    if group_id:
        plugin._whitelist_runtime_umos.setdefault(group_id, set()).add(umo)
    return umo, state_key


def _accepted_content(
    plugin: Any,
    event: AstrMessageEvent,
    text: str,
    umo: str,
    state_key: str,
) -> tuple[str, bool] | None:
    """Return normalized content, invalidating ignored or empty events."""
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
        return None
    if not clean_text and not has_images:
        plugin._invalidate_session(umo)
        return None
    return clean_text or "[图片]", has_images


def _record_message(
    plugin: Any,
    event: AstrMessageEvent,
    *,
    umo: str,
    state_key: str,
    clean_text: str,
) -> tuple[int, float]:
    generation = plugin._gate.advance(umo)
    active_at = append_recent_user_message(
        plugin,
        event,
        state_key=state_key,
        clean_text=clean_text,
    )
    plugin._coordinator.record_event(umo, event, active_at)
    return generation, active_at


async def _capture_images(
    plugin: Any,
    event: AstrMessageEvent,
    *,
    umo: str,
    generation: int,
    active_at: float,
) -> None:
    images = ImageExtractor.extract_images(
        event,
        sender_id=event_sender_id(event),
        timestamp=active_at,
        skip_stickers=plugin.settings.vision_skip_stickers,
    )[: max(1, int(plugin.settings.vision_max_images))]
    if not images:
        logger.debug(
            "[%s] has_images=True but extract_images returned empty for umo=%s",
            PLUGIN_ID,
            umo,
        )
        return
    parser = get_image_parser(plugin)
    if parser is not None:
        try:
            await parser.snapshot_local_sources(images, max_concurrent=2)
        except Exception as exc:
            logger.debug("[%s] local image snapshot stage failed: %s", PLUGIN_ID, exc)
    plugin._track_background_task(
        prepare_images_for_session(
            plugin,
            umo,
            generation=generation,
            active_at=active_at,
            images=images,
        )
    )


def _schedule_message_check(plugin: Any, umo: str, clean_text: str, generation: int) -> None:
    if not plugin.settings.enabled_message_trigger:
        return
    trigger = (
        "reply_request"
        if looks_like_reply_request(clean_text, plugin.settings.bot_aliases)
        else "message_delay"
    )
    plugin._scheduler.schedule_delayed_check(
        umo,
        delay_sec=plugin._scheduler.message_trigger_delay(trigger),
        trigger=trigger,
        force=False,
        generation=generation,
    )


async def handle_incoming_message(plugin: Any, event: AstrMessageEvent) -> None:
    """Route one host event; invalid or ignored events stop before scheduling."""
    text = event_text(event).strip()
    if event_extra(event, COMMAND_HANDLED_KEY, False):
        return
    parsed = parse_command_text(text)
    if parsed is not None and plugin._is_command_entry(event, text):
        await plugin._handle_inline_command(event, parsed)
        return

    session = _eligible_session(plugin, event)
    if session is None:
        return
    umo, state_key = session
    content = _accepted_content(plugin, event, text, umo, state_key)
    if content is None:
        return
    clean_text, has_images = content
    generation, active_at = _record_message(
        plugin,
        event,
        umo=umo,
        state_key=state_key,
        clean_text=clean_text,
    )
    if has_images:
        await _capture_images(
            plugin,
            event,
            umo=umo,
            generation=generation,
            active_at=active_at,
        )
    plugin._scheduler.cleanup_events_if_needed()
    _schedule_message_check(plugin, umo, clean_text, generation)
