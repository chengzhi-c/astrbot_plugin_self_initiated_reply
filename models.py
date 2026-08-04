from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PLUGIN_ID = "astrbot_plugin_self_initiated_reply"
PLUGIN_VERSION = "0.8.5"
COMMAND_HANDLED_KEY = f"{PLUGIN_ID}:command_handled"
STATE_VERSION = 4

# 配置安全限制
MAX_PROMPT_LENGTH = 8000  # 提示词最大长度，防止 OOM 和费用爆炸
MAX_WHITELIST_SIZE = 1000  # 白名单最大条目数，防止性能降级
MAX_STRING_LIST_ITEM_LEN = (
    200  # 字符串列表条目最大长度（白名单/别名/忽略名单等共用），防止垃圾长条目
)
MAX_RECENT_MESSAGE_LIMIT = 100  # 历史消息最大缓存数
MAX_DAILY_REPLIES_LIMIT = 1000  # 每日回复次数上限
MAX_VISION_IMAGES = 5  # 单次主动回复最多解析的图片数
MAX_VISION_IMAGE_AGE_SEC = 86400  # 图片上下文最长保留时间
MAX_VISION_TIMEOUT_SEC = 120  # 单张图片解析超时上限
MAX_CACHED_IMAGE_EVENTS = 20  # 每会话临时保留的含图事件数
MAX_IMAGE_CACHE_BYTES = 256 * 1024 * 1024  # 图片冻结缓存总容量上限

# 插件运行常量
MAX_AGENT_STEPS = 15  # Agent 最大步数：为主 Agent 生成预留足够步数
MAX_DIRECT_TOOL_SENDS = 2  # 每次主动回复最多允许工具直接发出的消息数
# 生成超时后留给 run_agent 优雅退出的宽限秒数：request_stop 后宿主会
# 正常清理内部任务（如 stop_watcher），宽限过后仍未退出才兜底取消。
GRACEFUL_STOP_GRACE_SEC = 3.0
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
REPLY_REQUEST_WINDOW_SEC = 180  # 明确请求窗口：3分钟内的接话请求视为有效
EVENT_CLEANUP_INTERVAL_SEC = 3600  # 事件清理间隔：1小时清理一次陈旧事件
MAX_CACHED_EVENTS = 100  # 最大缓存事件数：防止内存无限增长
PATROL_BACKOFF_DELAY_SEC = 60  # 巡检失败退避延迟：避免错误循环

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


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60}m"


def as_bool(value: Any, default: bool = False) -> bool:
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


def as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[\n,，]+", value) if item.strip()]
    return []


def choice(value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


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
            line = "".join(char for char in line if ord(char) >= 32)
            line = re.sub(r"[^\S\n]+", " ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    # 3. 单行模式：换行、制表符归一为空格，并移除控制字符
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = "".join(char for char in text if ord(char) >= 32)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class MessageRecord:
    role: str
    name: str
    text: str
    sender_id: str = ""
    at: float = field(default_factory=now_ts)


@dataclass(frozen=True)
class PipelineReply:
    """Result of one main-Agent run, including tool-side direct sends."""

    text: str = ""
    direct_send_count: int = 0
    direct_texts: tuple[str, ...] = ()


class SendStatus(str, Enum):
    """Outcome of one outbound attempt.

    UNKNOWN means the platform call may have reached the adapter, so callers
    must not blindly retry through a second channel.
    """

    DELIVERED = "delivered"
    FAILED_BEFORE_SUBMIT = "failed_before_submit"
    UNKNOWN = "unknown"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class SendOutcome:
    status: SendStatus
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.status is SendStatus.DELIVERED


@dataclass
class SessionState:
    recent: deque[MessageRecord] = field(default_factory=lambda: deque(maxlen=20))
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
        """
        self.last_proactive_at = at
        self.daily_count += 1
        if not confirmed:
            return
        self.last_proactive_text = text
        self.recent.append(MessageRecord(role="assistant", name="Bot", text=text, at=at))


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
        prompt = str(self.decision_prompt_template or "").strip()
        return bool(prompt and prompt != DEFAULT_DECISION_PROMPT_TEMPLATE.strip())

    @classmethod
    def from_config(cls, config: Any) -> Settings:
        from astrbot.api import logger

        # 提示词长度限制
        prompt_template = str(
            config.get("decision_prompt_template", "") or DEFAULT_DECISION_PROMPT_TEMPLATE
        ).strip()
        if len(prompt_template) > MAX_PROMPT_LENGTH:
            logger.warning(
                "[%s] 判断提示词过长 (%d 字符)，已截断到 %d 字符",
                PLUGIN_ID,
                len(prompt_template),
                MAX_PROMPT_LENGTH,
            )
            prompt_template = prompt_template[:MAX_PROMPT_LENGTH]

        # 白名单条目数限制
        whitelist_raw = as_list(config.get("whitelist_sessions", []))
        if len(whitelist_raw) > MAX_WHITELIST_SIZE:
            logger.warning(
                "[%s] 白名单过大 (%d 条目)，已截断到前 %d 条",
                PLUGIN_ID,
                len(whitelist_raw),
                MAX_WHITELIST_SIZE,
            )
            whitelist_raw = whitelist_raw[:MAX_WHITELIST_SIZE]

        # 旧版本只有一个 vision_enabled 开关，作为两个新开关的默认值迁移。
        # 只读不写：to_config_dict() 不再写回该键，否则它不在 schema 里，
        # AstrBot 配置页会把它渲染成一个既无效又可编辑的裸文本框。
        legacy_vision = as_bool(config.get("vision_enabled", False), False)
        vision_judge_enabled = as_bool(
            config.get("vision_judge_enabled", legacy_vision), legacy_vision
        )
        vision_main_enabled = as_bool(
            config.get("vision_main_enabled", legacy_vision), legacy_vision
        )

        return cls(
            enabled=as_bool(config.get("enabled", True), True),
            judge_provider_id=str(config.get("judge_provider_id", "") or "").strip(),
            decision_prompt_template=prompt_template,
            decision_history_min_messages=as_int(
                config.get("decision_history_min_messages", 5), 5, 0, 30
            ),
            decision_temperature=as_float(config.get("decision_temperature", 0.2), 0.2, 0.0, 2.0),
            decision_timeout_sec=as_float(config.get("decision_timeout_sec", 20), 20, 1, 300),
            decision_model_enabled=as_bool(config.get("decision_model_enabled", True), True),
            reply_length_mode=choice(
                config.get("reply_length_mode", "balanced"),
                {"short", "balanced", "expressive"},
                "balanced",
            ),
            allow_multiline_reply=as_bool(config.get("allow_multiline_reply", True), True),
            max_reply_chars=as_int(config.get("max_reply_chars", 220), 220, 0, 2000),
            log_reply_content=as_bool(config.get("log_reply_content", False), False),
            bot_aliases=as_list(config.get("bot_aliases", [])),
            whitelist=set(whitelist_raw),
            ignored_sender_ids=set(as_list(config.get("ignored_sender_ids", []))),
            recent_message_limit=as_int(
                config.get("recent_message_limit", 20), 20, 3, MAX_RECENT_MESSAGE_LIMIT
            ),
            message_delay_sec=as_int(config.get("message_delay_sec", 60), 60, 5, 86400),
            min_silence_sec=as_int(config.get("min_silence_sec", 45), 45, 0, 86400),
            cooldown_sec=as_int(config.get("cooldown_sec", 900), 900, 0, 86400),
            max_daily_replies_per_session=as_int(
                config.get("max_daily_replies_per_session", 5), 5, 0, MAX_DAILY_REPLIES_LIMIT
            ),
            quiet_hours=as_list(config.get("quiet_hours", [])),
            enabled_message_trigger=as_bool(config.get("enabled_message_trigger", True), True),
            enabled_patrol_trigger=as_bool(config.get("enabled_patrol_trigger", False), False),
            check_interval_sec=as_int(config.get("check_interval_sec", 300), 300, 30, 86400),
            patrol_inactive_after_sec=as_int(
                config.get("patrol_inactive_after_sec", 1800), 1800, 0, 604800
            ),
            generation_timeout_sec=as_float(config.get("generation_timeout_sec", 60), 60, 1, 300),
            proactive_inherit_tools=as_bool(config.get("proactive_inherit_tools", False), False),
            vision_judge_enabled=vision_judge_enabled,
            vision_main_enabled=vision_main_enabled,
            vision_provider_id=str(config.get("vision_provider_id", "") or "").strip(),
            vision_judge_provider_id=str(config.get("vision_judge_provider_id", "") or "").strip(),
            vision_skip_stickers=as_bool(config.get("vision_skip_stickers", False), False),
            vision_max_images=as_int(config.get("vision_max_images", 2), 2, 1, MAX_VISION_IMAGES),
            vision_image_age_sec=as_int(
                config.get("vision_image_age_sec", 300), 300, 60, MAX_VISION_IMAGE_AGE_SEC
            ),
            vision_timeout_sec=as_float(
                config.get("vision_timeout_sec", 20), 20, 1, MAX_VISION_TIMEOUT_SEC
            ),
        )

    def to_config_dict(self) -> dict[str, Any]:
        """Return only currently active configuration keys.

        Deprecated direct-model/direct-plugin settings are ignored and no longer
        written back because proactive replies now use AstrBot's main Agent
        pipeline. Stealer and LivingMemory participate through their normal
        tool/hooks instead of this plugin's legacy adapters.
        """
        return {
            "enabled": self.enabled,
            "decision_model_enabled": self.decision_model_enabled,
            "judge_provider_id": self.judge_provider_id,
            "decision_prompt_template": self.decision_prompt_template,
            "decision_history_min_messages": self.decision_history_min_messages,
            "decision_temperature": self.decision_temperature,
            "decision_timeout_sec": self.decision_timeout_sec,
            "reply_length_mode": self.reply_length_mode,
            "allow_multiline_reply": self.allow_multiline_reply,
            "max_reply_chars": self.max_reply_chars,
            "log_reply_content": self.log_reply_content,
            "bot_aliases": self.bot_aliases,
            "ignored_sender_ids": sorted(self.ignored_sender_ids),
            "whitelist_sessions": sorted(self.whitelist),
            "check_interval_sec": self.check_interval_sec,
            "patrol_inactive_after_sec": self.patrol_inactive_after_sec,
            "message_delay_sec": self.message_delay_sec,
            "min_silence_sec": self.min_silence_sec,
            "cooldown_sec": self.cooldown_sec,
            "max_daily_replies_per_session": self.max_daily_replies_per_session,
            "recent_message_limit": self.recent_message_limit,
            "quiet_hours": self.quiet_hours,
            "enabled_message_trigger": self.enabled_message_trigger,
            "enabled_patrol_trigger": self.enabled_patrol_trigger,
            "generation_timeout_sec": self.generation_timeout_sec,
            "proactive_inherit_tools": self.proactive_inherit_tools,
            "vision_judge_enabled": self.vision_judge_enabled,
            "vision_main_enabled": self.vision_main_enabled,
            "vision_provider_id": self.vision_provider_id,
            "vision_judge_provider_id": self.vision_judge_provider_id,
            "vision_skip_stickers": self.vision_skip_stickers,
            "vision_max_images": self.vision_max_images,
            "vision_image_age_sec": self.vision_image_age_sec,
            "vision_timeout_sec": self.vision_timeout_sec,
        }
