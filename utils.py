"""宿主事件字段提取与消息文本纯函数库。

全部函数无状态（仅依赖入参），供 main/scheduler/decision 与测试共用。
职责四簇：
- 事件字段提取：event_text/event_umo/event_sender_* 等，统一宿主事件访问口径；
- 文本清洗与判定：strip_leading_mentions/is_at_* 等；
- 白名单与历史记录：session_whitelisted/whitelist_storage_key/build_history_text 等；
- 凭证脱敏：redact_url/redact_exc_text，供日志与对外 reason 共用同一口径。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from collections.abc import Iterator
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
_TOOL_CALL_LEAK_PATTERN = re.compile(r"^\s*\[(?:historical )?tool call\]", re.IGNORECASE)
# 工具标记及其同行残留（不跨行，避免吃掉后续正常内容）
_TOOL_CALL_INLINE_PATTERN = re.compile(r"\[(?:historical\s+)?tool\s+call\][^\n]*", re.IGNORECASE)
# 提示词/回复文本的剥离正则：静态模式，提前编译。
_TRUNCATED_JSON_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
# 判断模型输出的 JSON 定位器：从每个左花括号起点 raw_decode 试解，字符串内花括号、
# 转义引号由标准库处理（贪婪正则与括号计数都会在这两处截错）。
_JSON_DECODER = json.JSONDecoder()
# raw_decode 失败时标准库按 doc.count("\n", 0, pos) 算行列号构造 JSONDecodeError，
# 单次失败 O(n)，文本里 n 个左花括号就是 O(n²) 同步卡死事件循环。真正的成本是每次
# 尝试从起点扫到失败点（未闭合的嵌套会扫到文末）的字符量，与对象个数无关：模型先铺
# 若干示例对象属于正常输出，按对象数封顶会把「合法 JSON 在后段」误判成没有 JSON。
# 故主预算按累计扫描字符计，次数只留作防死循环的宽松兜底。
_JSON_SCAN_MAX_ATTEMPTS = 2048
# 累计扫描字符预算 = min(len(text) * 倍数, 绝对上限)：正常噪声只占全文一小部分，倍数
# 给「几乎全文都是候选」的长输出留余量；绝对上限让再长的文本也不放大单轮成本（实测
# 2048 次尝试 ~5 ms；未闭合的长字符串一次就扫到文末，单次即越预算）。
_JSON_SCAN_BUDGET_FACTOR = 8
_JSON_SCAN_BUDGET_MAX_CHARS = 200_000
_REPLY_FENCE_PATTERN = re.compile(r"^```(?:text)?\s*|\s*```$", re.IGNORECASE)
_REPLY_PREFIX_PATTERN = re.compile(r"^(?:回复|答复)\s*[:：]\s*")
# 控制字符不是空白：``\s`` 不含 \x00-\x08 与 \x0e-\x1f，回复里出现会原样发到
# 平台（部分平台遇 \x00 直接截断消息）。\t 与换行保留，换行还要给多行模式用。
_REPLY_CONTROL_REMOVAL = {
    **{code: None for code in range(0x00, 0x09)},
    **{code: None for code in (0x0B, 0x0C, 0x7F)},
    **{code: None for code in range(0x0E, 0x20)},
    # C1 与 DEL 同属控制字符，模型转述乱码时也会带出来。
    **{code: None for code in range(0x80, 0xA0)},
}
# 历史正文里的换行会把一条消息拆成提示词里的多行，群友据此伪造独立轮次（一行
# "SYSTEM: ..." 或另一条 "Bot: ..."）。发言人名字侧已单行化（history_display_name），
# 正文侧必须对称；只在渲染成提示词时折行，存储的原文不受影响。
# 字符集对齐 str.splitlines()：只认 CR/LF 时 U+2028 之类仍能裂出独立一行。
_LINEBREAK_RUN_PATTERN = re.compile(r"\s*[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+\s*")
_SENTENCE_TAIL_PATTERN = re.compile(r"^([\s\S]*[。！？.!?])[^。！？.!?]*$")

# 日志/对外文本中 URL 的最大呈现长度（含脱敏标记）。
LOG_URL_MAX_CHARS = 80
# 判断模型 JSON 里的布尔字面量集合，刻意窄于 models.as_bool（配置面认
# on/启用，模型侧不认）。should_reply 与 quote 共用这一份判定。
MODEL_JSON_TRUE_WORDS = frozenset({"true", "yes", "1", "是"})
MODEL_JSON_FALSE_WORDS = frozenset({"false", "no", "0", "否"})
_REDACTED_QUERY_MARK = "?<redacted>"
_REDACTED_PATH_MARK = "/<redacted>"
# 凭证运行的最小形态：``&key=`` / ``;key=`` 起头的一段。query 之外的位置
# （netloc、path）出现它同样是凭证：urlparse 不会把它归进 query，只按 query
# 处理会让 ``https://h/v1/chat&api_key=sk-...`` 整段幸存，这是实测占泄漏
# 输入最多的一类。
_AUTHORITY_KEY_RUN = re.compile(r"[&;][^\s/]*=")
# 日志兜底文本里的 C0 控制字符（含 CR/LF）与 DEL 一律剔除：换行能把一段
# 可疑 URL 拆成独立日志行，其余 C0 在 URL 里没有合法含义。兜底分支与
# netloc 都要过一遍——urlparse 只剥 \t\r\n，剥不掉 \x00 / \x0b / \x7f。
# C1（0x80–0x9F）、U+2028 / U+2029 与 C0 同样能把一行拆成两行：``str.splitlines``
# 与 ``\s`` 都把它们当换行，只挡 C0 时 U+2028 仍可伪造后续日志行。
_LOG_CONTROL_REMOVAL = dict.fromkeys((*range(0x20), 0x7F, *range(0x80, 0xA0), 0x2028, 0x2029))
# 有 scheme 但缺 ``//`` 的畸形 URL（``http:/host/p?key=...``）与协议相对 URL：
# urlparse 都给不出 netloc，落入兜底分支。这类形状必须按 URL 处理（path 也要
# 抹掉），不能当本地路径原样放行。scheme 至少两个字符：单字母 ``C:`` 是
# Windows 盘符；``data:`` 这类不带 ``/`` 的 opaque scheme 也不是本模式。
_URL_SHAPED_PATTERN = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]+:/|//)")
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
# 不带 scheme、也不以 ``/`` 开头的裸 host/path 不单独建模式：按参数名抹值
# 已覆盖它的凭证，连带保留 host/path 更利于排查。
# SDK 异常也会携带请求头、JSON 或参数字典，凭证字段不能只按 URL 查询串识别。
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret"
    r"|client[_-]?secret|secret[_-]?key"
    r"|password|passwd|signature|sig|auth[_-]?key|key|rkey|authorization)"
    r"\b['\"]?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|(?:Bearer|Basic)\s+[^\s,'\"<>]+|[^\s&,'\"<>;}]+)"
)
# 无 scheme 的 ``user:pass@host``：切不出 URL 形状时密码整段幸存。邮箱没有冒号。
_USERINFO_PATTERN = re.compile(r"(?<![\w/])[^\s'\"<>@/:]+:[^\s'\"<>@/]+@")


def _redact_relative_url(text: str) -> str:
    """相对路径 URL：保留 host:port，路径与 query/fragment 一律抹掉。

    与 ``redact_url`` 分开是因为后者要求 scheme+netloc，路径形态会被它原样
    放行（只截断），凭证因此漏出去。截断预算与 ``redact_url`` 同一口径：
    这段文本同样经 ``redact_exc_text`` 进 reason 并由 GET /status 出网。
    """
    authority = text.split("/", 1)[0]
    return (
        authority[: LOG_URL_MAX_CHARS - len(_REDACTED_PATH_MARK) - len(_REDACTED_QUERY_MARK)]
        + _REDACTED_PATH_MARK
        + _REDACTED_QUERY_MARK
    )


def strip_log_control(text: Any) -> str:
    """日志插值前的控制字符剔除：换行能把一条日志拆成两行（伪造后续记录）。"""
    return str(text if text is not None else "").translate(_LOG_CONTROL_REMOVAL)


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

    与 happy path 同一口径：去 userinfo、路径、query/fragment 与 ``&key=`` 运行、
    截断到 ``LOG_URL_MAX_CHARS``。全部按字符串边界做，不依赖 urlparse——
    分解失败恰恰是最需要脱敏的形状（残缺 IPv6 主机带着 userinfo 与 query）。
    本地路径与 opaque scheme 没有 authority 可剥，只抹凭证承载面、保留文件名
    （``redact_url`` 的调用方也传落盘路径，日志要能定位到具体文件）。
    """
    # C0 控制字符先剔除再按字符边界重拼：\n / \r 原样落到 logger 输出会把
    # 一条日志变成两条（伪造行），其余 C0 在 URL 里没有合法含义。
    text = text.translate(_LOG_CONTROL_REMOVAL)
    if not _URL_SHAPED_PATTERN.match(text):
        cut = len(text)
        for mark in ("?", "#"):
            index = text.find(mark)
            if index != -1:
                cut = min(cut, index)
        head = text[:cut]
        # userinfo 只在第一个路径分隔符之前才算 userinfo。
        slash = head.find("/")
        at = head.rfind("@", 0, slash if slash != -1 else len(head))
        if at != -1:
            head = head[at + 1 :]
        key_run = _AUTHORITY_KEY_RUN.search(head)
        if key_run:
            head = head[: key_run.start()]
        suffix = _REDACTED_QUERY_MARK if cut < len(text) else ""
        return head[: LOG_URL_MAX_CHARS - len(suffix)] + suffix
    scheme_end = text.find("://")
    if scheme_end != -1:
        authority_start = scheme_end + 3
    elif text.startswith("//"):
        # 协议相对 URL（``//host/path?key=...``）没有 scheme，authority 从
        # 第二根斜杠之后开始；否则 host 会被当成空 authority 丢掉。
        authority_start = 2
    else:
        # 畸形 scheme（``http:/host/path?key=...``）：urlparse 认得出 scheme
        # 却给不出 netloc，按 ``:/`` 切出 host 仍能保留失败对象。
        colon = text.find(":/")
        authority_start = colon + 2 if colon > 0 else 0
    authority_end = len(text)
    for mark in ("/", "?", "#"):
        index = text.find(mark, authority_start)
        if index != -1:
            authority_end = min(authority_end, index)
    authority = text[authority_start:authority_end]
    at = authority.rfind("@")
    if at != -1:
        authority = authority[at + 1 :]
    key_run = _AUTHORITY_KEY_RUN.search(authority)
    if key_run:
        authority = authority[: key_run.start()]
    prefix = text[:authority_start]
    has_path = authority_end < len(text) and text[authority_end] == "/"
    has_query = "?" in text[authority_end:] or "#" in text[authority_end:] or key_run is not None
    suffix = (_REDACTED_PATH_MARK if has_path else "") + (_REDACTED_QUERY_MARK if has_query else "")
    return (prefix + authority)[: LOG_URL_MAX_CHARS - len(suffix)] + suffix


def redact_url(value: str) -> str:
    """去掉 userinfo、path 与 query/fragment 后再截断，避免凭证进日志或对外文本。"""
    # 先剔换行类字符再解析：``http://h\u2028x/a?k=v`` 的 netloc 里留着 U+2028 时，
    # 返回值能把一条日志拆成两行。
    text = str(value or "").translate(_LOG_CONTROL_REMOVAL).strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        # 原样返回等于把 userinfo 与 query 一起放行：分解失败不改变这段文本
        # 可疑的事实，改走同口径的边界兜底。
        return _redact_url_fallback(text)
    if not parsed.scheme or not parsed.netloc:
        # 无 netloc 不等于「不是 URL」：``http:/host/p``（畸形 scheme）与
        # ``//host/p``（协议相对）都能带 query 凭证，统一走边界兜底。
        return _redact_url_fallback(text)
    # netloc 含 user:password@ 形态的 userinfo，必须去掉；从 netloc 尾部截取
    # 而非拼 hostname+port：后者要处理 IPv6 方括号，且 ``parsed.port`` 对
    # 越界/非数字端口抛 ValueError，本函数跑在异常处理路径上。
    # netloc 还可能是 urlparse 不剥离的 C0（``\x0b``/``\x00``/``\x7f``），
    # 不剔除就能把一条日志拆成两行。
    netloc = parsed.netloc.rsplit("@", 1)[-1].translate(_LOG_CONTROL_REMOVAL)
    # ``&key=`` / ``;key=`` 运行可能落在 netloc（urlparse 不把它们归进 query）。
    key_run = _AUTHORITY_KEY_RUN.search(netloc)
    if key_run:
        netloc = netloc[: key_run.start()]
    clean = f"{parsed.scheme}://{netloc}"
    if parsed.path:
        clean += _REDACTED_PATH_MARK
    suffix = (
        _REDACTED_QUERY_MARK if (parsed.query or parsed.fragment or key_run is not None) else ""
    )
    return clean[: LOG_URL_MAX_CHARS - len(suffix)] + suffix


def redact_exc_text(exc: BaseException) -> str:
    """移除异常文本中的 URL 凭证、认证头和凭证字段值。

    provider SDK 的异常串常把请求 URL 整段带出来（含 api_key/Signature 等
    query 凭证）。这些文本既进日志，也进 ``reason`` 经 GET /status 出网，
    故统一在此收口。其余错误信息保留，便于排查。

    两种 URL 形态都要收：绝对 URL（scheme://）与相对路径（urllib3
    ``MaxRetryError`` 的 ``url: /path?key=...``）。漏掉后者时凭证照样经
    reason 出网，而它恰恰是 httpx/requests/urllib3 连接失败的标准文本。

    最后两步不依赖 URL 边界：按参数名抹值，再抹 ``user:pass@`` 的 userinfo。
    无路径的 ``host?key=…``、单标签主机、无 scheme 的 IPv6 都切不出 URL
    形状，靠这两步收口。
    """
    text = safe_exc_text(exc).translate(_LOG_CONTROL_REMOVAL)
    text = _EXCEPTION_URL_PATTERN.sub(lambda m: redact_url(m.group(0)), text)
    text = _EXCEPTION_RELATIVE_URL_PATTERN.sub(lambda m: _redact_relative_url(m.group(0)), text)
    text = _USERINFO_PATTERN.sub("<redacted>@", text)
    return _CREDENTIAL_PATTERN.sub(r"\1<redacted>", text)


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
    task_or_future.exception()


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
    records = dedupe_message_records(local_records)
    if count_text_records(records) < min_text_records:
        try:
            host_records = list(await read_history(umo, limit=limit))
        except Exception as exc:
            logger.debug(
                "[%s] host history unavailable session=%s error=%s",
                PLUGIN_ID,
                umo,
                redact_exc_text(exc),
            )
        else:
            host_keys = [(item.role, collapse_whitespace(item.text)) for item in host_records]
            cursor = 0
            for index, item in enumerate(records):
                try:
                    pos = host_keys.index((item.role, collapse_whitespace(item.text)), cursor)
                except ValueError:
                    # 首个未匹配项之后保留本地顺序，避免把新的重复短句并回旧消息。
                    host_records.extend(records[index:])
                    break
                host_records[pos] = item
                cursor = pos + 1
            records = host_records
    return format_message_records(records, limit=limit)


_MISSING = object()


def conversation_history(conversation: Any) -> list[Any]:
    """会话历史：老宿主是 ``history``（JSON 串），4.30 读结果是 ``content`` 列表。

    两个字段都取不到时抛 AttributeError，让调用方按「结构不认识」留痕，而
    不是静默失去上下文；形状不对同样抛，由调用方分档告警。
    """
    raw = getattr(conversation, "history", _MISSING)
    if raw is _MISSING:
        raw = getattr(conversation, "content", _MISSING)
    if raw is _MISSING:
        raise AttributeError("conversation has no history/content field")
    if isinstance(raw, list):
        return raw
    if raw is None:
        # 4.30 的读结果在没请求历史时 content 为 None。
        return []
    if not isinstance(raw, str):
        raise TypeError(f"history is {type(raw).__name__}, expected str or list")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"history is {type(parsed).__name__}, expected a list")
    return parsed


def usable_conversation(conversation: Any) -> bool:
    """能否回填给 ``req.conversation``：宿主 build 用它建 event writer 并读 cid。"""
    return conversation is not None and hasattr(conversation, "cid")


def _iter_json_values(text: str) -> Iterator[Any]:
    """从每个左花括号起点尝试 ``raw_decode``，产出所有能独立解析的 JSON 值。

    模型输出会在 JSON 前后夹解释文字、连排多个对象，正则贪婪匹配（或括号计数）
    都会把整段并成一个无效 JSON。``raw_decode`` 允许从任意下标起解，且由标准库
    自己处理字符串内花括号与转义引号，不需要外部猜边界。
    """
    attempts = 0
    scanned = 0
    budget = min(len(text) * _JSON_SCAN_BUDGET_FACTOR, _JSON_SCAN_BUDGET_MAX_CHARS)
    index = text.find("{")
    while index != -1:
        if attempts >= _JSON_SCAN_MAX_ATTEMPTS or scanned > budget:
            # 上限必须留痕：否则运维无法区分「模型没给 JSON」与「解析器主动放弃」。
            logger.warning(
                "[%s] json scan aborted reason=%s scanned_chars=%d discarded_chars=%d attempts=%d",
                PLUGIN_ID,
                "attempts" if attempts >= _JSON_SCAN_MAX_ATTEMPTS else "chars",
                scanned,
                len(text) - index,
                attempts,
            )
            return
        attempts += 1
        try:
            value, end = _JSON_DECODER.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            # 失败点到起点的距离即本次扫描成本（pos 是含 idx 偏移的绝对下标）。
            scanned += exc.pos - index + 1
            index = text.find("{", index + 1)
            continue
        except RecursionError:
            # C 扫描器对深嵌套抛 RecursionError（RuntimeError 子类，不是
            # JSONDecodeError）；漏捕会让异常穿出 parse_decision_json。递归上限处
            # 必然中止，单次成本与文本长度无关，按 1 字符计。
            scanned += 1
            index = text.find("{", index + 1)
            continue
        yield value
        scanned += end - index + 1
        index = text.find("{", end)


def _json_candidates(text: Any) -> Iterator[Any]:
    raw = str(text or "").strip()
    return _iter_json_values(_TRUNCATED_JSON_FENCE_PATTERN.sub("", raw).strip())


def parse_decision_json(text: str) -> dict[str, Any] | None:
    """严格解析判断模型的 JSON 响应，带类型校验和规范化。

    Args:
        text: 判断模型返回的原始文本

    Returns:
        规范化后的字典 {"should_reply": bool, "reason": str}，解析失败返回 None
    """
    # 跳过不相关 JSON（如模型先吐 {"status": "ok"}）。出现两个及以上含 should_reply
    # 的对象说明模型自相矛盾（草稿 + 结论、或复读了提示词里的示例），此时不接话：
    # 这是判断环节，宁可少说一句，也不能按其中任意一段去开口。
    decisions = [
        value
        for value in _json_candidates(text)
        if isinstance(value, dict) and "should_reply" in value
    ]
    if len(decisions) > 1:
        return None
    parsed = decisions[0] if decisions else None
    if parsed is None:
        return None

    # 规范化 should_reply 为布尔值（字面量集合单源见 MODEL_JSON_TRUE_WORDS）。
    raw_reply = parsed["should_reply"]
    if isinstance(raw_reply, bool):
        should_reply = raw_reply
    elif isinstance(raw_reply, str):
        should_reply = raw_reply.strip().lower() in MODEL_JSON_TRUE_WORDS
    elif isinstance(raw_reply, (int, float)) and math.isfinite(raw_reply):
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
    return str(getattr(event, "unified_msg_origin", "") or "").strip()


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
    走裸号分支照样能查到它。
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
    同号的群会共用一条状态记录，配额与冷却互相污染。
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
    """读取事件 extra，显式 None 同缺省值处理。"""
    value = event.get_extra(key, default)
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
    return bool(getattr(event, "is_at_or_wake_command", False))


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


def dedupe_message_records(records: list[MessageRecord]) -> list[MessageRecord]:
    """本地缓存只合并同角色、昵称、正文和时间的消息，保留首次出现的位置。"""
    deduped: list[MessageRecord] = []
    seen: set[tuple[str, str, str, float]] = set()
    for item in records:
        text = collapse_whitespace(item.text)
        if not text:
            continue
        key = (item.role, history_display_name(item.role, item.name), text, item.at)
        if key in seen:
            continue
        seen.add(key)
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
        # 一条消息恒为一行（换行折成单空格）：理由见 _LINEBREAK_RUN_PATTERN。
        lines.append(f"{name}: {_LINEBREAK_RUN_PATTERN.sub(' ', str(item.text))}")
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

    text = text.translate(_REPLY_CONTROL_REMOVAL)

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
