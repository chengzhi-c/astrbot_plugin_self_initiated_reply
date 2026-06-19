from __future__ import annotations

import inspect
import json
import re
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At

from .models import MessageRecord


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def parse_json(text: str) -> Any:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item.get("text")))
                elif item.get("text"):
                    parts.append(str(item.get("text")))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(part.strip() for part in parts if part.strip())
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "").strip()
    text = getattr(content, "text", None)
    return str(text or "").strip()


def event_text(event: AstrMessageEvent) -> str:
    value = getattr(event, "message_str", None)
    if value:
        return str(value)
    try:
        return str(event.get_message_str() or "")
    except Exception:
        return ""


def event_components(event: AstrMessageEvent) -> list[Any]:
    try:
        return list(event.get_messages() or [])
    except Exception:
        return list(getattr(getattr(event, "message_obj", None), "message", []) or [])


def raw_umo(event: AstrMessageEvent) -> str:
    value = getattr(event, "unified_msg_origin", "")
    if callable(value):
        try:
            value = value()
        except Exception:
            value = ""
    return str(value or "").strip()


def event_group_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_group_id() or "").strip()
    except Exception:
        return str(getattr(getattr(event, "message_obj", None), "group_id", "") or "").strip()


def event_umo(event: AstrMessageEvent) -> str:
    raw = raw_umo(event)
    if not raw:
        return ""
    parts = raw.split(":", 2)
    if len(parts) < 3:
        return raw
    platform, msg_type, session_id = parts
    group_id = event_group_id(event)
    if group_id and "group" in msg_type.lower():
        return f"{platform}:{msg_type}:{group_id}"
    return f"{platform}:{msg_type}:{session_id.strip()}"


def event_sender_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_id() or "").strip()
    except Exception:
        return ""


def event_self_id(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_self_id() or "").strip()
    except Exception:
        return ""


def event_sender_name(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_name() or event.get_sender_id() or "用户")
    except Exception:
        return "用户"


def session_label(event: AstrMessageEvent) -> str:
    group_id = event_group_id(event)
    if group_id:
        return f"群聊 {group_id}"
    return f"私聊 {event_sender_name(event)}"


def is_self_message(event: AstrMessageEvent) -> bool:
    sender = event_sender_id(event)
    self_id = event_self_id(event)
    return bool(sender and self_id and sender == self_id)


def is_admin_event(event: AstrMessageEvent, admin_ids: set[str]) -> bool:
    try:
        if event.is_admin():
            return True
    except Exception:
        pass
    role = str(getattr(event, "role", "") or getattr(event, "role_type", "")).lower()
    if role in {"admin", "owner", "superuser"}:
        return True
    sender_id = event_sender_id(event)
    return bool(sender_id and sender_id in admin_ids)


def is_at_or_wake_command_event(event: AstrMessageEvent) -> bool:
    value = getattr(event, "is_at_or_wake_command", False)
    if callable(value):
        try:
            value = value()
        except Exception:
            return False
    return bool(value)


def is_explicit_direct_call(event: AstrMessageEvent, text: str) -> bool:
    if is_at_or_wake_command_event(event):
        return True
    self_id = event_self_id(event)
    if self_id:
        if re.search(rf"\[At:{re.escape(self_id)}\]", text, re.IGNORECASE):
            return True
        if re.search(rf"\[CQ:at,[^\]]*(?:qq=)?{re.escape(self_id)}(?:\D|$)", text, re.IGNORECASE):
            return True
        for comp in event_components(event):
            if isinstance(comp, At) and str(getattr(comp, "qq", "")).strip() == self_id:
                return True
    return False


def strip_leading_mentions(text: str) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"^(?:\[[^\]]*[Aa][Tt][^\]]*\]\s*)+", "", raw).strip()
    raw = re.sub(r"^(?:\[CQ:at,[^\]]+\]\s*)+", "", raw).strip()
    raw = re.sub(r"^(?:@\S+\s*)+", "", raw).strip()
    return raw


def clean_chat_text(text: str) -> str:
    raw = strip_leading_mentions(text)
    raw = re.sub(r"\[CQ:at,[^\]]+\]", "", raw)
    raw = re.sub(r"\[At:[^\]]+\]", "", raw)
    return re.sub(r"\s+", " ", raw).strip()


def is_alias_call(text: str, aliases: list[str]) -> bool:
    normalized = strip_leading_mentions(text).strip()
    for alias in aliases:
        if alias and (normalized == alias or normalized.startswith(alias)):
            return True
    return False


def looks_like_reply_request(text: str, aliases: list[str]) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    if not normalized:
        return False
    if is_alias_call(text, aliases):
        return True
    patterns = (
        r"(回|回复|回应|理|搭理).{0,8}(我|一下|下|句|句话|啊|嘛)?$",
        r"(说|讲|吱声|吱个声|冒泡|出来).{0,8}(话|句|一下|下|啊|嘛)?$",
        r"(在吗|还在吗|你在吗|有人吗|听得到|看得到)",
        r"(快点|赶紧|速速).{0,8}(回|说|理|出来)",
        r"(发|发表|发个|发张|来|来个|来张|整|整个|丢|甩|给).{0,12}(表情包|表情|图|gif|动图)",
        r"(表情包|表情|图|gif|动图).{0,12}(来|发|整|给|一个|一下|下)",
        r"(找|搜|搜索).{0,12}(表情包|表情|图|gif|动图)",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def dedupe_message_records(records: list[MessageRecord]) -> list[MessageRecord]:
    deduped: list[MessageRecord] = []
    index: dict[tuple[str, str], int] = {}
    for item in records:
        text = re.sub(r"\s+", " ", str(item.text or "")).strip()
        if not text:
            continue
        key = (str(item.role or ""), text)
        if key in index:
            deduped[index[key]] = item
            continue
        index[key] = len(deduped)
        deduped.append(item)
    return deduped


def format_message_records(records: list[MessageRecord], *, limit: int) -> str:
    rows = records[-limit:]
    if not rows:
        return "(无)"
    lines = []
    for item in rows:
        name = "Bot" if item.role == "assistant" else (item.name or "用户")
        lines.append(f"{name}: {item.text}")
    return "\n".join(lines)


def count_text_records(records: list[MessageRecord]) -> int:
    return sum(1 for item in records if str(item.text or "").strip())


def latest_user_text(records: list[MessageRecord]) -> str:
    for item in reversed(records):
        if item.role == "user" and item.text.strip():
            return item.text.strip()
    return ""


def clean_reply(text: str, *, allow_multiline: bool, max_chars: int) -> str:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(?:回复|答复)\s*[:：]\s*", "", text).strip()
    if not allow_multiline:
        text = re.sub(r"\s+", " ", text)
    if max_chars and len(text) > max_chars:
        clipped = text[:max_chars].rstrip()
        match = re.search(r"^([\s\S]*[。！？.!?])[^。！？.!?]*$", clipped)
        text = (match.group(1) if match else clipped).strip()
    return text
