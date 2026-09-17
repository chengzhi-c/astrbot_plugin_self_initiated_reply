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
    clean_chat_text,
    event_extra,
    event_text,
    event_umo,
    is_explicit_direct_call,
    is_self_message,
    session_group_id,
    session_is_private,
    session_whitelisted,
    should_ignore_event,
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
    # 索引键取白名单项的写法（完整 UMO 一条、有群号再补一条），不是状态键：
    # scheduler 与 whitelist.replace 都按白名单项查这张表。``event_umo`` 的输出
    # 恒为已 strip 的规范写法（raw_umo 先 strip，重建时首尾字符非空白），所以
    # 此处直接用 umo，无需再套一次状态键派生。
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
    clean_text = clean_chat_text(text)
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
    empty = not clean_text and not has_images
    if ignored or empty:
        # 语义单点：任何被入口接住的消息（含被忽略与空内容）都推进代次，作废
        # 未发出的旧回复（契约 §6.3）。
        if plugin.settings.abandon_stale_on_new_message:
            plugin._coordinator.invalidate(umo)
    if ignored:
        if not is_self_message(event) and is_explicit_direct_call(event, text):
            state = plugin._state_for(umo)
            state.last_active_at = now_ts()
            if plugin.settings.skip_after_direct_call:
                # 这条 @Bot/唤醒消息由 AstrBot 正常回复（不经过本插件），因此把它
                # 记为「这条消息之后 Bot 已经回应过」：同一批消息不再触发主动回复，
                # 避免"刚被点名答过、静默时间一到又主动接一句"。下一条新消息到达时
                # last_active_at 前进，观察窗口自然重新打开。
                state.last_proactive_observed_at = state.last_active_at
        return None
    if empty:
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
        plugin,
        event,
        umo=umo,
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


def _schedule_message_check(
    plugin: SelfInitiatedReplyPlugin, umo: str, clean_text: str, generation: int
) -> None:
    if not plugin.settings.enabled_message_trigger:
        return
    trigger = CheckTrigger.MESSAGE_DELAY
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
            logger.warning("[%s] image capture failed session=%s: %s", PLUGIN_ID, umo, exc)
    plugin._scheduler.cleanup_events_if_needed()
    _schedule_message_check(plugin, umo, clean_text, generation)
