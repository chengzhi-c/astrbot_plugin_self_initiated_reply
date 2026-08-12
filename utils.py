"""宿主事件字段提取与消息文本纯函数库。

全部函数无状态（仅依赖入参），供 main/scheduler/decision 与测试共用。
职责三簇：
- 事件字段提取：event_text/event_umo/event_sender_* 等，统一宿主事件访问口径；
- 文本清洗与判定：clean_chat_text/is_at_*/looks_like_reply_request 等；
- 白名单与历史记录：session_whitelisted/whitelist_storage_key/build_history_text 等。
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At

from .models import PLUGIN_ID, MessageRecord, ReadHistoryCallback

# 预编译正则表达式以提升性能
_AT_MENTION_PATTERN = re.compile(r"^(?:\[[^\]]*[Aa][Tt][^\]]*\]\s*)+")
_CQ_AT_PATTERN = re.compile(r"^(?:\[CQ:at,[^\]]+\]\s*)+")
_TEXT_AT_PATTERN = re.compile(r"^(?:@\S+\s*)+")
_INLINE_AT_PATTERN = re.compile(r"\[CQ:at,[^\]]+\]")
_INLINE_MENTION_PATTERN = re.compile(r"\[At:[^\]]+\]")
_TOOL_CALL_LEAK_PATTERN = re.compile(r"^\s*\[(?:historical )?tool call\]", re.IGNORECASE)
# 工具标记及其同行残留（不跨行，避免吃掉后续正常内容）
_TOOL_CALL_INLINE_PATTERN = re.compile(r"\[(?:historical\s+)?tool\s+call\][^\n]*", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
# 行内空白：不含换行，用于保留多行结构时压缩空格
_INLINE_SPACE_PATTERN = re.compile(r"[^\S\n]+")

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
    # 用 inspect.isawaitable 而非 hasattr(value, "__await__")：后者会漏
    # CO_ITERABLE_COROUTINE 生成器（@types.coroutine），0.8.8 已统一语义
    # 并删除 recorder_bridge 的 hasattr 私有副本，勿退化。
    if inspect.isawaitable(value):
        return await value
    return value


async def build_history_text(
    *,
    umo: str,
    local_records: list[MessageRecord],
    read_history: ReadHistoryCallback,
    limit: int,
    min_text_records: int,
) -> str:
    """本地文本记录不足阈值时回宿主补历史，去重后按 limit 格式化。

    决策（decision.py）与生成（generation.py）共用同一形状（复审 S2），
    阈值与 limit 由调用方按各自配置决定。
    """
    records = local_records
    if count_text_records(records) < min_text_records:
        try:
            records = (await read_history(umo, limit=limit)) + records
        except Exception as exc:
            logger.debug("[%s] host history unavailable session=%s error=%s", PLUGIN_ID, umo, exc)
    return format_message_records(dedupe_message_records(records), limit=limit)


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


def parse_decision_json(text: str) -> dict[str, Any] | None:
    """严格解析判断模型的 JSON 响应，带类型校验和规范化。

    Args:
        text: 判断模型返回的原始文本

    Returns:
        规范化后的字典 {"should_reply": bool, "reason": str}，解析失败返回 None
    """
    parsed = parse_json(text)
    if not isinstance(parsed, dict):
        return None

    # 必须包含 should_reply
    if "should_reply" not in parsed:
        return None

    # 规范化 should_reply 为布尔值
    raw_reply = parsed["should_reply"]
    if isinstance(raw_reply, bool):
        should_reply = raw_reply
    elif isinstance(raw_reply, str):
        should_reply = raw_reply.strip().lower() in {"true", "yes", "1", "是"}
    elif isinstance(raw_reply, (int, float)):
        should_reply = bool(raw_reply)
    else:
        return None  # 无效类型

    # 规范化 reason
    reason = str(parsed.get("reason") or "未提供理由").strip()
    if len(reason) > 200:
        reason = reason[:200] + "..."

    return {
        "should_reply": should_reply,
        "reason": reason,
    }


def content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("text"):
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


def whitelist_storage_key(umo: str) -> str:
    """状态键就是完整 UMO 本身——本函数刻意是个恒等式（仅去空白）。

    诚实说明：原文写「Return a platform-aware state key」，
    并称"裸群号仍作为遗留通配白名单项被接受"。两句都不是本函数做的事——
    它没有任何平台感知逻辑，裸群号通配是**邻居** ``session_whitelisted``
    的行为（见上方 ``session_group_id`` 回退分支）。文档描述了邻居的职责，
    读者会以为这里有归一化逻辑而去找，找不到。

    它的价值不在做了什么，而在它作为**唯一命名接缝**存在：9 个调用点
    （main / scheduler / storage / whitelist）全部经由它取状态键，于是
    「状态键 = 完整 UMO」这个决定只有一处可改。

    为什么绝不能把它退化成裸群号：``session_whitelisted`` 接受裸群号作为
    通配匹配，若状态键也退化到群号，两个平台上**同号**的群会共用一条状态
    记录，配额与冷却互相污染（``tests/test_security.py`` 就钉这一条）。
    """
    return str(umo or "").strip()


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


def event_extra(event: AstrMessageEvent, key: str, default: Any = None) -> Any:
    """读取宿主事件的 extra 字段，跨宿主签名差异做三层回退。

    与本模块其余 ``event_*`` 同属宿主字段兼容探测（0.9.3 自 main.py 外迁）。
    回退阶梯：无 ``get_extra`` → 默认值；可检查签名时先预绑定双参，若仅单参
    可绑定则调用 ``get_extra(key)``；无法检查签名时仅尝试双参一次；仍失败或取到
    None → 默认值。
    """
    get_extra = getattr(event, "get_extra", None)
    if not callable(get_extra):
        return default
    args: tuple[Any, ...]
    try:
        signature = inspect.signature(get_extra)
    except (TypeError, ValueError):
        args = (key, default)
    else:
        try:
            signature.bind(key, default)
            args = (key, default)
        except TypeError:
            try:
                signature.bind(key)
            except TypeError:
                return default
            args = (key,)
    try:
        value = get_extra(*args)
    except Exception:
        return default
    return default if value is None else value


def response_text(response: Any) -> str:
    """从宿主响应对象提取纯文本：completion_text 优先，result_chain.get_plain_text 兜底。

    0.8.8 三处镜像（decision/generation/parser）统一至此；get_plain_text 异常
    兜底为空串（原 decision 版异常会传播，统一后更稳，原因文案由调用方判定）。
    """
    text = str(getattr(response, "completion_text", "") or "").strip()
    if text:
        return text
    chain = getattr(response, "result_chain", None)
    getter = getattr(chain, "get_plain_text", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "").strip()
    except Exception:
        return ""


def is_self_message(event: AstrMessageEvent) -> bool:
    sender = event_sender_id(event)
    self_id = event_self_id(event)
    return bool(sender and self_id and sender == self_id)


def is_admin_event(event: AstrMessageEvent, admin_ids: set[str]) -> bool:
    """管理员判定：宿主 API → role 字段 → 配置白名单，三级兜底。

    失败方向必须是 fail-safe（判为非管理员），不得为便利改成 ``return True``。
    """
    try:
        if event.is_admin():
            return True
    except Exception:
        # 宿主未实现或实现异常时不在此判定结果，继续走下面的 role / admin_ids
        # 兜底链；三级全不命中才算非管理员。降级方向是收紧权限而非放开。
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
    # 去空白后硬截断：超长畸形输入（如粘贴长文本）只需检测头部语义，
    # 同时避免超长输入喂给后续正则造成线性放大。
    return re.sub(r"\s+", "", str(text or "").lower())[:200]


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


def should_ignore_event(
    event: Any,
    text: str,
    *,
    vision_has_images: bool,
    ignored_sender_ids: set[str],
) -> bool:
    """判定一条消息是否应被忽略（不进主动回复观察）。（0.9.0 B4 自 events.py 并入）

    - 机器人自己的消息忽略
    - 以 ``/`` 开头的命令消息忽略
    - 纯图片消息没有文本，但在识图开启时仍需观察，否则图片无法进入缓存
    - 忽略名单中的发送者忽略
    - 直接点名（@Bot）的请求消息忽略（由回复请求窗口另行处理）
    """
    if is_self_message(event):
        return True
    if text.startswith("/"):
        return True
    if not text and not vision_has_images:
        return True
    if event_sender_id(event) in ignored_sender_ids:
        return True
    return is_explicit_direct_call(event, text)


def clean_reply(text: str, *, allow_multiline: bool, max_chars: int) -> str:
    text = str(text or "").strip()
    text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^(?:回复|答复)\s*[:：]\s*", "", text).strip()

    # 整条回复就是工具标记时直接丢弃
    if _TOOL_CALL_LEAK_PATTERN.match(text):
        return ""

    # 移除文本中所有位置的工具标记及其同行残留
    text = _TOOL_CALL_INLINE_PATTERN.sub("", text)

    if allow_multiline:
        # 保留换行结构，只压缩行内空白并丢弃因过滤而变空的行
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [_INLINE_SPACE_PATTERN.sub(" ", line).strip() for line in text.split("\n")]
        text = "\n".join(line for line in lines if line).strip()
    else:
        text = _WHITESPACE_PATTERN.sub(" ", text).strip()

    if not text:
        return ""

    # max_chars 为 0 时视为无限制
    if max_chars > 0 and len(text) > max_chars:
        clipped = text[:max_chars].rstrip()
        match = re.search(r"^([\s\S]*[。！？.!?])[^。！？.!?]*$", clipped)
        text = (match.group(1) if match else clipped).strip()

    return text
