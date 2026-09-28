"""数据形状、配置规约与无依赖纯函数。

拥有：会话状态与投递结果的数据类、``Settings`` / ``ConfigSpec`` 的配置读写
规约、上限常量、时间与类型转换纯函数、跨模块回调的 ``Protocol`` 形状。

不拥有任何 I/O 与业务判断：落盘属 ``storage``，宿主字段读取属 ``utils``，
是否接话属 ``decision``。本模块是依赖图的叶子（只依赖标准库与宿主 logger），
反向依赖会立刻成环。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from astrbot.api import logger

PLUGIN_ID = "astrbot_plugin_self_initiated_reply"
PLUGIN_VERSION = "1.5.0"
COMMAND_HANDLED_KEY = f"{PLUGIN_ID}:command_handled"
STATE_VERSION = 4

# 配置安全限制：防 OOM、费用滥用与性能降级的硬边界。
MAX_PROMPT_LENGTH = 8000
MAX_WHITELIST_SIZE = 1000
MAX_STRING_LIST_ITEM_LEN = 200
# str 类键（provider id）的硬上限：与列表条目同宽，防止无限长字符串落盘。
MAX_PROVIDER_ID_LEN = MAX_STRING_LIST_ITEM_LEN
# 与前端 pages/主动回复设置/config-form.mjs 的 WHITELIST_ILLEGAL_RE 同字符集。
# 控制字符 + 引号 + 反斜杠：过长文案截进 logger.warning 时不能伪造日志行。
STRING_LIST_ILLEGAL_RE = re.compile(r"[\x00-\x1f\"'\\]")
MAX_BOT_ALIASES = 64
MAX_IGNORED_SENDER_IDS = 1000
MAX_QUIET_HOURS = 24
# ``recent_message_limit`` 规格与 ``SessionState.recent`` 的兜底 maxlen 共用，
# 写两份数字会让新会话窗口与配置面板显示不一致。
RECENT_MESSAGE_LIMIT_DEFAULT = 20
MAX_RECENT_MESSAGE_LIMIT = 100
# 生成路径上下文（历史文本）总字符预算：宿主单条消息长度不受本插件约束，
# 100 条缓存上限挡不住成本失控。6000 ≈ 默认 20 条 × 常见消息长度，只裁病态长史。
MAX_GENERATION_CONTEXT_CHARS = 6000
# 保尾裁剪时插在最前的省略提示。判断与生成两条路径必须展示同一句话。
CONTEXT_CAP_MARKER = "…(更早历史因长度预算省略)"
MAX_DAILY_REPLIES_LIMIT = 1000
MAX_VISION_IMAGES = 5
MIN_VISION_IMAGE_AGE_SEC = 60  # 短于此的清理窗口会让图片上下文抖动
MAX_VISION_IMAGE_AGE_SEC = 86400
MAX_VISION_TIMEOUT_SEC = 120
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 远程下载与本地读取共用
MAX_CACHED_IMAGE_EVENTS = 20
MAX_IMAGE_CACHE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_DESCRIPTION_CACHE_BYTES = 512 * 1024
MAX_IMAGE_MEMORY_BYTES = 64 * 1024 * 1024
MAX_SESSION_IMAGE_MEMORY_BYTES = 16 * 1024 * 1024

# 两条措辞相近但语义不同，不可互换：前者放弃整个任务，后者只放弃这条回复。
STALE_TASK_MESSAGE = "会话已经更新，放弃旧任务。"
STALE_REPLY_MESSAGE = "会话已更新，放弃旧回复。"
# 投递早退与发送后回读成因相同时必须同一口径，否则误导排查方向。
STOPPING_REPLY_TEXT = "插件正在停止，放弃回复。"

# 泄漏告警阈值：任务表每会话至多 1-2 个常驻条目，代次表 ≤ 白名单上限（1000）。
LEAK_WARN_TASK_THRESHOLD = 100
LEAK_WARN_SESSION_THRESHOLD = 1500
# 生成上下文文本记录预算下限：本地文本记录不足此数时才回宿主补历史。
# 判断路径另有 decision.DECISION_HISTORY_FLOOR = 8，那是提示词契约（"优先参考
# 最近至少 8 条"），两者刻意不同源，不得顺手统一。
MIN_RECENT_TEXT_RECORDS = 5

# 插件运行常量
MAX_AGENT_STEPS = 15  # 为主 Agent 生成预留步数
MAX_DIRECT_TOOL_SENDS = 2
# 生成超时后留给 run_agent 优雅退出的宽限秒数；terminate 的 stop_timeout
# fallback 必须引用 TERMINATE_TASK_TIMEOUT_SEC，禁止再写字面量。
GRACEFUL_STOP_GRACE_SEC = 3.0
TERMINATE_TASK_TIMEOUT_SEC = 3.0
# 内联指令入口做管理员校验的动作集合（help 任何成员可取）。
ADMIN_COMMAND_ACTIONS = {"status", "list", "add", "remove", "check", "on", "off", "debug"}
# 写操作在权限校验通过后取消在途回复；只读动作不打断进行中的检查。
SESSION_CANCEL_COMMAND_ACTIONS = frozenset({"add", "remove", "check", "on", "off"})
# 0.7.x 主动 Agent 工具允许列表：默认空集，未列入的工具在 build/hook 后一律移除，
# 无法验证时终止本次主动运行（fail closed）。
PROACTIVE_ALLOWED_TOOL_IDS: frozenset[str] = frozenset()
# 宿主级危险能力工具 ID（实证于 AstrBot 4.26/4.27 源码）：cron、电脑使用、
# 文件提取、知识库 agentic。无论 proactive_inherit_tools 如何，这些工具在主动
# 运行中一律拒绝；是 build config 硬关闭之外拦截 hook 注入的最终防线。
# 条目必须是宿主 FunctionTool 的精确 name，运行期不做名字匹配；完整性由
# tests/test_security.py 的精确集合断言与 scripts/compat_check.py 枚举真实宿主
# 模块共同钉住。
HOST_DANGEROUS_TOOL_IDS: frozenset[str] = frozenset(
    {
        # 4.23.3 实测为单工具 multiCommand，FunctionTool.name 为 future_task
        "future_task",
        "astrbot_execute_shell",
        "astrbot_execute_ipython",
        "astrbot_execute_python",
        # 4.27.1 新增
        "astrbot_shell_session",
        "astrbot_execute_browser",
        "astrbot_execute_browser_batch",
        "astrbot_run_browser_skill",
        # fs.py 4.23.3 实测的 FunctionTool name
        "astrbot_upload_file",
        "astrbot_download_file",
        "astrbot_file_read_tool",
        "astrbot_file_write_tool",
        "astrbot_file_edit_tool",
        "astrbot_grep_tool",
        "astr_kb_search",
    }
)


EVENT_CLEANUP_INTERVAL_SEC = 3600
MAX_CACHED_EVENTS = 100
PATROL_BACKOFF_DELAY_SEC = 60
# release 闸门等待兜底：配置回滚会把运行标记恢复成快照态，而支撑它的检查任务
# 可能已结束，release 事件永不再 set。超时 + 轮次上限把失同步降级为一次延迟
# 或一次丢弃，而不是让会话静默死亡或空转独占事件循环。
RELEASE_WAIT_TIMEOUT_SEC = 30
MAX_RELEASE_WAIT_ROUNDS = 20
# 管理员列表重探窗口：高频事件路径在窗口内跳过对 cmd_config.json 的 stat；
# 运行期改管理员下个窗口生效，探测失败不变更缓存。
ADMIN_REFRESH_WINDOW_SEC = 30.0
# 外部时间戳的容许时钟偏移：状态文件是可手工编辑的外部输入。远未来值会把
# 会话永久锁死（remaining_silence 变数十年、巡检每轮空跑）；负值单独毒
# last_active_at 会被观察窗口拦住，但一并毒 last_proactive_observed_at 即可放行。
# 取 300s 覆盖 NTP 校正与容器间漂移；上界用 now + 偏移，避免随时间失效。
MAX_CLOCK_SKEW_SEC = 300.0

DEFAULT_DECISION_PROMPT_TEMPLATE = """会话: {session}
触发: {trigger}
Bot昵称: {bot_aliases}
距离最后一条可观察消息: {last_message_age_sec}s
距离上次主动回复: {last_reply_age_sec}s
最后一条消息: {latest_message}

最近消息（优先参考最近至少 8 条当前会话历史；如果历史不足则按已有内容判断，不要只看最后一条）:
{recent_messages}

任务:
判断 Bot 现在是否适合温和地接一句。目标是自然融入群聊，不是抢答 @Bot、命令或私人对话。

判断规则:
- 默认保守克制。只在接话自然、有信息量时 should_reply=true。
- 可以回复的情况（满足其一即可，但必须不打扰当前对话节奏）:
  1. 群友明确点名 Bot 昵称，并要求接话、回复、发表情包、找图、发图。
  2. 群友在讨论技术/知识/工具类问题，Bot 能补充一个简短有用的信息、提醒或判断。
  3. 群聊明显冷场，最后一条是开放式提问、评价或吐槽，轻短接一句能自然续上话题。

- 以下情况必须 should_reply=false：
  - 群友间正在密集互动、互问互答、热烈讨论中，插话会显得突兀。
  - 对话明显是针对某个具体人的提问，或群友间的私人话题。
  - 最近消息只是简单附和、表情包刷屏、哈哈哈/嗯/好/草/确实/6 等无实质内容的闲聊。
  - Bot 最近刚回复过，且没有人接着 Bot 的话继续聊。
  - 纯主观/个人话题（八卦、情感、个人生活细节），Bot 没有立场也没有价值。
  - 最后一条消息是自洽的陈述或结论，没有留下接话的自然入口。
- 特别注意：
  - “不好说吧”“怎么说呢”“我说真的”“别说了”等普通句子不算要求 Bot 接话。
  - 只有明确出现 Bot 昵称并要求 Bot 说话/回复/发图/发表情包，才算“点名 Bot 接话”。
  - 如果只是可以接但价值很低，倾向 false
- 只适合自然的接一句，像群友随口搭话，不要强行刷存在感。

输出要求:
只输出严格 JSON，不要解释：
{"should_reply": true/false, "reason": "一句简短理由"}"""

DECISION_JSON_CONTRACT = """输出 JSON:
{"should_reply": true/false, "reason": "一句简短理由"}"""


def now_ts() -> float:
    return time.time()


def today_key() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


_MINUTE_SECONDS = 60
_HOUR_SECONDS = 3600
# 控制字符（0x00-0x1F）一律从提示词变量里剔除。
_PRINTABLE_CHAR_MIN = 32

# 空白归一正则住 models：utils 依赖 models（叶子），反向会成环。
# 两处各内联编译一份会漂移成「同一段文本在不同路径形状不同」。
WHITESPACE_PATTERN = re.compile(r"\s+")
# 行内空白：不含换行，用于保留多行结构时压缩空格
INLINE_SPACE_PATTERN = re.compile(r"[^\S\n]+")


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < _MINUTE_SECONDS:
        return f"{seconds}s"
    if seconds < _HOUR_SECONDS:
        return f"{seconds // _MINUTE_SECONDS}m{seconds % _MINUTE_SECONDS}s"
    # 三档口径一致：小时档丢秒会让 72h0m50s 显示成 72h0m。
    return (
        f"{seconds // _HOUR_SECONDS}h"
        f"{seconds % _HOUR_SECONDS // _MINUTE_SECONDS}m"
        f"{seconds % _MINUTE_SECONDS}s"
    )


def as_bool(value: Any, default: bool = False) -> bool:
    """Parse a persisted/host config flag. Broader than ``parse_decision_json``.

    Config values come from Dashboard/JSON files and historically used on/启用/开启.
    Decision-model JSON only accepts true/yes/1/是; do not reuse this set there.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enable",
        "enabled",
        "启用",
        "开启",
        "是",
    }


def as_int(value: Any, default: int, minimum: int = 0, maximum: int = 100000) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


def as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(parsed):
        return default
    return max(minimum, min(maximum, parsed))


def as_timestamp(value: Any, *, now: float | None = None) -> float:
    """把外部来源的 epoch 秒钳到 ``[0, now + MAX_CLOCK_SKEW_SEC]``。

    上界随时钟走（写死绝对值会失效）；NaN/inf/不可解析一律归 0.0（等价
    「从未活跃」）。``now`` 可注入以便测试。
    """
    ceiling = (now_ts() if now is None else now) + MAX_CLOCK_SKEW_SEC
    return as_float(value, 0.0, minimum=0.0, maximum=ceiling)


def choice(value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def first_bindable_args(
    func: Any, candidates: list[tuple[tuple[Any, ...], dict[str, Any]]]
) -> tuple[tuple[Any, ...], dict[str, Any]] | None:
    """返回首个可绑定到 ``func`` 签名的候选实参；都不匹配返回 None。

    只做 ``bind`` 预检、绝不调用：函数体内的 TypeError 必须由调用方处理，
    在此重试意味同一宿主调用可能执行两次（对落盘/LLM 即重复副作用）。
    签名不可检查时返回首个候选（与各调用点原有回退一致）。
    """
    if not candidates:
        return None
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return candidates[0]
    for args, kwargs in candidates:
        try:
            signature.bind(*args, **kwargs)
        except TypeError:
            continue
        return (args, kwargs)
    return None


def restore_container_inplace(target: Any, source: Any) -> None:
    """原地恢复容器内容：等待者与运行中的 ``async with`` 持有容器本身的引用，
    重绑定会制造孤儿表。"""
    target.clear()
    target.update(source)


def sanitize_prompt_variable(
    text: str,
    max_length: int | None = 500,
    *,
    allow_newlines: bool = False,
) -> str:
    """清理用于提示词的变量，防注入与长度攻击。

    提示词是纯文本，不做反斜杠转义；双引号改成中文引号，避免用户内容
    伪造出与输出契约一致的 JSON 片段。``max_length=None`` 表示不截断
    （调用方自带保尾预算时用，截头会先丢最新消息）。``allow_newlines``
    供多行聊天记录保留行结构，单字段变量保持单行。
    """
    text = str(text or "").strip()
    if not text:
        return ""

    if max_length is not None and len(text) > max_length:
        text = text[:max_length] + "..."

    text = text.replace('"', "“")

    if allow_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for line in text.split("\n"):
            line = "".join(char for char in line if ord(char) >= _PRINTABLE_CHAR_MIN)
            line = INLINE_SPACE_PATTERN.sub(" ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = "".join(char for char in text if ord(char) >= _PRINTABLE_CHAR_MIN)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


@dataclass
class MessageRecord:
    role: str
    name: str
    text: str
    sender_id: str = ""
    at: float = field(default_factory=now_ts)


# 取不到发送者名字时的兜底显示名：提示词历史行与事件侧必须同一称呼。
FALLBACK_SENDER_NAME = "用户"


def history_display_name(role: str, name: str | None = None) -> str:
    """历史展示名：助手固定 Bot，其他人用名字，缺名才回落用户。"""
    if role == "assistant":
        return "Bot"
    return str(name or FALLBACK_SENDER_NAME)


class ReadHistoryCallback(Protocol):
    """宿主历史读取回调（limit 关键字调用）。"""

    def __call__(self, umo: str, *, limit: int) -> Awaitable[list[MessageRecord]]: ...


class ImageContextCallback(Protocol):
    """Vision 描述上下文回调（enabled/provider_id 关键字调用）。"""

    def __call__(self, umo: str, *, enabled: bool, provider_id: str) -> Awaitable[str]: ...


class LocalGateCallback(Protocol):
    """局部闸门回调（state 位置 + force 关键字调用，返回跳过原因或空串）。"""

    def __call__(
        self,
        state: SessionState,
        *,
        force: bool,
        silence_active_at: float | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class PipelineReply:
    """Result of one main-Agent run with its outbound evidence ledger."""

    text: str = ""
    ledger: AttemptLedger | None = None

    @property
    def direct_send_count(self) -> int:
        return self.ledger.direct_send_count if self.ledger is not None else 0

    @property
    def direct_texts(self) -> tuple[str, ...]:
        return self.ledger.direct_texts if self.ledger is not None else ()


class CheckTrigger(StrEnum):
    """会话检查触发名。拼错在加载期变成 AttributeError，不再静默漏判。"""

    MESSAGE_DELAY = "message_delay"
    PATROL = "patrol"
    MANUAL = "manual"


class SendStatus(StrEnum):
    """Outcome of one outbound attempt.

    UNKNOWN means the platform call may have reached the adapter, so callers
    must not blindly retry through a second channel.
    """

    DELIVERED = "delivered"
    FAILED_BEFORE_SUBMIT = "failed_before_submit"
    UNKNOWN = "unknown"
    SUPPRESSED = "suppressed"


class SuppressCode(StrEnum):
    """SUPPRESSED 的机器可判成因。

    ``detail`` 是给人看的自由文本，不能拿它做分支：改文案不该改变控制流。
    调用方要区分的成因放这里，``detail`` 只进日志。四个成员各有真实构造点，
    新增分支按语义取用现成成员。
    """

    STOPPING = "stopping"
    GENERATION_CHANGED = "generation_changed"
    BUDGET = "budget"
    GATE_REJECTED = "gate_rejected"


class PluginLifecycle(StrEnum):
    """Plugin-level lifecycle owned by ``SelfInitiatedReplyPlugin``."""

    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    DEGRADED = "DEGRADED"


class AttemptState(StrEnum):
    """Lifecycle evidence for one outbound adapter attempt."""

    RESERVED = "reserved"
    IN_FLIGHT = "in_flight"
    DELIVERED = "delivered"
    UNKNOWN = "unknown"
    FAILED_BEFORE_SUBMIT = "failed_before_submit"
    SUPPRESSED = "suppressed"
    ABANDONED = "abandoned"


@dataclass(eq=False)
class SendAttempt:
    """One outbound call tracked by a pipeline-owned ledger.

    ``eq=False``：账本按身份判定成员。``attempt_id`` 每账本从 1 起，值相等
    会让另一账本里同号同文的 attempt 冒充本账本成员。
    """

    attempt_id: int
    kind: str
    text: str = ""
    state: AttemptState = AttemptState.RESERVED


# SendStatus → AttemptState 的两组固定映射，语义不同（in-flight 出口 vs
# 预提交出口）不可合并；模块级常量避免每次调用重建 dict。
_IN_FLIGHT_OUTCOMES: dict[SendStatus, AttemptState] = {
    SendStatus.DELIVERED: AttemptState.DELIVERED,
    SendStatus.UNKNOWN: AttemptState.UNKNOWN,
    SendStatus.FAILED_BEFORE_SUBMIT: AttemptState.FAILED_BEFORE_SUBMIT,
}
_PRE_SUBMIT_OUTCOMES: dict[SendStatus, AttemptState] = {
    SendStatus.FAILED_BEFORE_SUBMIT: AttemptState.FAILED_BEFORE_SUBMIT,
    SendStatus.SUPPRESSED: AttemptState.SUPPRESSED,
}


class LedgerPhase(StrEnum):
    """``AttemptLedger`` 的阶段：五个迁移点各自守一件事。

    ``OPEN`` 允许登记与结算；``SEALED`` 表示证据已冻结（在途一律悲观记
    ``UNKNOWN``）；``RECORDING`` → ``RECORDED`` / ``RECORD_FAILED`` 是唯一
    记账任务的三种收尾。成员与字符串相等，既有字符串比较语义不变。
    """

    OPEN = "open"
    SEALED = "sealed"
    RECORDING = "recording"
    RECORDED = "recorded"
    RECORD_FAILED = "record_failed"


@dataclass
class AttemptLedger:
    """Single source of outbound submission evidence for one pipeline run.

    All mutators are synchronous and contain no await points, so separate
    asyncio tasks cannot interleave a state transition on the event loop.
    """

    ledger_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _attempts: list[SendAttempt] = field(default_factory=list)
    _next_attempt_id: int = 1
    _record_task: object | None = field(default=None, init=False, repr=False)
    phase: LedgerPhase = LedgerPhase.OPEN
    record_failure: str = ""

    @property
    def attempts(self) -> tuple[SendAttempt, ...]:
        return tuple(self._attempts)

    @property
    def record_task(self) -> object | None:
        return self._record_task

    @property
    def has_submission(self) -> bool:
        return any(
            attempt.state in {AttemptState.DELIVERED, AttemptState.UNKNOWN}
            for attempt in self._attempts
        )

    @property
    def has_unknown(self) -> bool:
        return any(attempt.state is AttemptState.UNKNOWN for attempt in self._attempts)

    @property
    def direct_send_count(self) -> int:
        return sum(
            1
            for attempt in self._attempts
            if attempt.kind == "tool_direct"
            and attempt.state in {AttemptState.DELIVERED, AttemptState.UNKNOWN}
        )

    @property
    def direct_texts(self) -> tuple[str, ...]:
        return tuple(
            attempt.text
            for attempt in self._attempts
            if attempt.kind == "tool_direct"
            and attempt.text
            and attempt.state in {AttemptState.DELIVERED, AttemptState.UNKNOWN}
        )

    @property
    def accepts_attempts(self) -> bool:
        """账本是否还能受理新尝试（调用方在 reserve 之前判闸门）。

        账本已封时收到迟到的工具直发是可预期时序（被隔离的运行保留
        tracker），调用方据此降级为闸门拒绝而非让异常逃进宿主。
        """
        return self.phase is LedgerPhase.OPEN

    def reserve(self, kind: str, text: str = "") -> SendAttempt:
        if self.phase != LedgerPhase.OPEN:
            raise RuntimeError("cannot reserve an attempt after the ledger is sealed")
        attempt = SendAttempt(self._next_attempt_id, kind, text)
        self._next_attempt_id += 1
        self._attempts.append(attempt)
        return attempt

    def mark_in_flight(self, attempt: SendAttempt) -> None:
        if self.phase != LedgerPhase.OPEN or attempt not in self._attempts:
            raise RuntimeError("cannot start an attempt outside an open ledger")
        if attempt.state is not AttemptState.RESERVED:
            raise RuntimeError("only a reserved attempt can enter the adapter")
        attempt.state = AttemptState.IN_FLIGHT

    def resolve(self, attempt: SendAttempt, status: SendStatus) -> bool:
        """Apply an adapter outcome, returning false for a sealed late result."""
        if self.phase != LedgerPhase.OPEN or attempt not in self._attempts:
            return False
        if attempt.state is not AttemptState.IN_FLIGHT:
            raise RuntimeError("only an in-flight attempt can receive an adapter outcome")
        try:
            attempt.state = _IN_FLIGHT_OUTCOMES[status]
        except KeyError as exc:
            raise ValueError(f"invalid in-flight outcome: {status}") from exc
        return True

    def finish_before_submit(self, attempt: SendAttempt, status: SendStatus) -> None:
        if self.phase != LedgerPhase.OPEN or attempt not in self._attempts:
            raise RuntimeError("cannot finish an attempt outside an open ledger")
        if attempt.state is not AttemptState.RESERVED:
            raise RuntimeError("only a reserved attempt can finish before adapter entry")
        try:
            attempt.state = _PRE_SUBMIT_OUTCOMES[status]
        except KeyError as exc:
            raise ValueError(f"invalid pre-submit outcome: {status}") from exc

    def start_recording(self, task: object) -> bool:
        """Register the ledger's sole persistence task after sealing evidence."""
        if self.phase != LedgerPhase.SEALED or self._record_task is not None:
            return False
        self._record_task = task
        self.phase = LedgerPhase.RECORDING
        return True

    def mark_recorded(self) -> None:
        if self.phase != LedgerPhase.RECORDING:
            raise RuntimeError("only a recording ledger can become recorded")
        self.phase = LedgerPhase.RECORDED

    def mark_record_failed(self, detail: str) -> None:
        if self.phase != LedgerPhase.RECORDING:
            raise RuntimeError("only a recording ledger can fail persistence")
        self.record_failure = detail
        self.phase = LedgerPhase.RECORD_FAILED

    def seal(self) -> tuple[SendAttempt, ...]:
        """Freeze evidence; any in-flight adapter call is pessimistically UNKNOWN."""
        if self.phase != LedgerPhase.OPEN:
            return self.attempts
        for attempt in self._attempts:
            if attempt.state is AttemptState.RESERVED:
                attempt.state = AttemptState.ABANDONED
            elif attempt.state is AttemptState.IN_FLIGHT:
                attempt.state = AttemptState.UNKNOWN
        self.phase = LedgerPhase.SEALED
        return self.attempts


@dataclass(frozen=True)
class SessionContainers:
    """main 侧共享容器的收拢视图（按名字交给需要多个容器的协作者）。

    存在的理由是把「这些集合必须始终保持同一身份」这条承重契约变成一个
    有名字的实体。``frozen=True`` 只阻止字段重绑；容器内容仍原地修改。
    **不**把 ``SessionGate`` 的三张表收进来：release 表刻意不参与快照恢复，
    混进同一对象会诱导"整对象恢复"的错误写法。
    """

    last_events: dict[str, Any]
    last_event_at: dict[str, float]
    recent_image_events: dict[str, Any]
    whitelist_runtime_umos: dict[str, set[str]]
    delay_tasks: dict[str, Any]
    running_check_tasks: dict[str, Any]
    background_tasks: set[Any]
    sessions: dict[str, Any]


@dataclass(frozen=True)
class SendOutcome:
    status: SendStatus
    detail: str = ""
    code: SuppressCode | None = None

    @property
    def delivered(self) -> bool:
        return self.status is SendStatus.DELIVERED


@dataclass
class SessionState:
    recent: deque[MessageRecord] = field(
        default_factory=lambda: deque(maxlen=RECENT_MESSAGE_LIMIT_DEFAULT)
    )
    last_active_at: float = 0.0
    last_proactive_at: float = 0.0
    last_proactive_observed_at: float = 0.0
    last_proactive_text: str = ""
    daily_key: str = field(default_factory=today_key)
    daily_count: int = 0

    def refresh_day(self) -> None:
        key = today_key()
        if self.daily_key != key:
            self.daily_key = key
            self.daily_count = 0

    def record_proactive_attempt(self, *, confirmed: bool, text: str, at: float) -> None:
        """记录一次主动回复尝试的状态字段更新（单点写入）。

        ``confirmed=False`` 表示 UNKNOWN 投递：只消耗冷却与日配额，不写历史
        条目。先 ``refresh_day`` 再自增：调用方的跨天刷新发生在判断+生成之前，
        二者相隔可达数十秒，跨零点时增量会记到昨日键上。
        """
        self.refresh_day()
        self.last_proactive_at = at
        self.daily_count += 1
        if not confirmed:
            return
        self.last_proactive_text = text
        self.recent.append(
            MessageRecord(
                role="assistant",
                name=history_display_name("assistant"),
                text=text,
                at=at,
            )
        )

    def remaining_silence_sec(
        self,
        min_silence_sec: float,
        now: float,
        *,
        active_at: float | None = None,
    ) -> float:
        """距指定活动时间的剩余静默（默认上次活跃；从未活跃按 0 计）。"""
        stamp = self.last_active_at if active_at is None else active_at
        if not stamp:
            return 0.0
        return max(0.0, min_silence_sec - (now - stamp))

    def age_sec(self, now: float) -> float:
        """距上次活跃的经过秒数（从未活跃按 0 计）。"""
        return now - self.last_active_at if self.last_active_at else 0.0


@dataclass(frozen=True)
class ConfigSpec:
    """单个配置键的完整规格。

    同一个键散在多处声明时，漏一处就静默失效（面板上能改、保存返回成功、
    值不生效）。本表驱动 ``Settings`` 字段表、``from_config``、
    ``to_config_dict``、``_parse_config_updates`` 与 ``_AUDITED_CONFIG_KEYS``；
    ``_conf_schema.json`` 保留独立文件（承载 UI 文案），一致性由
    ``tests/test_config_schema.py`` 断言。表只收机器可校验的语义，UI 拷贝不进表。

    字段语义：
        key: 配置键名（= schema 键名）。
        kind: ``bool`` / ``int`` / ``float`` / ``str`` / ``enum`` / ``text`` / ``list``。
        default: 缺键时的默认值，须与 schema 的 ``default`` 一致。
        minimum/maximum: 数值夹取边界，须与 schema ``slider`` 的 min/max 一致。
        step: 仅 UI 用（slider 步长），Python 侧不参与校验。
        field_name: ``Settings`` 上的属性名，与 key 同名时留空。
        options: 枚举取值，须与 schema ``options`` 一致。
        audited: 是否记 INFO 审计日志（安全敏感键）。
        container: ``list`` 键在 ``Settings`` 上的容器类型（``set`` 需去重/排序）。
        legacy_keys: 旧版本键名，只在读侧回退；``to_config_dict`` 只写正式键。
        special/editor_mode/editor_language: schema 的 UI 专属字段。
        max_len/max_items: 硬上限，超限截断并记 warning。
        item_max_len/item_pattern: list/set 条目的统一规范化规则。
        reset_default: 空提交复位的内置默认（目前唯一消费者是 text 类键）。
        surfaces: 该键出现在哪些配置面。``host`` 为宿主 schema；
            ``panel`` 为自定义设置页。GET /config 与前端可写键都从此派生。
    """

    key: str
    kind: str
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    field_name: str = ""
    options: tuple[str, ...] = ()
    audited: bool = False
    container: str = ""
    legacy_keys: tuple[str, ...] = ()
    special: str = ""
    editor_mode: bool = False
    editor_language: str = ""
    max_len: int | None = None
    max_items: int | None = None
    item_max_len: int | None = None
    item_pattern: str = ""
    reset_default: Any = ""
    surfaces: frozenset[str] = frozenset({"host"})

    @property
    def reset_value(self) -> str:
        """复位后的实际取值：GET /config 的默认填充与读侧落盘必须取同一表达式，
        否则「恢复默认 → 保存」会被误报成改过字段。"""
        return str(self.reset_default).strip()

    @property
    def attr(self) -> str:
        """``Settings`` 上的属性名。"""
        return self.field_name or self.key

    @property
    def schema_type(self) -> str:
        """``_conf_schema.json`` 里的 ``type`` 值（enum/str 都渲染成 string）。"""
        if self.kind in {"enum", "str"}:
            return "string"
        return self.kind

    def canonical_value(self, value: Any) -> Any:
        """set 容器按排序输出：JSON 无集合类型，无序写盘会产生伪 diff。"""
        return sorted(value) if self.container == "set" else value


_PANEL = frozenset({"host", "panel"})

# 配置键规格表。顺序 = _conf_schema.json 顺序 = 面板呈现顺序。
CONFIG_SPECS: tuple[ConfigSpec, ...] = (
    ConfigSpec("enabled", "bool", True, audited=True, surfaces=_PANEL),
    ConfigSpec("decision_model_enabled", "bool", True, surfaces=_PANEL),
    ConfigSpec(
        "judge_provider_id",
        "str",
        "",
        special="select_provider",
        audited=True,
        max_len=MAX_PROVIDER_ID_LEN,
        surfaces=_PANEL,
    ),
    ConfigSpec(
        "decision_prompt_template",
        "text",
        "",
        editor_mode=True,
        editor_language="text",
        max_len=MAX_PROMPT_LENGTH,
        reset_default=DEFAULT_DECISION_PROMPT_TEMPLATE,
        surfaces=_PANEL,
    ),
    ConfigSpec("decision_temperature", "float", 0.2, 0.0, 2.0, step=0.1, surfaces=_PANEL),
    ConfigSpec("decision_timeout_sec", "float", 20.0, 1, 300, step=1, surfaces=_PANEL),
    ConfigSpec(
        "decision_history_min_messages",
        "int",
        # 消费值即 MIN_RECENT_TEXT_RECORDS，再写一遍数字等于允许两者被单独改掉。
        MIN_RECENT_TEXT_RECORDS,
        0,
        30,
        step=1,
        legacy_keys=("min_context_messages", "proactive_threshold"),
        surfaces=_PANEL,
    ),
    ConfigSpec(
        "reply_length_mode",
        "enum",
        "balanced",
        options=("short", "balanced", "expressive"),
    ),
    ConfigSpec("allow_multiline_reply", "bool", True),
    ConfigSpec("max_reply_chars", "int", 220, 0, 2000, step=10),
    ConfigSpec(
        "quote_mode",
        "enum",
        "off",
        options=("off", "model", "random"),
        surfaces=_PANEL,
    ),
    ConfigSpec("quote_probability", "int", 50, 0, 100, step=5, surfaces=_PANEL),
    ConfigSpec(
        "mention_mode",
        "enum",
        "off",
        options=("off", "always", "random"),
        surfaces=_PANEL,
    ),
    ConfigSpec("mention_probability", "int", 50, 0, 100, step=5, surfaces=_PANEL),
    ConfigSpec("log_reply_content", "bool", False),
    ConfigSpec(
        "bot_aliases",
        "list",
        [],
        container="list",
        max_items=MAX_BOT_ALIASES,
        item_max_len=MAX_STRING_LIST_ITEM_LEN,
        item_pattern=STRING_LIST_ILLEGAL_RE.pattern,
    ),
    ConfigSpec(
        "ignored_sender_ids",
        "list",
        [],
        container="set",
        audited=True,
        max_items=MAX_IGNORED_SENDER_IDS,
        item_max_len=MAX_STRING_LIST_ITEM_LEN,
        item_pattern=STRING_LIST_ILLEGAL_RE.pattern,
    ),
    ConfigSpec(
        "whitelist_sessions",
        "list",
        [],
        field_name="whitelist",
        container="set",
        audited=True,
        legacy_keys=("whitelist",),
        max_items=MAX_WHITELIST_SIZE,
        item_max_len=MAX_STRING_LIST_ITEM_LEN,
        item_pattern=STRING_LIST_ILLEGAL_RE.pattern,
        surfaces=_PANEL,
    ),
    ConfigSpec("enabled_private_sessions", "bool", True, surfaces=_PANEL),
    ConfigSpec(
        "abandon_stale_on_new_message",
        "bool",
        False,
        surfaces=_PANEL,
    ),
    ConfigSpec("skip_after_direct_call", "bool", True, surfaces=_PANEL),
    ConfigSpec("check_interval_sec", "int", 300, 30, 86400, step=30),
    ConfigSpec("patrol_inactive_after_sec", "int", 1800, 0, 604800, step=3600),
    ConfigSpec(
        "message_delay_sec",
        "int",
        60,
        5,
        86400,
        step=5,
        legacy_keys=("idle_trigger_seconds",),
        surfaces=_PANEL,
    ),
    ConfigSpec("min_silence_sec", "int", 45, 0, 86400, step=10, surfaces=_PANEL),
    ConfigSpec(
        "cooldown_sec",
        "int",
        900,
        0,
        86400,
        step=60,
        legacy_keys=("cooldown_seconds",),
        surfaces=_PANEL,
    ),
    ConfigSpec("max_daily_replies_per_session", "int", 5, 0, MAX_DAILY_REPLIES_LIMIT, step=1),
    ConfigSpec(
        "recent_message_limit",
        "int",
        RECENT_MESSAGE_LIMIT_DEFAULT,
        3,
        MAX_RECENT_MESSAGE_LIMIT,
        step=1,
    ),
    ConfigSpec(
        "quiet_hours",
        "list",
        [],
        container="list",
        max_items=MAX_QUIET_HOURS,
        item_max_len=MAX_STRING_LIST_ITEM_LEN,
        item_pattern=STRING_LIST_ILLEGAL_RE.pattern,
    ),
    ConfigSpec("enabled_message_trigger", "bool", True),
    ConfigSpec("enabled_patrol_trigger", "bool", False),
    ConfigSpec("generation_timeout_sec", "float", 60.0, 1, 300, step=1),
    ConfigSpec("proactive_inherit_tools", "bool", False, audited=True, surfaces=_PANEL),
    ConfigSpec(
        "vision_judge_enabled",
        "bool",
        False,
        audited=True,
        legacy_keys=("vision_enabled",),
        surfaces=_PANEL,
    ),
    ConfigSpec(
        "vision_main_enabled",
        "bool",
        False,
        audited=True,
        legacy_keys=("vision_enabled",),
        surfaces=_PANEL,
    ),
    ConfigSpec(
        "vision_provider_id",
        "str",
        "",
        special="select_provider",
        audited=True,
        max_len=MAX_PROVIDER_ID_LEN,
        surfaces=_PANEL,
    ),
    ConfigSpec("vision_skip_stickers", "bool", False, surfaces=_PANEL),
    ConfigSpec(
        "vision_judge_provider_id",
        "str",
        "",
        special="select_provider",
        audited=True,
        max_len=MAX_PROVIDER_ID_LEN,
        surfaces=_PANEL,
    ),
    ConfigSpec("vision_max_images", "int", 2, 1, MAX_VISION_IMAGES, step=1, surfaces=_PANEL),
    ConfigSpec(
        "vision_image_age_sec",
        "int",
        300,
        MIN_VISION_IMAGE_AGE_SEC,
        MAX_VISION_IMAGE_AGE_SEC,
        step=60,
        surfaces=_PANEL,
    ),
    ConfigSpec(
        "vision_timeout_sec",
        "float",
        20.0,
        1,
        MAX_VISION_TIMEOUT_SEC,
        step=1,
        surfaces=_PANEL,
    ),
)

CONFIG_SPEC_BY_KEY: dict[str, ConfigSpec] = {spec.key: spec for spec in CONFIG_SPECS}


def panel_config_specs() -> tuple[ConfigSpec, ...]:
    """Specs exposed on the custom settings page and GET /config."""
    return tuple(spec for spec in CONFIG_SPECS if "panel" in spec.surfaces)


def _list_items(raw: Any) -> list[Any]:
    if isinstance(raw, (list, tuple, set, frozenset)):
        return list(raw)
    if isinstance(raw, str):
        return re.split(r"[\n,，]+", raw)
    raise ValueError("配置列表类型无效")


def _normalize_list_item(spec: ConfigSpec, raw: Any, mode: str) -> tuple[str | None, int, int]:
    """Normalize one list item and return ``(value, dropped, adjusted)``."""
    text = str(raw).strip()
    if not text:
        # 空条目一律丢弃：白名单与别名列表都无意义。
        return None, 1, 0
    if spec.item_pattern and re.search(spec.item_pattern, text):
        if mode == "api":
            raise ValueError(f"{spec.key} 条目含非法字符")
        return None, 1, 0
    if spec.item_max_len is not None and len(text) > spec.item_max_len:
        if mode == "api":
            raise ValueError(f"{spec.key} 条目过长")
        return text[: spec.item_max_len], 0, 1
    return text, 0, 0


def normalize_string_list(
    spec: ConfigSpec, raw: Any, *, mode: str = "disk"
) -> list[str] | set[str]:
    """Normalize one list/set according to its ``ConfigSpec``.

    ``disk`` mode filters and bounds untrusted persisted data while ``api`` mode
    rejects malformed input. Warnings contain counts only, never user-provided
    list content. ``api`` is only for ``webapi._string_list`` (400 on illegal
    input); ``coerce_config_value`` / ``from_config`` always use ``disk`` so a
    bad on-disk list cannot refuse plugin load.
    """
    if mode not in {"disk", "api"}:
        raise ValueError(f"unknown list normalization mode: {mode}")
    if mode == "api" and not isinstance(raw, list):
        raise ValueError(f"{spec.key} 必须是数组")

    items: list[str] = []
    dropped = 0
    adjusted = 0
    for item in _list_items(raw):
        text, item_dropped, item_adjusted = _normalize_list_item(spec, item, mode)
        dropped += item_dropped
        adjusted += item_adjusted
        if text is not None:
            items.append(text)

    if spec.container == "set":
        unique_items = sorted(set(items))
        dropped += len(items) - len(unique_items)
        items = unique_items
    if spec.max_items is not None and len(items) > spec.max_items:
        if mode == "disk":
            dropped += len(items) - spec.max_items
            items = items[: spec.max_items]

    result: list[str] | set[str]
    result = set(items) if spec.container == "set" and mode == "disk" else items
    if mode == "disk" and (dropped or adjusted):
        logger.warning(
            "[%s] %s list normalized: dropped=%d adjusted=%d",
            PLUGIN_ID,
            spec.key,
            dropped,
            adjusted,
        )
    return result


def _truncate_text(spec: ConfigSpec, text: str) -> str:
    """按规格截断文本。无上限或未超限时原样返回。"""
    if spec.max_len is None or len(text) <= spec.max_len:
        return text
    logger.warning(
        "[%s] %s 过长 (%d 字符)，已截断到 %d 字符",
        PLUGIN_ID,
        spec.key,
        len(text),
        spec.max_len,
    )
    return text[: spec.max_len]


def coerce_config_value(spec: ConfigSpec, raw: Any, fallback: Any) -> Any:
    """按规格把一个原始配置值强制成目标类型并夹取边界。

    ``fallback`` 与 ``raw`` 分开传：旧键回退时强制失败要落回同一个旧键的值
    而非静态默认（``vision_enabled`` 迁移到两个新开关的语义）。截断是防
    OOM 与 token 滥用的硬边界，静默生效但必须留 warning。
    """
    if spec.kind == "bool":
        return as_bool(raw, bool(fallback))
    if spec.kind == "int":
        if spec.minimum is None or spec.maximum is None:
            raise RuntimeError(f"{spec.key}: int 规格缺少 minimum/maximum")
        return as_int(raw, int(fallback), int(spec.minimum), int(spec.maximum))
    if spec.kind == "float":
        if spec.minimum is None or spec.maximum is None:
            raise RuntimeError(f"{spec.key}: float 规格缺少 minimum/maximum")
        return as_float(raw, float(fallback), float(spec.minimum), float(spec.maximum))
    if spec.kind == "enum":
        return choice(raw, set(spec.options), str(fallback))
    if spec.kind == "text":
        # 空值回落默认模板（面板留空即复位）：唯一实现点在读侧，复位值单源
        # 于规格表 reset_default。
        text = str(raw or "").strip() or spec.reset_value
        return _truncate_text(spec, text)
    if spec.kind == "list":
        try:
            return normalize_string_list(spec, raw, mode="disk")
        except ValueError:
            # 只有 raw 类型非法才走到这里；disk 模式对合法 list 不抛。
            logger.warning("[%s] %s list value invalid; using fallback", PLUGIN_ID, spec.key)
            return normalize_string_list(spec, fallback, mode="disk")
    if spec.kind == "str":
        return _truncate_text(spec, str(raw or "").strip())
    # 规格表写错 kind 不得静默降级：那会让 int/bool 键带着非法值落盘并显示成
    # 正常值。与上面缺边界的自检同口径，加载期响。
    raise RuntimeError(f"{spec.key}: 未知配置 kind {spec.kind!r}")


def normalize_config_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Return API updates in the same canonical form persisted by ``Settings``."""
    normalized: dict[str, Any] = {}
    for key, value in updates.items():
        spec = CONFIG_SPEC_BY_KEY[key]
        value = coerce_config_value(spec, value, spec.default)
        normalized[key] = spec.canonical_value(value)
    return normalized


def read_config_value(spec: ConfigSpec, config: Any) -> Any:
    """从宿主配置对象读一个键：正式键优先，缺失时按旧键顺序回退。

    只强制转换一次：把旧键值 coerce 成 fallback 再二次 coerce 会让同一份值
    走两遍边界与截断。``fallback`` 的语义是「``raw`` 强制失败时落回哪个值」：
    正式键存在时落回旧键的值而非静态默认。守卫：
    ``test_spec_table_legacy_fallback_matches_from_config``。
    """
    raw: Any = spec.default
    fallback: Any = spec.default
    if spec.key in config:
        raw = config.get(spec.key)
        for legacy in spec.legacy_keys:
            if legacy in config:
                fallback = coerce_config_value(spec, config.get(legacy), spec.default)
                break
    else:
        for legacy in spec.legacy_keys:
            if legacy in config:
                raw = config.get(legacy)
                break
    return coerce_config_value(spec, raw, fallback)


@dataclass
class Settings:
    enabled: bool
    judge_provider_id: str
    decision_prompt_template: str
    decision_history_min_messages: int
    decision_temperature: float
    decision_timeout_sec: float
    decision_model_enabled: bool
    reply_length_mode: str
    allow_multiline_reply: bool
    max_reply_chars: int
    quote_mode: str
    quote_probability: int
    mention_mode: str
    mention_probability: int
    log_reply_content: bool
    bot_aliases: list[str]
    whitelist: set[str]
    enabled_private_sessions: bool
    abandon_stale_on_new_message: bool
    skip_after_direct_call: bool
    ignored_sender_ids: set[str]
    recent_message_limit: int
    message_delay_sec: int
    min_silence_sec: int
    cooldown_sec: int
    max_daily_replies_per_session: int
    quiet_hours: list[str]
    enabled_message_trigger: bool
    enabled_patrol_trigger: bool
    check_interval_sec: int
    patrol_inactive_after_sec: int
    generation_timeout_sec: float
    proactive_inherit_tools: bool
    vision_judge_enabled: bool
    vision_main_enabled: bool
    vision_provider_id: str
    vision_judge_provider_id: str
    vision_skip_stickers: bool
    vision_max_images: int
    vision_image_age_sec: int
    vision_timeout_sec: float

    @property
    def vision_judge_provider_resolved(self) -> str:
        """判断阶段实际使用的识图 Provider ID。

        判断阶段触发频率高，允许单独指定更便宜的识图模型；留空回落主识图
        Provider，两者都空则由 adapter 回落当前会话模型。
        """
        return (
            str(self.vision_judge_provider_id or "").strip()
            or str(self.vision_provider_id or "").strip()
        )

    @property
    def vision_enabled(self) -> bool:
        """Whether any Vision path is active.

        Image events only need to be retained when at least one of the judge
        or main paths will actually consume them.
        """
        return self.vision_judge_enabled or self.vision_main_enabled

    @property
    def decision_prompt_custom(self) -> bool:
        """用户是否自定义了判断提示词（空值与内置默认都算「未自定义」）。

        比照 ``ConfigSpec.reset_value`` 而非模板常量的字形，与读侧落盘、
        面板填充取同一表达式。
        """
        prompt = str(self.decision_prompt_template or "").strip()
        return bool(prompt and prompt != CONFIG_SPEC_BY_KEY["decision_prompt_template"].reset_value)

    def apply(self, other: Settings) -> None:
        """原地写入另一实例的全部字段，保持对象身份不变。

        运行组件构造时各存 self.settings 引用；热更新/回滚若整体替换
        plugin.settings，组件会读到过期配置。
        """
        self.__dict__.update(other.__dict__)

    @classmethod
    def from_config(cls, config: Any) -> Settings:
        """把宿主配置对象归一化为 ``Settings``：缺键取默认，超限截断，别名回退。

        表驱动：每个键的类型/边界/旧键/上限都只在 ``CONFIG_SPECS`` 声明一次。
        输入不可信（用户手改 JSON、旧版本遗留键），每个字段都走类型强制 +
        边界裁剪。别名回退只在读侧生效，``to_config_dict()`` 只写正式键。

        失败时**从不抛异常**：全部降级为默认值或截断后的安全值并按项记
        warning。配置解析失败若抛出会让插件整体加载失败，而单个字段异常
        不该导致主动回复完全不可用。
        """
        return cls(**{spec.attr: read_config_value(spec, config) for spec in CONFIG_SPECS})

    def to_config_dict(self) -> dict[str, Any]:
        """Return only currently active configuration keys.

        表驱动：键名与顺序都取自 ``CONFIG_SPECS``。Legacy alias keys are read
        by ``from_config`` but never written back: they are absent from
        ``_conf_schema.json``, so writing them makes the host settings panel
        render a stray editable text box that has no effect.
        """
        payload: dict[str, Any] = {}
        for spec in CONFIG_SPECS:
            payload[spec.key] = spec.canonical_value(getattr(self, spec.attr))
        return payload


def config_revision(config: Mapping[str, Any] | Settings) -> str:
    """Return a stable digest of canonical persistent configuration fields."""
    payload = config.to_config_dict() if isinstance(config, Settings) else dict(config)

    def canonical(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): canonical(item) for key, item in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted((canonical(item) for item in value), key=lambda item: repr(item))
        if isinstance(value, (list, tuple)):
            return [canonical(item) for item in value]
        return value

    encoded = json.dumps(
        canonical(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
