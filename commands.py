"""指令文本解析、回显拼装，以及写动作分派。

解析/帮助/状态/列表/调试为纯函数。``dispatch_command_action`` 承载 add/remove/
check/on/off 等有副作用分支，经 plugin 回调访问状态（测试可替换实例方法）。
"""

from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent

from .models import MessageRecord, SessionState, Settings, fmt_ts, now_ts
from .utils import (
    clean_chat_text,
    event_group_id,
    event_self_id,
    event_sender_id,
    event_sender_name,
    event_text,
    event_umo,
    is_at_or_wake_command_event,
    is_explicit_direct_call,
    looks_like_reply_request,
    raw_umo,
    session_whitelisted,
    strip_leading_mentions,
    whitelist_storage_key,
)


def parse_command_text(text: str) -> tuple[str, str] | None:
    raw = strip_leading_mentions(str(text or "")).strip()
    if not raw:
        return None
    if raw.startswith("/"):
        raw = raw[1:].lstrip()
    lowered = raw.lower()
    if lowered == "selfreply":
        return "help", ""
    if not lowered.startswith("selfreply "):
        return None
    body = raw[len("selfreply") :].strip()
    parts = body.split(maxsplit=1)
    token = parts[0].strip().lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    aliases = {
        "help": {"help", "h"},
        "status": {"status", "stat"},
        "add": {"add"},
        "remove": {"remove", "rm", "del", "delete"},
        "list": {"list", "ls", "whitelist"},
        "check": {"check", "test"},
        "on": {"on", "enable", "start"},
        "off": {"off", "disable", "pause", "stop"},
        "debug": {"debug", "diag", "diagnose"},
    }
    for action, names in aliases.items():
        if token in names:
            return action, rest
    return None


def strip_command_prefix(text: str) -> str:
    parsed = parse_command_text(text)
    return parsed[1] if parsed else text


def help_text() -> str:
    return "\n".join(
        [
            "主动回复指令组：/selfreply",
            "/selfreply status: 查看运行状态、判断模型、白名单、冷却和今日次数（管理员）",
            "/selfreply add: 将当前会话加入主动回复白名单（管理员）",
            "/selfreply remove: 将当前会话移出主动回复白名单（管理员）",
            "/selfreply list: 查看当前主动回复白名单（管理员）",
            "/selfreply check [content]: 手动测试一次主动回复；可附带测试内容（管理员）",
            "/selfreply on: 启用主动回复，重启后保持（管理员）",
            "/selfreply off: 暂停主动回复，重启后保持（管理员）",
            "/selfreply debug: 查看当前会话、发送者、@/唤醒词和接话请求识别信息（管理员）",
            "可用英文别名：help/h、status/stat、list/ls/whitelist、check/test、remove/rm/del/delete、on/enable/start、off/disable/pause/stop、debug/diag/diagnose。",
            "中文命令入口已移除；也支持 @Bot selfreply add。",
        ]
    )


def list_text(settings: Settings) -> str:
    if not settings.whitelist:
        return "主动回复白名单为空。"
    return "主动回复白名单：\n" + "\n".join(f"- {item}" for item in sorted(settings.whitelist))


def status_text(
    settings: Settings, event: AstrMessageEvent, state: object, runtime_enabled: bool
) -> str:
    umo = event_umo(event)
    return "\n".join(
        [
            "主动回复状态",
            f"运行中: {runtime_enabled}",
            f"当前会话: {umo or '-'}",
            f"当前会话在白名单: {'是' if session_whitelisted(umo, settings.whitelist) else '否'}",
            f"白名单数量: {len(settings.whitelist)}",
            f"判断模型: {'启用' if settings.decision_model_enabled else '关闭'}，"
            f"Provider: {settings.judge_provider_id or '当前会话模型'}",
            f"判断提示词: {'自定义' if settings.decision_prompt_custom else '默认'}",
            f"最少上下文消息数: {settings.decision_history_min_messages} 条",
            f"消息后触发: {settings.enabled_message_trigger}，延迟 {settings.message_delay_sec}s，"
            f"最小静默 {settings.min_silence_sec}s",
            f"后台巡检: {settings.enabled_patrol_trigger}",
            "直接 @Bot: 始终交给 AstrBot 主回复链，主动回复不抢答",
            f"忽略发送者: {', '.join(sorted(settings.ignored_sender_ids)) or '-'}",
            f"冷却: {settings.cooldown_sec}s，今日上限: "
            f"{settings.max_daily_replies_per_session or '不限'}",
            f"今日已回复: {getattr(state, 'daily_count', 0)}",
            f"上次主动回复: {fmt_ts(getattr(state, 'last_proactive_at', 0.0))}",
            "回复生成: AstrBot 正常 LLM 管线模式",
            "表情包/LivingMemory: 由 AstrBot 主回复链中的插件自动处理",
        ]
    )


def debug_text(settings: Settings, event: AstrMessageEvent, ignored_sender: bool) -> str:
    text = event_text(event)
    return "\n".join(
        [
            "主动回复调试信息",
            f"原始 UMO: {raw_umo(event) or '-'}",
            f"归一化 UMO: {event_umo(event) or '-'}",
            f"group_id: {event_group_id(event) or '-'}",
            f"sender_id: {event_sender_id(event) or '-'}",
            f"self_id: {event_self_id(event) or '-'}",
            f"message_str: {text or '-'}",
            f"is_at_or_wake_command: {is_at_or_wake_command_event(event)}",
            f"ignored_sender: {ignored_sender}",
            f"explicit_direct_call: {is_explicit_direct_call(event, text)}",
            f"reply_request: "
            f"{looks_like_reply_request(clean_chat_text(text), settings.bot_aliases)}",
        ]
    )


async def dispatch_command_action(
    plugin: Any, event: AstrMessageEvent, action: str, arg: str = ""
) -> str:
    """指令动作 → 回显文本。check 在 finally 回收缓存；未知 action 回落 help。"""
    umo = event_umo(event)
    if action == "help":
        return help_text()
    if action == "status":
        state = plugin._state_for(whitelist_storage_key(umo)) if umo else SessionState()
        return status_text(plugin.settings, event, state, plugin.runtime_enabled)
    if action == "list":
        return list_text(plugin.settings)
    if not umo:
        return "无法识别当前会话。"
    if action == "add":
        added = await plugin._add_whitelist_session(umo)
        return (
            f"已将当前会话加入主动回复白名单：{umo}"
            if added
            else f"当前会话已在主动回复白名单中：{umo}"
        )
    if action == "remove":
        removed = await plugin._remove_whitelist_session(umo)
        return f"已移出主动回复白名单：{umo}" if removed else f"当前会话本不在主动回复白名单：{umo}"
    if action == "check":
        generation = plugin._invalidate_session(umo)
        plugin._coordinator.record_event(umo, event, now_ts())
        state = plugin._state_for(whitelist_storage_key(umo))
        text = clean_chat_text(arg or strip_command_prefix(event_text(event)))
        if text:
            state.last_active_at = now_ts()
            state.last_active_sender_id = event_sender_id(event)
            state.recent.append(
                MessageRecord(
                    role="user",
                    name=event_sender_name(event),
                    text=text,
                    at=state.last_active_at,
                )
            )
        try:
            result = await plugin._pipeline.check_session(
                umo,
                trigger="manual",
                force=True,
                expected_generation=generation,
            )
        finally:
            if plugin._last_events.get(umo) is event:
                plugin._coordinator.clear(umo)
            if not session_whitelisted(umo, plugin.settings.whitelist):
                plugin._prune_session(umo)
        return f"主动回复检查结果：{result}"
    if action == "on":
        async with plugin._config_lock:
            await plugin._persist_enabled(True)
            plugin._scheduler.ensure_patrol()
            plugin._scheduler.ensure_image_cleanup()
        return "主动回复插件已启用（重启后保持）。"
    if action == "off":
        async with plugin._config_lock:
            await plugin._persist_enabled(False)
            plugin._cancel_delay_tasks()
            await plugin._scheduler.stop_patrol()
        return "主动回复插件已暂停（重启后保持）。"
    if action == "debug":
        return debug_text(
            plugin.settings,
            event,
            ignored_sender=event_sender_id(event) in plugin.settings.ignored_sender_ids,
        )
    return help_text()
