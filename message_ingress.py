"""消息入口：指令分流、白名单过滤、上下文记录与延迟检查调度。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .commands import parse_command_text
from .image import ImageExtractor
from .models import COMMAND_HANDLED_KEY, PLUGIN_ID, CheckTrigger, now_ts
from .plugin_state import append_recent_user_message
from .utils import (
    collapse_whitespace,
    event_extra,
    event_text,
    event_umo,
    is_explicit_direct_call,
    is_self_message,
    redact_exc_text,
    session_group_id,
    session_is_private,
    session_whitelisted,
    should_ignore_event,
    strip_leading_mentions,
)

if TYPE_CHECKING:
    from .main import SelfInitiatedReplyPlugin


def _eligible_session(plugin: SelfInitiatedReplyPlugin, event: AstrMessageEvent) -> str | None:
    if plugin._stopping or not plugin.runtime_enabled or event.is_stopped():
        return None
    umo = event_umo(event)
    if not session_whitelisted(umo, plugin.settings.whitelist):
        return None
    if session_is_private(umo) and not plugin.settings.enabled_private_sessions:
        return None
    # 索引键取白名单项的写法，不是状态键：scheduler 与 whitelist.replace
    # 都按白名单项查这张表。``event_umo`` 的输出恒为已 strip 的规范写法。
    plugin._whitelist_runtime_umos.setdefault(umo, set()).add(umo)
    group_id = session_group_id(umo)
    if group_id:
        plugin._whitelist_runtime_umos.setdefault(group_id, set()).add(umo)
    return umo


def _accepted_content(
    plugin: SelfInitiatedReplyPlugin,
    event: AstrMessageEvent,
    text: str,
    umo: str,
) -> tuple[str, bool] | None:
    """Return normalized content, invalidating ignored or empty events."""
    clean_text = collapse_whitespace(text)
    has_images = plugin.settings.vision_enabled and ImageExtractor.has_images(
        event,
        skip_stickers=plugin.settings.vision_skip_stickers,
    )
    ignored = should_ignore_event(
        event,
        text,
        vision_has_images=has_images,
        ignored_sender_ids=plugin.settings.ignored_sender_ids,
    )
    empty = not strip_leading_mentions(clean_text) and not has_images
    if ignored or empty:
        # 只作废旧任务，保留最近事件和图片供后续上下文使用。
        if plugin.settings.abandon_stale_on_new_message:
            plugin._gate.advance(umo)
        if not is_self_message(event):
            state = plugin._state_for(umo)
            active_at = now_ts()
            # 无正文的活动也刷新静默，但不重新打开已回复过的观察窗口。
            skip_batch = (
                plugin.settings.skip_after_direct_call
                if is_explicit_direct_call(event, text)
                else state.last_proactive_observed_at >= state.last_active_at
            )
            if skip_batch:
                state.last_proactive_observed_at = active_at
            state.last_active_at = active_at
            plugin._scheduler.notify_activity(umo)
        return None
    return clean_text or "[图片]", has_images


def _record_message(
    plugin: SelfInitiatedReplyPlugin,
    event: AstrMessageEvent,
    *,
    umo: str,
    clean_text: str,
) -> tuple[int, float]:
    if plugin.settings.abandon_stale_on_new_message or not plugin._gate.current(umo):
        generation = plugin._gate.advance(umo)
    else:
        generation = plugin._gate.current(umo)
    active_at = append_recent_user_message(
        plugin._state_for(umo),
        event,
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


def _schedule_message_check(plugin: SelfInitiatedReplyPlugin, umo: str, generation: int) -> None:
    if not plugin.settings.enabled_message_trigger:
        return
    trigger = CheckTrigger.MESSAGE_DELAY
    plugin._scheduler.schedule_delayed_check(
        umo,
        delay_sec=plugin._scheduler.message_trigger_delay(),
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

    umo = _eligible_session(plugin, event)
    if umo is None:
        return
    content = _accepted_content(plugin, event, text, umo)
    if content is None:
        return
    clean_text, has_images = content
    generation, active_at = _record_message(
        plugin,
        event,
        umo=umo,
        clean_text=clean_text,
    )
    if has_images:
        try:
            await _capture_images(
                plugin,
                event,
                umo=umo,
                generation=generation,
                active_at=active_at,
            )
        except Exception as exc:
            # 脱敏后进日志：recorder 桥与 provider SDK 的异常文本常带请求 URL
            # （含签名 token），而日志是应当留在服务端的那一面。
            logger.warning(
                "[%s] image capture failed session=%s error=%s",
                PLUGIN_ID,
                umo,
                redact_exc_text(exc),
            )
    plugin._scheduler.cleanup_events_if_needed()
    _schedule_message_check(plugin, umo, generation)
