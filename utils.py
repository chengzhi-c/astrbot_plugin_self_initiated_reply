from __future__ import annotations

import inspect
import json
import re
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At

from .models import MessageRecord

# 预编译正则表达式以提升性能
_AT_MENTION_PATTERN = re.compile(r"^(?:\[[^\]]*[Aa][Tt][^\]]*\]\s*)+")
_CQ_AT_PATTERN = re.compile(r"^(?:\[CQ:at,[^\]]+\]\s*)+")
_TEXT_AT_PATTERN = re.compile(r"^(?:@\S+\s*)+")
_INLINE_AT_PATTERN = re.compile(r"\[CQ:at,[^\]]+\]")
_INLINE_MENTION_PATTERN = re.compile(r"\[At:[^\]]+\]")
_WHITESPACE_PATTERN = re.compile(r"\s+")

ALIAS_REPLY_REQUEST_PATTERN = re.compile(
    r"(?:"
    r"(?:回|回复|回应|理|搭理)(?:我|一下|下|句|句话|啊|嘛|呢)?|"
    r"(?:说|讲)(?:话|句|句话|一下|下|啊|嘛|呢)?|"
    r"(?:吱声|吱个声|冒泡|出来)(?:一下|下|啊|嘛|呢)?|"
    r"(?:出来)?(?:冒泡)(?:一下|下|啊|嘛|呢)?|"
    r"(?:在吗|还在吗|你在吗|听得到|看得到)|"
    r"(?:快点|赶紧|速速)(?:回|回复|说|讲|理|出来)(?:一下|下|句话|句|话|啊|嘛|呢)?|"
    r"(?:发|发个|发张|来|来个|来张|整|整个|丢|甩|给|找|搜|搜索)(?:个|张|一个|一张)?(?:表情包|表情|图|gif|动图)"
    r")"
)
GENERAL_REPLY_REQUEST_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:有人吗|在吗|还在吗|听得到吗?|看得到吗?)$",
        r"^(?:发|发个|发张|来|来个|来张|整|整个|丢|甩)(?:一个|一张|个|张)?(?:表情包|表情|图|gif|动图)$",
        r"^(?:表情包|表情|图|gif|动图)(?:来|发|整|给)(?:一个|一下|下)?$",
        r"^(?:找|搜|搜索)(?:个|张)?(?:表情包|表情|图|gif|动图)$",
    )
)


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


def session_group_id(umo: str) -> str:
    parts = str(umo or "").strip().split(":", 2)
    if len(parts) == 3 and "group" in parts[1].lower():
        return parts[2].strip()
    return ""


def session_whitelisted(umo: str, whitelist: set[str]) -> bool:
    normalized = str(umo or "").strip()
    if not normalized:
        return False
    if normalized in whitelist:
        return True
    group_id = session_group_id(normalized)
    return bool(group_id and group_id in whitelist)


def whitelist_storage_key(umo: str, whitelist: set[str]) -> str:
    normalized = str(umo or "").strip()
    group_id = session_group_id(normalized)
    if group_id and group_id in whitelist:
        return group_id
    return normalized


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
    raw = _AT_MENTION_PATTERN.sub("", raw).strip()
    raw = _CQ_AT_PATTERN.sub("", raw).strip()
    raw = _TEXT_AT_PATTERN.sub("", raw).strip()
    return raw


def clean_chat_text(text: str) -> str:
    raw = strip_leading_mentions(text)
    raw = _INLINE_AT_PATTERN.sub("", raw)
    raw = _INLINE_MENTION_PATTERN.sub("", raw)
    return _WHITESPACE_PATTERN.sub(" ", raw).strip()


def is_alias_call(text: str, aliases: list[str]) -> bool:
    normalized = strip_leading_mentions(text).strip()
    for alias in aliases:
        if alias and normalized == alias:
            return True
    return False


def _compact_reply_request_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").lower())


def _alias_request_tail(text: str, aliases: list[str]) -> str:
    normalized = _compact_reply_request_text(strip_leading_mentions(text))
    for alias in aliases:
        compact_alias = _compact_reply_request_text(alias)
        if not compact_alias:
            continue
        if normalized == compact_alias:
            return ""
        if normalized.startswith(compact_alias):
            return normalized[len(compact_alias) :].lstrip("，,。.!！?？:：-—")
    return ""


def looks_like_reply_request(text: str, aliases: list[str]) -> bool:
    normalized = _compact_reply_request_text(text)
    if not normalized:
        return False
    if is_alias_call(text, aliases):
        return True

    alias_tail = _alias_request_tail(text, aliases)
    if alias_tail and ALIAS_REPLY_REQUEST_PATTERN.fullmatch(alias_tail):
        return True

    return any(pattern.search(normalized) for pattern in GENERAL_REPLY_REQUEST_PATTERNS)


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
