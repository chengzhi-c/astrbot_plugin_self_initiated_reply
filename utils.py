"""宿主事件字段提取与消息文本纯函数库。

全部函数无状态（仅依赖入参），供 main/scheduler/decision 与测试共用。
职责四簇：
- 事件字段提取：event_text/event_umo/event_sender_* 等，统一宿主事件访问口径；
- 文本清洗与判定：clean_chat_text/is_at_* 等；
- 白名单与历史记录：session_whitelisted/whitelist_storage_key/build_history_text 等；
- 凭证脱敏：redact_url/redact_exc_text，供日志与对外 reason 共用同一口径。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At

from .models import (
    FALLBACK_SENDER_NAME,
    INLINE_SPACE_PATTERN,
    PLUGIN_ID,
    WHITESPACE_PATTERN,
    MessageRecord,
    ReadHistoryCallback,
    history_display_name,
)

# 预编译正则以避免热路径反复编译；空白类两个由 models 持有（依赖方向）。
# is_explicit_direct_call 的两条「@ 提及」模式由 self_id 拼出，只能在调用点构造。
# 只剥离 `[At:<id>]` 这一种前缀形态：宽松写法会把 `[chat]` 等正文方括号块
# 一并从历史文本里吃掉，用户侧表现为消息前缀静默丢失。
_AT_MENTION_PATTERN = re.compile(r"^(?:\[At:[^\]]+\]\s*)+", re.IGNORECASE)
_CQ_AT_PATTERN = re.compile(r"^(?:\[CQ:at,[^\]]+\]\s*)+")
_TEXT_AT_PATTERN = re.compile(r"^(?:@\S+\s*)+")
_INLINE_AT_PATTERN = re.compile(r"\[CQ:at,[^\]]+\]")
_INLINE_MENTION_PATTERN = re.compile(r"\[At:[^\]]+\]")
_TOOL_CALL_LEAK_PATTERN = re.compile(r"^\s*\[(?:historical )?tool call\]", re.IGNORECASE)
# 工具标记及其同行残留（不跨行，避免吃掉后续正常内容）
_TOOL_CALL_INLINE_PATTERN = re.compile(r"\[(?:historical\s+)?tool\s+call\][^\n]*", re.IGNORECASE)
# 提示词/回复文本的剥离正则：静态模式，提前编译。
_TRUNCATED_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_JSON_BODY_PATTERN = re.compile(r"\{[\s\S]*\}")
_REPLY_FENCE_PATTERN = re.compile(r"^```(?:text)?\s*|\s*```$", re.IGNORECASE)
_REPLY_PREFIX_PATTERN = re.compile(r"^(?:回复|答复)\s*[:：]\s*")
_SENTENCE_TAIL_PATTERN = re.compile(r"^([\s\S]*[。！？.!?])[^。！？.!?]*$")

# 日志/对外文本中 URL 的最大呈现长度（含脱敏标记）。
LOG_URL_MAX_CHARS = 80
# 判断模型 JSON 里的布尔字面量集合，刻意窄于 models.as_bool（配置面认
# on/启用，模型侧不认）。should_reply 与 quote 共用这一份判定。
MODEL_JSON_TRUE_WORDS = frozenset({"true", "yes", "1", "是"})
MODEL_JSON_FALSE_WORDS = frozenset({"false", "no", "0", "否"})
_REDACTED_QUERY_MARK = "?<redacted>"
# 凭证运行的最小形态：``&key=`` / ``;key=`` 起头的一段。query 之外的位置
# （netloc、path）出现它同样是凭证——urlparse 不会把它归进 query，而原实现
# 原样复制 netloc 与 path，``https://h/v1/chat&api_key=sk-...`` 之类因此
# 整段幸存（实测占泄漏输入的最大一类）。
_AUTHORITY_KEY_RUN = re.compile(r"[&;][^\s/]*=")
# 判断模型 reason 的对外呈现长度上限：超长截断，只进日志/前端展示。
DECISION_REASON_MAX_CHARS = 200
# UMO（unified message object）为 platform:message_type:session_id 三段式。
_UMO_PARTS = 3

# 异常文本里的 URL 形态：scheme:// 开头，吃到引号/空白/尖括号为止。
# 只认这一种形态（实证泄漏全部来自它），不做全文 secret 扫描。
# 字符类刻意不收 `)` 与 `]`：IPv6 字面量主机的 `]`（http://[::1]:11434/...?
# api_key=...）或路径里的 `)` 会截断匹配，query 凭证原样幸存；而把两者放进
# 类内只可能多吞一个紧随其后的散文括号，与既有的 `,`/`.` 同罪。
_EXCEPTION_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")

# 同上的**相对路径**形态：urllib3 的 MaxRetryError 把请求 URL 拆成
# "... Max retries exceeded with url: /v1/chat?api_key=sk-...&x=1 (Caused by ...)"，
# 没有 scheme://，上面的模式匹配不到，而它照样把 query 凭证带进 reason 并经
# GET /status 出网。只认「以 / 开头且带 query」的片段，且前一个字符不能是
# 单词字符或斜杠——否则会切进绝对 URL 的 path 部分重复脱敏，也会误伤
# ``https://`` 的那两个斜杠。右界同绝对形态：不收括号，路径里的 `)` 不该
# 成为 query 的泄密口（真实右界是空白）。可选的 ``host:port`` 前缀收
# ``127.0.0.1:11434/v1/chat?api_key=...`` 这种不带 scheme 的本机端点写法。
_EXCEPTION_RELATIVE_URL_PATTERN = re.compile(
    r"(?<![\w/])(?:[\w.\-]+:\d+)?/[^\s'\"<>]*\?[^\s'\"<>]+"
)


def _redact_relative_url(text: str) -> str:
    """相对路径 URL：保留路径，query 与 fragment 一律抹掉。

    与 ``redact_url`` 分开是因为后者要求 scheme+netloc，路径形态会被它原样
    放行（只截断），凭证因此漏出去。截断预算与 ``redact_url`` 同一口径：
    这段文本同样经 ``redact_exc_text`` 进 reason 并由 GET /status 出网。
    """
    cut = len(text)
    for mark in ("?", "#"):
        index = text.find(mark)
        if index != -1:
            cut = min(cut, index)
    return text[:cut][: LOG_URL_MAX_CHARS - len(_REDACTED_QUERY_MARK)] + _REDACTED_QUERY_MARK


def safe_exc_text(exc: BaseException) -> str:
    """异常文本提取的单点收口：``__str__`` 本身抛异常时退化为类型名。

    在投递路径的 ``except`` 块里构造 ``SendOutcome(status, str(exc))`` 时，
    二次抛出会跳过账本记账，消息若已提交，下一轮会重复发送。
    调用方不得直接 ``str(exc)``。
    """
    try:
        return str(exc)
    except Exception:
        return type(exc).__name__


def _redact_url_fallback(text: str) -> str:
    """urlparse 分解不了（畸形 IPv6 等）或没有 netloc 时的脱敏兜底。

    与 happy path 同一口径：去 userinfo、去 query/fragment、去 ``&key=`` 运行、
    截断到 ``LOG_URL_MAX_CHARS``。全部按字符串边界做，不依赖 urlparse——
    分解失败恰恰是最需要脱敏的形状（残缺 IPv6 主机带着 userinfo 与 query）。
    """
    scheme_end = text.find("://")
    authority_start = scheme_end + 3 if scheme_end != -1 else 0
    authority_end = len(text)
    for mark in ("/", "?", "#"):
        index = text.find(mark, authority_start)
        if index != -1:
            authority_end = min(authority_end, index)
    at = text.rfind("@", authority_start, authority_end)
    if at != -1:
        text = text[:authority_start] + text[at + 1 :]
        authority_end -= at + 1 - authority_start
    cut = len(text)
    for mark in ("?", "#"):
        index = text.find(mark)
        if index != -1:
            cut = min(cut, index)
    key_run = _AUTHORITY_KEY_RUN.search(text[:cut])
    if key_run:
        cut = min(cut, key_run.start())
    suffix = _REDACTED_QUERY_MARK if cut < len(text) else ""
    return text[:cut][: LOG_URL_MAX_CHARS - len(suffix)] + suffix


def redact_url(value: str) -> str:
    """去掉 userinfo 与 query/fragment 后再截断，避免凭证进日志或对外文本。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        # 原样返回等于把 userinfo 与 query 一起放行：分解失败不改变这段文本
        # 可疑的事实，改走同口径的边界兜底。
        return _redact_url_fallback(text)
    if not parsed.scheme or not parsed.netloc:
        return _redact_url_fallback(text)
    # netloc 含 user:password@ 形态的 userinfo，必须去掉；从 netloc 尾部截取
    # 而非拼 hostname+port：后者要处理 IPv6 方括号，且 ``parsed.port`` 对
    # 越界/非数字端口抛 ValueError，本函数跑在异常处理路径上。
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    clean = f"{parsed.scheme}://{netloc}{parsed.path}"
    # ``&key=`` / ``;key=`` 运行可能落在 netloc 或 path（urlparse 不把它们归进
    # query）：出现即视为凭证段整体截掉，宁可多截也不留 key。
    key_run = _AUTHORITY_KEY_RUN.search(clean)
    if key_run:
        clean = clean[: key_run.start()]
    suffix = _REDACTED_QUERY_MARK if (parsed.query or parsed.fragment or key_run) else ""
    return clean[: LOG_URL_MAX_CHARS - len(suffix)] + suffix


def redact_exc_text(exc: BaseException) -> str:
    """把异常文本里 URL 形态片段的 userinfo 与 query/fragment 抹掉。

    provider SDK 的异常串常把请求 URL 整段带出来（含 api_key/Signature 等
    query 凭证）。这些文本既进日志，也进 ``reason`` 经 GET /status 出网，
    故统一在此收口。非 URL 片段原样保留，便于排查。

    两种 URL 形态都要收：绝对 URL（scheme://）与相对路径（urllib3
    ``MaxRetryError`` 的 ``url: /path?key=...``）。漏掉后者时凭证照样经
    reason 出网，而它恰恰是 httpx/requests/urllib3 连接失败的标准文本。
    """
    text = _EXCEPTION_URL_PATTERN.sub(lambda m: redact_url(m.group(0)), safe_exc_text(exc))
    return _EXCEPTION_RELATIVE_URL_PATTERN.sub(lambda m: _redact_relative_url(m.group(0)), text)


async def maybe_await(value: Any) -> Any:
    # 用 inspect.isawaitable 而非 hasattr(value, "__await__")：后者会漏
    # CO_ITERABLE_COROUTINE 生成器（@types.coroutine）。
    if inspect.isawaitable(value):
        return await value
    return value


def consume_task_result(task_or_future: asyncio.Future[Any]) -> None:
    """取回已结束 future 的结果，抑制 "Task exception was never retrieved"。

    超时/取消路径里由收敛方显式消费一次：任务若以异常收尾，异常会被记账
    到该路径的日志，而不是在垃圾回收时以无归属的循环级 ERROR 出现。
    """
    if task_or_future.cancelled():
        return
    try:
        task_or_future.exception()
    except Exception:
        # 取结果本身不应成为新的失败源
        pass


async def build_history_text(
    *,
    umo: str,
    local_records: list[MessageRecord],
    read_history: ReadHistoryCallback,
    limit: int,
    min_text_records: int,
) -> str:
    """本地文本记录不足阈值时回宿主补历史，去重后按 limit 格式化。

    决策（decision.py）与生成（generation.py）共用同一形状，
    阈值与 limit 由调用方按各自配置决定。
    """
    records = local_records
    if count_text_records(records) < min_text_records:
        try:
            records = (await read_history(umo, limit=limit)) + records
        except Exception as exc:
            logger.debug(
                "[%s] host history unavailable session=%s error=%s",
                PLUGIN_ID,
                umo,
                redact_exc_text(exc),
            )
    # 先按时间取窗口、再去重：dedupe 把重复文本合并到**最早**出现的位置，先去重
    # 会让最新一条（内容与更早某条重复时，聊天里"1"/"哈哈"极常见）被抬到列表
    # 前部、随后被尾部截断丢掉——判断与生成共用这条路径，最新消息因此从提示词里
    # 消失（契约 §14.3 的"保尾"要求）。
    windowed = records[-limit:] if limit > 0 else []
    return format_message_records(dedupe_message_records(windowed), limit=limit)


def parse_json(text: str) -> Any:
    text = str(text or "").strip()
    text = _TRUNCATED_JSON_FENCE_PATTERN.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BODY_PATTERN.search(text)
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

    # 规范化 should_reply 为布尔值（字面量集合单源见 MODEL_JSON_TRUE_WORDS）。
    raw_reply = parsed["should_reply"]
    if isinstance(raw_reply, bool):
        should_reply = raw_reply
    elif isinstance(raw_reply, str):
        should_reply = raw_reply.strip().lower() in MODEL_JSON_TRUE_WORDS
    elif isinstance(raw_reply, (int, float)):
        should_reply = bool(raw_reply)
    else:
        return None  # 无效类型

    # 规范化 reason
    reason = str(parsed.get("reason") or "未提供理由").strip()
    if len(reason) > DECISION_REASON_MAX_CHARS:
        reason = reason[:DECISION_REASON_MAX_CHARS] + "..."

    # 引用决定（可选字段）：只在模型显式给出可辨识的布尔时才采纳，其余一律
    # None =「模型没说」，交由投递侧按 quote_mode 兜底。刻意不像 should_reply
    # 那样把无法解析判为整条无效，一个坏字段不该废掉整次判断。
    raw_quote = parsed.get("quote")
    quote: bool | None
    if isinstance(raw_quote, bool):
        quote = raw_quote
    elif isinstance(raw_quote, str):
        normalized = raw_quote.strip().lower()
        quote = (
            True
            if normalized in MODEL_JSON_TRUE_WORDS
            else (False if normalized in MODEL_JSON_FALSE_WORDS else None)
        )
    elif isinstance(raw_quote, (int, float)):
        quote = bool(raw_quote)
    else:
        quote = None

    return {
        "should_reply": should_reply,
        "reason": reason,
        "quote": quote,
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
    parts = raw.split(":", _UMO_PARTS - 1)
    if len(parts) < _UMO_PARTS:
        return raw
    platform, msg_type, session_id = parts
    group_id = event_group_id(event)
    if group_id and "group" in msg_type.lower():
        return f"{platform}:{msg_type}:{group_id}"
    return f"{platform}:{msg_type}:{session_id.strip()}"


def is_full_umo(value: str) -> bool:
    """是否 ``platform:message_type:session_id`` **恰好三段**的完整 UMO。

    与 :func:`session_group_id` 同源但不等价：会话 ID 自身含冒号时两者结论
    相反。该分歧是良性的：入口按白名单项**逐字**登记 ``_whitelist_runtime_umos``，
    走裸号分支照样能查到它（tests/test_storage_and_umo.py 的分歧守卫用例）。
    不要"顺手统一"成 ``>=``：那会把两段畸形条当成可直接巡检的 UMO。
    """
    return str(value or "").count(":") == _UMO_PARTS - 1


def session_group_id(umo: str) -> str:
    parts = str(umo or "").strip().split(":", _UMO_PARTS - 1)
    if len(parts) == _UMO_PARTS and "group" in parts[1].lower():
        return parts[2].strip()
    return ""


def session_is_private(umo: str) -> bool:
    """非群会话视为私聊，不写死 FriendMessage。"""
    return not session_group_id(umo)


def session_whitelisted(umo: str, whitelist: set[str]) -> bool:
    normalized = str(umo or "").strip()
    if not normalized:
        return False
    if normalized in whitelist:
        return True
    group_id = session_group_id(normalized)
    return bool(group_id and group_id in whitelist)


def whitelist_storage_key(umo: str) -> str:
    """状态键就是完整 UMO 本身（仅去空白）。价值在作为**唯一命名接缝**存在：
    「状态键 = 完整 UMO」这个决定只有一处可改。绝不能退化成裸群号：
    ``session_whitelisted`` 接受裸群号通配，状态键若同样退化，两个平台上
    同号的群会共用一条状态记录，配额与冷却互相污染（tests/test_security.py 钉住）。
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


def event_message_id(event: Any) -> str:
    """消息 ID 的唯一取值口径（图片缓存去重与主动回复引用共用）。

    三层回退：事件自身字段 → ``message_obj`` 字段 → ``get_message_id()``；
    取不到返回空串走「无 ID」路径，ID 只用于去重与引用。
    """
    for owner in (event, getattr(event, "message_obj", None)):
        if owner is None:
            continue
        for name in ("message_id", "msg_id"):
            value = getattr(owner, name, None)
            if value:
                return str(value).strip()
    getter = getattr(event, "get_message_id", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            # 宿主 get_message_id 可能依赖已失效的连接态。
            return ""
    return ""


def event_sender_name(event: AstrMessageEvent) -> str:
    try:
        return str(event.get_sender_name() or event.get_sender_id() or FALLBACK_SENDER_NAME)
    except Exception:
        return FALLBACK_SENDER_NAME


def event_extra(event: AstrMessageEvent, key: str, default: Any = None) -> Any:
    """读取宿主事件的 extra 字段，跨宿主签名差异做两级调用回退。

    本函数在消息热路径上，不用 ``first_bindable_args`` 的签名预检（那要把
    签名解析开销花在每条消息上）；``get_extra`` 是纯读，重复读取无害。
    有副作用风险的宿主调用（LLM、落盘）仍走 ``first_bindable_args``，
    "预检绝不调用"的契约不变。
    """
    get_extra = getattr(event, "get_extra", None)
    if not callable(get_extra):
        return default
    try:
        value = get_extra(key, default)
    except TypeError:
        try:
            value = get_extra(key)
        except Exception:
            return default
    except Exception:
        return default
    return default if value is None else value


def response_text(response: Any) -> str:
    """从宿主响应对象提取纯文本：completion_text 优先，result_chain.get_plain_text 兜底。
    三处镜像（decision/generation/parser）统一至此。"""
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
    失败方向必须是 fail-safe（判为非管理员）。"""
    try:
        if event.is_admin():
            return True
    except Exception:
        # 继续走 role / admin_ids 兜底链，降级方向是收紧权限而非放开。
        pass
    # 宿主 ``AstrMessageEvent`` 只有 ``role``，不兜 ``role_type`` 这个不存在的属性。
    role = str(getattr(event, "role", "")).lower()
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
        if re.search(
            rf"\[CQ:at,[^\]]*(?:qq=)?(?<!\w){re.escape(self_id)}(?:\W|$)", text, re.IGNORECASE
        ):
            # (?<!\w)/(?:\W|$)：两侧都要求标识符边界。只挡数字一侧时
            # [CQ:at,qq=abc123def] 仍会以 self_id=123 误判点名。
            return True
        for comp in event_components(event):
            if isinstance(comp, At) and str(getattr(comp, "qq", "")).strip() == self_id:
                return True
    return False


def collapse_whitespace(text: Any) -> str:
    """把连续空白压成单空格并去首尾。历史去重与工具直发去重共用。"""
    return WHITESPACE_PATTERN.sub(" ", str(text or "")).strip()


def strip_leading_mentions(text: str) -> str:
    raw = str(text or "").strip()
    raw = _AT_MENTION_PATTERN.sub("", raw).strip()
    raw = _CQ_AT_PATTERN.sub("", raw).strip()
    return _TEXT_AT_PATTERN.sub("", raw).strip()


def clean_chat_text(text: str) -> str:
    raw = strip_leading_mentions(text)
    raw = _INLINE_AT_PATTERN.sub("", raw)
    raw = _INLINE_MENTION_PATTERN.sub("", raw)
    return collapse_whitespace(raw)


def dedupe_message_records(records: list[MessageRecord]) -> list[MessageRecord]:
    deduped: list[MessageRecord] = []
    index: dict[tuple[str, str], int] = {}
    for item in records:
        text = collapse_whitespace(item.text)
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
    # limit <= 0 时 records[-0:] 是全量而不是空表（adapters 的同形状分支已显式
    # 兜住，此处同口径）。
    rows = records[-limit:] if limit > 0 else []
    if not rows:
        return "(无)"
    lines = []
    for item in rows:
        name = history_display_name(item.role, item.name)
        lines.append(f"{name}: {item.text}")
    return "\n".join(lines)


def cap_context_text(text: str, max_chars: int, *, marker: str) -> str:
    """总字符预算内的保尾裁剪：历史越新越重要，超限只裁更早部分。

    从行边界起裁，加一行 ``marker`` 提示省略。两个例外：
    ``max_chars <= 0`` 表示关闭裁剪；预算装不下 marker 本身时返回 marker
    （空串会让调用方以为没有历史，提示「内容被省略」更接近事实）。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    lines = text.splitlines()
    budget = max_chars - len(marker) - 1  # -1 为 marker 与正文间的换行
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        addition = len(line) + (1 if kept else 0)
        if used + addition > budget:
            break
        kept.insert(0, line)
        used += addition
    if not kept:
        # 连一行都放不下：按字符保尾，至少让预算不为空转。
        kept = [text[-budget:]] if budget > 0 else []
    return "\n".join([marker, *kept])


def cap_command_reply(text: str, max_chars: int) -> str:
    """指令回显的字符预算：超限时保头裁尾，并说明丢了多少字符。

    与 ``cap_context_text`` 方向相反——回显的第一行是结论（是否运行中、是否
    已加入白名单），裁掉它比裁掉尾部明细更糟。按行边界裁，避免吐出半行；
    一行都放不下时按字符保头，至少让预算不为空转。

    ``max_chars <= 0`` 表示关闭裁剪。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = f"…(回显过长，已省略末尾 {len(text) - max_chars} 字符)"
    budget = max_chars - len(marker) - 1  # -1 为 marker 与正文间的换行
    if budget <= 0:
        return marker
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        addition = len(line) + (1 if kept else 0)
        if used + addition > budget:
            break
        kept.append(line)
        used += addition
    if not kept:
        kept = [text[:budget]]
    return "\n".join([*kept, marker])


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
    """判定一条消息是否应被忽略（不进主动回复观察）。

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
    text = _REPLY_FENCE_PATTERN.sub("", text).strip()
    text = _REPLY_PREFIX_PATTERN.sub("", text).strip()

    # 整条回复就是工具标记时直接丢弃
    if _TOOL_CALL_LEAK_PATTERN.match(text):
        return ""

    # 移除文本中所有位置的工具标记及其同行残留
    text = _TOOL_CALL_INLINE_PATTERN.sub("", text)

    if allow_multiline:
        # 保留换行结构，只压缩行内空白并丢弃因过滤而变空的行
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [INLINE_SPACE_PATTERN.sub(" ", line).strip() for line in text.split("\n")]
        text = "\n".join(line for line in lines if line).strip()
    else:
        text = collapse_whitespace(text)

    if not text:
        return ""

    # max_chars 为 0 时视为无限制
    if max_chars > 0 and len(text) > max_chars:
        clipped = text[:max_chars].rstrip()
        match = _SENTENCE_TAIL_PATTERN.search(clipped)
        text = (match.group(1) if match else clipped).strip()

    return text


async def await_rollback(restore: asyncio.Future[None]) -> None:
    """等回滚任务跑完再返回，取消期间也不放弃。

    ``done()`` 是退出条件而不是 ``break``：调用方已被取消过一次，之后每次
    await 都可能立刻再抛 CancelledError，包括回滚已经跑完的那一刻。enabled 双写、
    白名单、配置整包应用三条回滚路径共用这一条等待纪律。
    """
    while not restore.done():
        try:
            await asyncio.shield(restore)
        except asyncio.CancelledError:
            continue
