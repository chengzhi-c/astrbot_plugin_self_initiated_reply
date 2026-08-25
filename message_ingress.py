"""消息入口：指令分流 → 白名单 → 记上下文 → 延迟检查。

顺序是安全边界，不可重排。从 ``main.on_message`` 抽出以缩短入口文件。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .commands import parse_command_text
from .image import ImageExtractor
from .models import COMMAND_HANDLED_KEY, PLUGIN_ID, CheckTrigger, now_ts
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
    session_is_private,
    session_whitelisted,
    should_ignore_event,
    whitelist_storage_key,
)

if TYPE_CHECKING:
    from .main import SelfInitiatedReplyPlugin


def _eligible_session(
    plugin: SelfInitiatedReplyPlugin, event: AstrMessageEvent
) -> tuple[str, str] | None:
    if plugin._stopping or not plugin.runtime_enabled or event.is_stopped():
        return None
    umo = event_umo(event)
    if not session_whitelisted(umo, plugin.settings.whitelist):
        return None
    if session_is_private(umo) and not plugin.settings.enabled_private_sessions:
        return None
    state_key = whitelist_storage_key(umo)
    plugin._whitelist_runtime_umos.setdefault(state_key, set()).add(umo)
    group_id = session_group_id(umo)
    if group_id:
        plugin._whitelist_runtime_umos.setdefault(group_id, set()).add(umo)
    return umo, state_key


def _accepted_content(
    plugin: SelfInitiatedReplyPlugin,
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
    if should_ignore_event(
        event,
        text,
        vision_has_images=has_images,
        ignored_sender_ids=plugin.settings.ignored_sender_ids,
    ):
        if plugin.settings.abandon_stale_on_new_message:
            plugin._coordinator.invalidate(umo)
        if not is_self_message(event) and is_explicit_direct_call(event, text):
            state = plugin._state_for(state_key)
            state.last_active_at = now_ts()
            state.last_active_sender_id = event_sender_id(event)
        return None
    if not clean_text and not has_images:
        if plugin.settings.abandon_stale_on_new_message:
            plugin._coordinator.invalidate(umo)
        return None
    return clean_text or "[图片]", has_images


def _record_message(
    plugin: SelfInitiatedReplyPlugin,
    event: AstrMessageEvent,
    *,
    umo: str,
    state_key: str,
    clean_text: str,
) -> tuple[int, float]:
    if plugin.settings.abandon_stale_on_new_message or not plugin._gate.current(umo):
        generation = plugin._gate.advance(umo)
    else:
        generation = plugin._gate.current(umo)
    active_at = append_recent_user_message(
        plugin,
        event,
        state_key=state_key,
        clean_text=clean_text,
    )
    plugin._coordinator.record_event(umo, event, active_at)
    return generation, active_at


async def _capture_images(
    plugin: SelfInitiatedReplyPlugin,
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
    await plugin._vision.capture(
        umo,
        generation=generation,
        active_at=active_at,
        images=images,
    )


def _schedule_message_check(
    plugin: SelfInitiatedReplyPlugin, umo: str, clean_text: str, generation: int
) -> None:
    if not plugin.settings.enabled_message_trigger:
        return
    trigger = (
        CheckTrigger.REPLY_REQUEST
        if looks_like_reply_request(clean_text, plugin.settings.bot_aliases)
        else CheckTrigger.MESSAGE_DELAY
    )
    plugin._scheduler.schedule_delayed_check(
        umo,
        delay_sec=plugin._scheduler.message_trigger_delay(trigger),
        trigger=trigger,
        force=False,
        generation=generation,
    )


async def handle_incoming_message(
    plugin: SelfInitiatedReplyPlugin, event: AstrMessageEvent
) -> None:
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
