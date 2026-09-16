"""数据形状、配置规约与无依赖纯函数。

拥有：会话状态与投递结果的数据类、``Settings`` / ``ConfigSpec`` 的配置读写
规约、上限常量、时间与类型转换纯函数、跨模块回调的 ``Protocol`` 形状。

不拥有任何 I/O 与业务判断：落盘属 ``storage``，宿主字段读取属 ``utils``，
是否接话属 ``decision``。本模块是依赖图的叶子（只依赖标准库与宿主 logger），
19 个生产模块从这里取形状，反向依赖会立刻成环。

分区目录：安全上限常量 → 危险工具清单 → 提示词模板 → 通用纯函数 →
``AttemptLedger`` 状态机 → 配置规格表 / ``Settings``。分区只做定位，
不拆文件（拆分收益抵不过契约测试的摩擦成本）。
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
PLUGIN_VERSION = "1.3.3"
COMMAND_HANDLED_KEY = f"{PLUGIN_ID}:command_handled"
STATE_VERSION = 4

# 配置安全限制
MAX_PROMPT_LENGTH = 8000  # 提示词最大长度，防止 OOM 和费用爆炸
MAX_WHITELIST_SIZE = 1000  # 白名单最大条目数，防止性能降级
MAX_STRING_LIST_ITEM_LEN = (
    200  # 字符串列表条目最大长度（白名单/别名/忽略名单等共用），防止垃圾长条目
)
# str 类键（provider id）的硬上限：与列表条目同宽，防止无限长字符串落盘。
MAX_PROVIDER_ID_LEN = 200
# 与前端 pages/主动回复设置/config-form.mjs 的 WHITELIST_ILLEGAL_RE 同字符集。
# 控制字符 + 引号 + 反斜杠：过长文案截进 logger.warning 时不能伪造日志行。
STRING_LIST_ILLEGAL_RE = re.compile(r"[\x00-\x1f\"'\\]")
MAX_BOT_ALIASES = 64
MAX_IGNORED_SENDER_IDS = 1000
MAX_QUIET_HOURS = 24
# 历史消息缓存条数的默认值：``recent_message_limit`` 规格与 ``SessionState.recent``
# 的兜底 maxlen 共用。两处各写 20 时，改默认值会漏掉「规格表新值 + 空会话仍按旧值
# 建 deque」这一半，新会话的近期窗口与配置面板显示不一致。
RECENT_MESSAGE_LIMIT_DEFAULT = 20
MAX_RECENT_MESSAGE_LIMIT = 100  # 历史消息最大缓存数
# 生成路径上下文（历史文本）总字符预算：宿主单条消息长度不受本插件约束，
# 100 条缓存上限挡不住成本失控。判断路径已有 2000 cap（decision 提示词
# 变量净化），生成路径此前零预算——长文群会把整段刷屏历史灌进主 Agent。
# 6000 ≈ 默认 20 条 × 常见消息长度，正常会话永不触发，只裁病态长史。
MAX_GENERATION_CONTEXT_CHARS = 6000
MAX_DAILY_REPLIES_LIMIT = 1000  # 每日回复次数上限
MAX_VISION_IMAGES = 5  # 单次主动回复最多解析的图片数
MAX_VISION_IMAGE_AGE_SEC = 86400  # 图片上下文最长保留时间
MAX_VISION_TIMEOUT_SEC = 120  # 单张图片解析超时上限
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 单张图片字节上限（远程下载与本地读取共用）
MAX_CACHED_IMAGE_EVENTS = 20  # 每会话临时保留的含图事件数
MAX_IMAGE_CACHE_BYTES = 256 * 1024 * 1024  # 图片冻结缓存总容量上限
MAX_IMAGE_DESCRIPTION_CACHE_BYTES = 512 * 1024  # Vision 描述内存缓存上限
MAX_IMAGE_MEMORY_BYTES = 64 * 1024 * 1024  # 全局事件图片 data URL 内存上限
MAX_SESSION_IMAGE_MEMORY_BYTES = 16 * 1024 * 1024  # 单会话图片 data URL 内存上限

# 代次失效时返回给调用方的用户可见文案。两条措辞相近但语义不同，不可互换：
# 前者用于「发送尚未开始就发现代次已变」（放弃整个任务），后者用于「回复已生成
# 但发送未达成」（只放弃这条回复）。合并二者会改变已发布的用户可见输出。
STALE_TASK_MESSAGE = "会话已经更新，放弃旧任务。"
STALE_REPLY_MESSAGE = "会话已更新，放弃旧回复。"

# 泄漏告警阈值：后台任务表/会话代次表规模超阈值时在周期
# 清理中告警（运维状态）。任务表每会话至多 1-2 个常驻条目，100 已显著
# 高于正常规模；代次表条目数 ≤ 白名单上限（1000），1500 即泄漏。
LEAK_WARN_TASK_THRESHOLD = 100
LEAK_WARN_SESSION_THRESHOLD = 1500
# 生成上下文文本记录预算下限（与 decision_history_min_messages 默认值一致）：
# 本地文本记录不足此数时才回宿主补历史。
MIN_RECENT_TEXT_RECORDS = 5

# 插件运行常量
MAX_AGENT_STEPS = 15  # Agent 最大步数：为主 Agent 生成预留足够步数
MAX_DIRECT_TOOL_SENDS = 2  # 每次主动回复最多允许工具直接发出的消息数
# 生成超时后留给 run_agent 优雅退出的宽限秒数：request_stop 后宿主会
# 正常清理内部任务（如 stop_watcher），宽限过后仍未退出才兜底取消。
GRACEFUL_STOP_GRACE_SEC = 3.0
# terminate() 等待后台/关键任务的硬窗口；与宽限同值但是两条语义，
# 不能合成一个常数。stop_timeout fallback 必须引用这里，禁止再写 3.0。
TERMINATE_TASK_TIMEOUT_SEC = 3.0
# 指令动作集合：help/status/list/debug 为只读，不触碰会话任务；
# add/remove/check/on/off 为写操作，各自内部处理会话失效语义。
ADMIN_COMMAND_ACTIONS = {"status", "list", "add", "remove", "check", "on", "off", "debug"}
# 进入命令即需取消在途主动回复的写操作集合。只读动作（help/status/list/debug）
# 不打断正在进行的回复检查；写操作在权限校验通过后才触发取消。
SESSION_CANCEL_COMMAND_ACTIONS = frozenset({"add", "remove", "check", "on", "off"})
# 0.7.x 主动 Agent 工具允许列表：默认空集，未列入的工具在 build/hook 后一律移除，
# 无法验证时终止本次主动运行（fail closed）。后续如需放行工具，必须提供稳定工具
# ID、明确 owner、行为测试和独立安全评审后才能加入。
PROACTIVE_ALLOWED_TOOL_IDS: frozenset[str] = frozenset()
# 宿主级危险能力工具 ID（实证于 AstrBot 4.26/4.27 源码
# astrbot/core/{computer,tools,cron_tools,knowledge_base_tools}）：
# cron（create_future_task 等）、电脑使用（shell/python/browser/fs）、文档提取、知识库 agentic。
# 无论 proactive_inherit_tools 开关如何，这些工具在主动运行中一律拒绝；
# 该清单是 build config 硬关闭（add_cron_tools/computer_use_runtime/file_extract/kb_agentic）
# 之外的最终防线，用于拦截 hook 在 build 后注入的宿主危险工具。
HOST_DANGEROUS_TOOL_IDS: frozenset[str] = frozenset(
    {
        # cron（astrbot/core/tools/cron_tools.py，4.23.3 实测为单工具 multiCommand：
        # create/delete/list 均为子命令，FunctionTool.name 为 future_task）
        "future_task",
        # shell / python（astrbot/core/computer/tools/{shell,python}.py）
        "astrbot_execute_shell",
        "astrbot_execute_ipython",
        "astrbot_execute_python",
        # shell 会话（astrbot 4.27.1 新增，本机无此版本，采信审查方 wheel 证据）
        "astrbot_shell_session",
        # browser / computer use（astrbot/core/computer/tools/browser.py）
        "astrbot_execute_browser",
        "astrbot_execute_browser_batch",
        "astrbot_run_browser_skill",
        # filesystem（astrbot/core/computer/tools/fs.py，4.23.3 实测实际 name；
        # astrbot_create_file/astrbot_read_file/astrbot_read_file_tool 为死条目已删）
        "astrbot_upload_file",
        "astrbot_download_file",
        "astrbot_file_read_tool",
        "astrbot_file_write_tool",
        "astrbot_file_edit_tool",
        "astrbot_grep_tool",
        # knowledge base agentic（astrbot/core/tools/knowledge_base_tools.py）
        "astr_kb_search",
    }
)
# 危险工具名启发式：仅漂移守卫（tests/test_security.py）使用，不替代
# HOST_DANGEROUS_TOOL_IDS。运行路径以精确 denylist + 空 allowlist 为准，
# 避免启发式误伤无害工具。
_HOST_DANGEROUS_TOOL_NAME_RE = re.compile(
    r"(future_task|shell_session|execute_(shell|ipython|python|browser)|"
    r"run_browser_skill|upload_file|download_file|file_(read|write|edit)_tool|"
    r"grep_tool|kb_search)",
    re.IGNORECASE,
)


def looks_like_host_dangerous_tool(tool_id: str) -> bool:
    """Return whether a tool id looks host-dangerous (denylist membership or name hint)."""
    name = str(tool_id or "").strip()
    if not name:
        return False
    if name in HOST_DANGEROUS_TOOL_IDS:
        return True
    return _HOST_DANGEROUS_TOOL_NAME_RE.search(name) is not None


REPLY_REQUEST_WINDOW_SEC = 180  # 明确请求窗口：3分钟内的接话请求视为有效
EVENT_CLEANUP_INTERVAL_SEC = 3600  # 事件清理间隔：1小时清理一次陈旧事件
MAX_CACHED_EVENTS = 100  # 最大缓存事件数：防止内存无限增长
PATROL_BACKOFF_DELAY_SEC = 60  # 巡检失败退避延迟：避免错误循环
# release 闸门等待兜底：配置回滚会把运行标记恢复成快照态，
# 而支撑它的检查任务可能已在回滚窗口内结束。此时 release 事件永远不会
# 再被 set，裸 wait() 将永久挂起。超时 + 轮次上限把闸门失同步降级为
# 一次延迟或一次丢弃，而不是让该会话静默死亡或空转独占事件循环。
RELEASE_WAIT_TIMEOUT_SEC = 30
MAX_RELEASE_WAIT_ROUNDS = 20
# 管理员列表重探窗口：高频事件路径在窗口内跳过对 cmd_config.json 的 stat
# （mtime 缓存只省读文件不省系统调用）；运行期改管理员下个窗口生效，
# 最大延迟 = 窗口长，探测失败不变更缓存、窗口后重试。
ADMIN_REFRESH_WINDOW_SEC = 30.0
# 外部时间戳的容许时钟偏移：状态文件是可被手工编辑的外部输入，
# 而 _finite_float 只挡 NaN/inf，负值与远未来原样穿透。两个方向危害不同：
#
# - 远未来（now+1e9）：remaining_silence_sec 变成数十年，该会话永久锁死；
#   延迟检查以巨值为 timeout 停放，唤醒后重算仍是巨值又停回去，成为不死任务；
#   且巡检的 now - last_active_at 为负，永不大于 patrol_inactive_after_sec，
#   巡检每轮都白跑一次这个已锁死的会话。
# - 负值：单独毒 last_active_at 会被「这条消息之后已经主动回复过」拦住，但把
#   last_proactive_observed_at 一并毒成更负即可放行——全新会话被拦、毒过的放行，
#   是真实的能力提升。
#
# 取 300 秒：足够覆盖 NTP 校正与容器宿主间的正常漂移，又把投毒的可利用窗口
# 压到一次普通延迟。上界用 now + 偏移而非硬编码绝对时刻，避免随时间失效。
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
- 默认克制，但不要过于沉默。只在接话自然、有信息量或能轻微活跃气氛时 should_reply=true。
- 可以回复的情况（满足其一即可，但必须不打扰当前对话节奏）:
  1. 群友明确点名 Bot 昵称，并要求接话、回复、发表情包、找图、发图。
  2. 群友在讨论技术/知识/工具类问题，Bot 能补充一个简短有用的信息、提醒或判断。
  3. 群聊明显冷场，最后一条是开放式提问、评价或吐槽，轻短接一句能自然续上话题。
  4. 最近有可附和的公开吐槽、玩笑或轻松话题，Bot 接一句不会打断任何人，也不会显得刷存在感。
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
  - 如果只是可以接但价值很低，倾向 false；如果一句话能自然补充或暖场，可以 true。
- 即使决定回复，也只适合轻短自然的一句，像群友随口搭话，不要长篇大论、不要强行刷存在感。

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
# 可打印字符的 Unicode 码点下界：控制字符（0x00–0x1F）一律从提示词变量里剔除。
_PRINTABLE_CHAR_MIN = 32

# 文本空白归一的正则常量住在 models：utils 依赖 models（依赖图叶子），反向会成环。
# 此前 models 内联一份 ``re.sub(r"[^\S\n]+", ...)``、utils 各自编译一份，两边
# 同时漂移就会让「同一段文本在不同路径被压缩成不同形状」。
WHITESPACE_PATTERN = re.compile(r"\s+")
# 行内空白：不含换行，用于保留多行结构时压缩空格
INLINE_SPACE_PATTERN = re.compile(r"[^\S\n]+")


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < _MINUTE_SECONDS:
        return f"{seconds}s"
    if seconds < _HOUR_SECONDS:
        return f"{seconds // _MINUTE_SECONDS}m{seconds % _MINUTE_SECONDS}s"
    return f"{seconds // _HOUR_SECONDS}h{seconds % _HOUR_SECONDS // _MINUTE_SECONDS}m"


def as_bool(value: Any, default: bool = False) -> bool:
    """Parse a persisted/host config flag. Broader than ``parse_decision_json``.

    Config values come from Dashboard/JSON files and historically used on/启用/开启.
    Decision-model JSON only accepts true/yes/1/是 — do not reuse this set there.
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


def as_float(value: Any, default: float, minimum: float = 0.0, maximum: float = 300.0) -> float:
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

    与 ``as_float`` 的区别只在上界是动态的：时间戳的合法上界随时钟走，写死一个
    绝对值会随时间失效。NaN/inf/不可解析一律归 0.0（等价「从未活跃」），与
    ``_finite_float`` 的原语义一致；新增的是两侧钳位。

    ``now`` 可注入以便测试；默认取 ``now_ts()``。
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

    签名不可检查时返回首个候选（与各调用点原有回退一致）。只做 ``bind``
    预检、绝不调用——函数体内的 TypeError 必须由调用方处理，在此重试意味
    同一宿主调用可能执行两次（对落盘/LLM 即重复副作用）。
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
    """原地恢复容器内容，不换容器对象。

    等待者与运行中的 ``async with`` 持有容器本身的引用，重绑定会制造孤儿表。
    """
    target.clear()
    target.update(source)


def sanitize_prompt_variable(
    text: str,
    max_length: int = 500,
    *,
    allow_newlines: bool = False,
) -> str:
    """清理用于提示词的变量，防止注入和长度攻击。

    提示词是纯文本，不是 JSON，所以不做反斜杠转义（那只会把 `\\"` 当成字
    面量塑进模型看到的内容）。双引号改成中文引号，既不破坏可读性，又能避免
    用户内容伪造出与输出契约一模一样的 JSON 片段。

    Args:
        text: 原始文本
        max_length: 最大长度限制
        allow_newlines: 是否保留换行。多行聊天记录必须保留行结构，
            否则判断模型无法区分发言人和轮次；单字段变量保持单行。

    Returns:
        清理后的安全文本
    """
    text = str(text or "").strip()
    if not text:
        return ""

    # 1. 截断长度
    if len(text) > max_length:
        text = text[:max_length] + "..."

    # 2. 双引号改写，避免伪造 JSON 输出契约
    text = text.replace('"', "“")

    if allow_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for line in text.split("\n"):
            # 移除控制字符并压缩行内空白
            line = "".join(char for char in line if ord(char) >= _PRINTABLE_CHAR_MIN)
            line = INLINE_SPACE_PATTERN.sub(" ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    # 3. 单行模式：换行、制表符归一为空格，并移除控制字符
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


def history_display_name(role: str, name: str | None = None) -> str:
    """历史展示名：助手固定 Bot，其他人用名字，缺名才回落用户。"""
    if role == "assistant":
        return "Bot"
    return str(name or "用户")


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
    REPLY_REQUEST = "reply_request"
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

    ``detail`` 是给人看的自由文本，不能拿它做分支——``"stopping" in detail``
    这类判定会在措辞调整时静默失效（改文案不该改变控制流）。调用方要区分的
    成因放这里，``detail`` 只进日志。

    四个成员是**完整的成因分类**，不按"当前有几个读取点"裁剪：每个成员都有
    真实构造点（守卫强制声明，漏填即红），新增分支时按语义取用现成成员，
    而不是把分类重新拆一遍。目前只有 ``STOPPING`` 有读取点（决定回显文案），
    其余三个是分类域的一部分——这与"不留零消费者字段"不矛盾：那个口径针对
    的是无任何写入者的投机字段，而这些成员有 9 个构造点在使用。
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

    ``eq=False``：账本按**身份**判定成员（``attempt not in self._attempts`` 走
    ``==``）。值相等会让另一账本里同号同文的 attempt 冒充本账本成员——
    ``attempt_id`` 每账本从 1 起，两个账本各发一条同样文本时字段全等——
    于是 resolve/mark_in_flight 会把状态写到别的账本的 attempt 上。
    """

    attempt_id: int
    kind: str
    text: str = ""
    state: AttemptState = AttemptState.RESERVED


# SendStatus → AttemptState 的两组固定映射。模块级常量：每次 resolve/
# finish_before_submit 调用重建 dict 是纯浪费，且两表语义不同（in-flight
# 出口 vs 预提交出口）不可合并。
_IN_FLIGHT_OUTCOMES: dict[SendStatus, AttemptState] = {
    SendStatus.DELIVERED: AttemptState.DELIVERED,
    SendStatus.UNKNOWN: AttemptState.UNKNOWN,
    SendStatus.FAILED_BEFORE_SUBMIT: AttemptState.FAILED_BEFORE_SUBMIT,
}
_PRE_SUBMIT_OUTCOMES: dict[SendStatus, AttemptState] = {
    SendStatus.FAILED_BEFORE_SUBMIT: AttemptState.FAILED_BEFORE_SUBMIT,
    SendStatus.SUPPRESSED: AttemptState.SUPPRESSED,
}


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
    phase: str = "open"
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

    def reserve(self, kind: str, text: str = "") -> SendAttempt:
        if self.phase != "open":
            raise RuntimeError("cannot reserve an attempt after the ledger is sealed")
        attempt = SendAttempt(self._next_attempt_id, kind, text)
        self._next_attempt_id += 1
        self._attempts.append(attempt)
        return attempt

    def mark_in_flight(self, attempt: SendAttempt) -> None:
        if self.phase != "open" or attempt not in self._attempts:
            raise RuntimeError("cannot start an attempt outside an open ledger")
        if attempt.state is not AttemptState.RESERVED:
            raise RuntimeError("only a reserved attempt can enter the adapter")
        attempt.state = AttemptState.IN_FLIGHT

    def resolve(self, attempt: SendAttempt, status: SendStatus) -> bool:
        """Apply an adapter outcome, returning false for a sealed late result."""
        if self.phase != "open" or attempt not in self._attempts:
            return False
        if attempt.state is not AttemptState.IN_FLIGHT:
            raise RuntimeError("only an in-flight attempt can receive an adapter outcome")
        try:
            attempt.state = _IN_FLIGHT_OUTCOMES[status]
        except KeyError as exc:
            raise ValueError(f"invalid in-flight outcome: {status}") from exc
        return True

    def finish_before_submit(self, attempt: SendAttempt, status: SendStatus) -> None:
        if self.phase != "open" or attempt not in self._attempts:
            raise RuntimeError("cannot finish an attempt outside an open ledger")
        if attempt.state is not AttemptState.RESERVED:
            raise RuntimeError("only a reserved attempt can finish before adapter entry")
        try:
            attempt.state = _PRE_SUBMIT_OUTCOMES[status]
        except KeyError as exc:
            raise ValueError(f"invalid pre-submit outcome: {status}") from exc

    def start_recording(self, task: object) -> bool:
        """Register the ledger's sole persistence task after sealing evidence."""
        if self.phase != "sealed" or self._record_task is not None:
            return False
        self._record_task = task
        self.phase = "recording"
        return True

    def mark_recorded(self) -> None:
        if self.phase != "recording":
            raise RuntimeError("only a recording ledger can become recorded")
        self.phase = "recorded"

    def mark_record_failed(self, detail: str) -> None:
        if self.phase != "recording":
            raise RuntimeError("only a recording ledger can fail persistence")
        self.record_failure = detail
        self.phase = "record_failed"

    def seal(self) -> tuple[SendAttempt, ...]:
        """Freeze evidence; any in-flight adapter call is pessimistically UNKNOWN."""
        if self.phase != "open":
            return self.attempts
        for attempt in self._attempts:
            if attempt.state is AttemptState.RESERVED:
                attempt.state = AttemptState.ABANDONED
            elif attempt.state is AttemptState.IN_FLIGHT:
                attempt.state = AttemptState.UNKNOWN
        self.phase = "sealed"
        return self.attempts


@dataclass(frozen=True)
class SessionContainers:
    """main 侧共享容器的收拢视图（按名字交给需要多个容器的协作者）。

    存在的理由是把「这些集合必须始终保持同一身份」这条承重契约（见
    ``docs/BEHAVIOR_CONTRACT.md`` §11 B1）变成一个有名字的实体：改动容器集合时
    只有这一处需要同步，文档与守卫也指向同一份清单。

    ``frozen=True`` 只阻止字段重绑（该契约的失效形态之一）；容器内容仍按
    B1 原地修改。**不**把 ``SessionGate`` 的三张表收进来：``release`` 表按
    §11 B3 刻意不参与快照恢复，混进同一对象会诱导"整对象恢复"这种错误写法。
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
    last_active_sender_id: str = ""
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

        ``confirmed=False`` 表示 UNKNOWN 投递：只消耗冷却与日配额，
        不写历史条目。

        先 ``refresh_day`` 再自增：调用方（``SessionPipeline.check_session_locked``）的跨天
        刷新发生在判断+生成之前，二者相隔可达数十秒（判断超时 20s + 生成
        超时 60s）。跨零点时增量会记到昨日键上，随下一次刷新归零 —— 等于
        今日配额白送一次。本方法自带刷新后不再依赖调用方的时序。
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

    此前同一个键要在六处重复声明：``_conf_schema.json``、``Settings`` 字段表、
    ``from_config``、``to_config_dict``、``CONFIG_SCHEMA_KEYS``、
    ``_parse_config_updates``。新增一个键要改三到四处，漏一处审计就静默失效。
    本表驱动后四者 + ``_AUDITED_CONFIG_KEYS``；``_conf_schema.json`` 保留独立文件
    （它承载 UI 文案），由 ``tests/test_config_schema.py`` 断言与本表一致。

    为什么描述/提示文案不进表：那是纯 UI 拷贝（每条 1-3 行中文），放进表只会
    让表变成 schema 的第二份副本。表只收机器可校验的语义：类型、边界、步长、
    枚举、UI 控件类型、审计标记。

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
        max_len/max_items: 硬上限（防 OOM 与费用滥用），超限截断并记 warning。
        item_max_len/item_pattern/empty_policy: list/set 条目的统一规范化规则。
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
    empty_policy: str = ""
    # 空提交复位的内置默认（目前唯一消费者是 text 类键）。复位语义只在读侧
    # coerce_config_value 实现一次，webapi._strict_value 不做回落，以免两处
    # 各持一份口径（曾出现过「写侧落默认值、读侧再判差异」的误报链）。
    reset_default: Any = ""
    surfaces: frozenset[str] = frozenset({"host"})

    @property
    def reset_value(self) -> str:
        """复位后的实际取值（``reset_default`` 的规范化口径）。

        GET /config 的 ``decision_prompt_default``（面板「恢复默认」填充的值）
        与 ``coerce_config_value`` 落盘的默认必须取同一个表达式：两处各写一次
        ``str(...).strip()`` 时，只要常量字形带首尾空白，面板填回的默认就与
        读侧落盘的默认不等，"恢复默认 → 保存"会被 ``_config_update_was_adjusted``
        误报成改过字段。
        """
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
        """set 容器按排序输出：JSON 无集合类型，无序写盘会让每次保存都产生伪 diff。"""
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
        5,
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
    ConfigSpec("log_reply_content", "bool", False),
    ConfigSpec(
        "bot_aliases",
        "list",
        [],
        container="list",
        max_items=MAX_BOT_ALIASES,
        item_max_len=MAX_STRING_LIST_ITEM_LEN,
        item_pattern=STRING_LIST_ILLEGAL_RE.pattern,
        empty_policy="drop",
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
        empty_policy="drop",
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
        empty_policy="drop",
        surfaces=_PANEL,
    ),
    ConfigSpec("enabled_private_sessions", "bool", True, surfaces=_PANEL),
    ConfigSpec(
        "abandon_stale_on_new_message",
        "bool",
        False,
        surfaces=_PANEL,
    ),
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
        empty_policy="drop",
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
        60,
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
        if spec.empty_policy == "drop":
            return None, 1, 0
        return text, 0, 0
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
    list content.

    ``api`` is only for ``webapi._string_list`` (400 on illegal input).
    ``coerce_config_value`` / ``from_config`` always use ``disk`` so a bad
    on-disk list cannot refuse plugin load.
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


def coerce_config_value(spec: ConfigSpec, raw: Any, fallback: Any) -> Any:
    """按规格把一个原始配置值强制成目标类型并夹取边界。

    ``fallback`` 与 ``raw`` 分开传：旧键回退时 ``raw`` 取自旧键，而强制失败
    （None / 不可解析）时要落回同一个旧键的值，而非静态默认——这正是
    ``vision_enabled`` 迁移到两个新开关的语义（0.9.2 迁移护栏）。

    截断（提示词长度 / 白名单条目数）是防 OOM 与 token 滥用的硬边界，静默
    生效但必须留 warning，否则用户困惑于"配置没生效"。
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
        # 空值回落默认模板（面板留空即复位）：这条语义的唯一实现点在读侧——
        # 写侧 webapi._strict_value 只规范化空白，复位值单源于规格表 reset_default。
        text = str(raw or "").strip() or spec.reset_value
        if spec.max_len is not None and len(text) > spec.max_len:
            logger.warning(
                "[%s] 判断提示词过长 (%d 字符)，已截断到 %d 字符",
                PLUGIN_ID,
                len(text),
                spec.max_len,
            )
            text = text[: spec.max_len]
        return text
    if spec.kind == "list":
        try:
            return normalize_string_list(spec, raw, mode="disk")
        except ValueError:
            logger.warning("[%s] %s list value invalid; using fallback", PLUGIN_ID, spec.key)
            try:
                return normalize_string_list(spec, fallback, mode="disk")
            except ValueError:
                return normalize_string_list(spec, spec.default, mode="disk")
    if spec.kind == "str":
        text = str(raw or "").strip()
        if spec.max_len is not None and len(text) > spec.max_len:
            logger.warning(
                "[%s] %s 过长 (%d 字符)，已截断到 %d 字符",
                PLUGIN_ID,
                spec.key,
                len(text),
                spec.max_len,
            )
            text = text[: spec.max_len]
        return text
    return str(raw or "").strip()


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

    只强制转换一次。曾经写成「先把旧键值 coerce 成 fallback，再把 fallback 当
    raw 二次 coerce」，对 list 类键会静默清空——``container="set"`` 的第一次
    coerce 产出 ``set``，而列表条目归一化只认 list/str，第二次遇到 set 得
    空列表。存量配置里只有 ``whitelist``（无 ``whitelist_sessions``）的用户
    会整表丢白名单。规格表落地时由 ``test_spec_table_legacy_fallback_matches_from_config`` 抓到。

    ``fallback`` 的语义是「``raw`` 强制失败时落回哪个值」：正式键存在时落回旧键
    的值而非静态默认，这是 ``vision_enabled`` → 两个新开关的迁移语义（0.9.2）。
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
    log_reply_content: bool
    bot_aliases: list[str]
    whitelist: set[str]
    enabled_private_sessions: bool
    abandon_stale_on_new_message: bool
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

        判断阶段触发频率高，允许单独指定一个更便宜的识图模型。
        留空时回落到主识图 Provider；两者都留空则由 adapter
        进一步回落到当前会话模型。
        """
        return (
            str(self.vision_judge_provider_id or "").strip()
            or str(self.vision_provider_id or "").strip()
        )

    @property
    def vision_enabled(self) -> bool:
        """Whether any Vision path is active.

        Used by the event-caching gate and parser construction: image events
        only need to be retained when at least one of the judge or main paths
        will actually consume them.
        """
        return self.vision_judge_enabled or self.vision_main_enabled

    @property
    def decision_prompt_custom(self) -> bool:
        """用户是否自定义了判断提示词（空值与内置默认都算「未自定义」）。"""
        prompt = str(self.decision_prompt_template or "").strip()
        # 比照 ``ConfigSpec.reset_value`` 而非模板常量的字形：这里是第三种
        # 口径（"与默认值相等即未自定义"），必须与读侧落盘、面板填充取同一表达式。
        return bool(prompt and prompt != CONFIG_SPEC_BY_KEY["decision_prompt_template"].reset_value)

    def apply(self, other: Settings) -> None:
        """原地写入另一实例的全部字段，保持对象身份不变。

        运行组件（decision/generation/delivery/scheduler/whitelist/pipeline）
        构造时各存 self.settings 引用；配置热更新/回滚若整体替换
        plugin.settings，组件会读到过期配置（整体替换 Settings 会造成组件读旧值）。
        本方法让全部持有者经既有引用即时可见新值。Settings 是普通
        dataclass（无 __slots__/frozen），__dict__.update 即全字段同步。
        """
        self.__dict__.update(other.__dict__)

    @classmethod
    def from_config(cls, config: Any) -> Settings:
        """把宿主配置对象归一化为 ``Settings``：缺键取默认，超限截断，别名回退。

        表驱动：逐字段手写的归一化已由 ``CONFIG_SPECS`` 取代。每个键的
        类型/边界/旧键/上限都只在规格表里声明一次，此处只做遍历。

        输入不可信（用户手改 JSON、旧版本遗留键），因此每个字段都走类型强制 +
        边界裁剪，而不是直接取值。别名回退（如 ``whitelist`` → ``whitelist_sessions``）
        只在读侧生效，``to_config_dict()`` 只写正式键，一次 load+save 后旧键自然消失。

        失败时：**从不抛异常**，全部降级为默认值或截断后的安全值，并按项记
        warning。理由是配置解析失败若抛出会让插件整体加载失败，而单个字段异常
        不该导致主动回复完全不可用；超限截断（提示词/白名单）是防内存与 token
        滥用的硬边界，静默生效但必须留日志，否则用户会困惑于"配置没生效"。
        """
        return cls(**{spec.attr: read_config_value(spec, config) for spec in CONFIG_SPECS})

    def to_config_dict(self) -> dict[str, Any]:
        """Return only currently active configuration keys.

        表驱动：键名与顺序都取自 ``CONFIG_SPECS``，不再手抄。

        Deprecated direct-model/direct-plugin settings are ignored and no longer
        written back because proactive replies now use AstrBot's main Agent
        pipeline. Legacy alias keys (``vision_enabled``/``whitelist`` 等) are read
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
