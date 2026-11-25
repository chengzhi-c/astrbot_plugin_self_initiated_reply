"""数据形状、配置规约与无依赖纯函数。

拥有：会话状态与投递结果的数据类、``Settings`` / ``ConfigSpec`` 的配置读写
规约、上限常量、时间与类型转换纯函数、跨模块回调的 ``Protocol`` 形状。

不拥有任何 I/O 与业务判断：落盘属 ``storage``，宿主字段读取属 ``utils``，
是否接话属 ``decision``。本模块是依赖图的叶子（只依赖标准库与宿主 logger），
反向依赖会立刻成环。
"""

from __future__ import annotations

import hashlib
import heapq
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
PLUGIN_VERSION = "1.5.8"
COMMAND_HANDLED_KEY = f"{PLUGIN_ID}:command_handled"
STATE_VERSION = 4

# 配置安全限制：防 OOM、费用滥用与性能降级的硬边界。
MAX_PROMPT_LENGTH = 8000
MAX_WHITELIST_SIZE = 1000
MAX_FALLBACK_PROVIDERS = 8
MAX_STRING_LIST_ITEM_LEN = 200
# str 类键（provider id）的硬上限：与列表条目同宽，防止无限长字符串落盘。
MAX_PROVIDER_ID_LEN = MAX_STRING_LIST_ITEM_LEN
# 与前端 pages/主动回复设置/config-form.mjs 的 WHITELIST_ILLEGAL_RE 同字符集。
# 控制字符 + 引号 + 反斜杠：过长文案截进 logger.warning 时不能伪造日志行。
STRING_LIST_ILLEGAL_RE = re.compile(r"[\x00-\x1f\"'\\]")
# 列表条目 trim 用的字符集 = 设置页 String.prototype.trim() 的那一套（JS
# WhiteSpace + LineTerminator）。Python 的裸 str.strip() 还会剥 U+0085（NEL）
# 等 JS 不剥的字符：页面校验放行的条目会被后端静默改写，保存再加载后内容
# 与用户输入不一致。
_JS_TRIM_CHARS = (
    "\t\n\v\f\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def js_trim(text: str) -> str:
    """按设置页 ``String.prototype.trim()`` 的同一字符集去首尾空白。

    条目规范化（``_normalize_list_item``）与 webapi 的「值是否被调整」判定
    必须共用本函数：后者若用裸 ``str.strip()``，会把 U+0085 等 JS 不剥的
    字符算成改动，弹出与事实不符的「已规范化」提示。
    """
    return text.strip(_JS_TRIM_CHARS)


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
# 指令回显的字符预算。宿主单条消息上限同样不在插件控制内，而回显超限时
# ``event.send`` 抛错、兜底 ``set_result`` 又把同一段超长文本再发一次——管理员
# 一条回执都收不到，``stop_event()`` 却照旧执行，消息被吞。真实可达的超长形状：
# ``/selfreply debug`` 把整条用户消息原文拼进回显；``/selfreply list`` 在
# MAX_WHITELIST_SIZE × MAX_STRING_LIST_ITEM_LEN 的规格上限下约 20 万字符。
MAX_COMMAND_REPLY_CHARS = 2000
MAX_DAILY_REPLIES_LIMIT = 1000
MAX_VISION_IMAGES = 5
MIN_VISION_IMAGE_AGE_SEC = 60  # 短于此的清理窗口会让图片上下文抖动
MAX_VISION_IMAGE_AGE_SEC = 86400
# 巡检不活跃窗口的规格上限。"0 = 不截断"落到事件回收阈值时必须折算成这个
# 有限值：直接 max(1h, 0) 会让"不截断"实际只有 1 小时。
MAX_PATROL_INACTIVE_SEC = 604800
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
# 判断上下文默认补历史阈值；读取条数另由 DECISION_HISTORY_FLOOR 决定。
MIN_RECENT_TEXT_RECORDS = 5
DECISION_HISTORY_FLOOR = 8

# 插件运行常量
MAX_AGENT_STEPS = 15  # 为主 Agent 生成预留步数
MAX_DIRECT_TOOL_SENDS = 2
# 生成超时后留给 run_agent 优雅退出的宽限秒数；terminate 的 stop_timeout
# fallback 必须引用 TERMINATE_TASK_TIMEOUT_SEC，禁止再写字面量。
GRACEFUL_STOP_GRACE_SEC = 3.0
TERMINATE_TASK_TIMEOUT_SEC = 3.0
# 巡检停止窗口。被取消的巡检内联跑着一次会话检查，而后者自带两段
# GRACEFUL_STOP_GRACE_SEC 的收敛等待（generation._graceful_stop 先等优雅停止、
# 再等兜底取消）。套用 3s 的 terminate 窗口会把**合作**任务判成"吞掉取消"并
# 隔离登记，_mark_degraded 随即永久停摆——一次例行 /selfreply off 就够。
# 取三段：两段收敛 + 一段余量（覆盖 decision 收敛与落盘重试）。
PATROL_STOP_TIMEOUT_SEC = GRACEFUL_STOP_GRACE_SEC * 3
# terminate 等待后台任务收敛的窗口，与上面同一 hazard：被等待方自带
# GRACEFUL_STOP_GRACE_SEC 的优雅停止 + 一段记账落盘，套用 TERMINATE_TASK_TIMEOUT_SEC
# 的 3s 会把合作任务判成"吞掉取消"并永久 DEGRADED。
SHUTDOWN_TASK_DRAIN_TIMEOUT_SEC = PATROL_STOP_TIMEOUT_SEC
# 内联指令入口做管理员校验的动作集合（help 任何成员可取）。
ADMIN_COMMAND_ACTIONS = {"status", "list", "add", "remove", "check", "on", "off", "debug"}
# check 在分派中保存目标后自行取消，其余写操作在权限校验通过后取消。
SESSION_CANCEL_COMMAND_ACTIONS = frozenset({"add", "remove", "on", "off"})
# 0.7.x 主动 Agent 工具允许列表：默认空集，未列入的工具在 build/hook 后一律移除，
# 无法验证时终止本次主动运行（fail closed）。
PROACTIVE_ALLOWED_TOOL_IDS: frozenset[str] = frozenset()
# 宿主级危险能力工具 ID（实证于 AstrBot 4.26/4.27 源码）：cron、电脑使用、
# 文件提取、知识库 agentic。无论 proactive_inherit_tools 如何，这些工具在主动
# 运行中一律拒绝；是 build config 硬关闭之外拦截 hook 注入的最终防线。
# 条目必须是宿主 FunctionTool 的精确 name，运行期不做名字匹配；漏删即等于放行。
HOST_DANGEROUS_TOOL_IDS: frozenset[str] = frozenset(
    {
        # 4.23.3 的 multiCommand 使用 future_task 作为工具名。
        "future_task",
        "astrbot_execute_shell",
        "astrbot_execute_ipython",
        "astrbot_execute_python",
        # 4.27.1 新增
        "astrbot_shell_session",
        "astrbot_execute_browser",
        "astrbot_execute_browser_batch",
        "astrbot_run_browser_skill",
        # 4.23.3 的文件工具名。
        "astrbot_upload_file",
        "astrbot_download_file",
        "astrbot_file_read_tool",
        "astrbot_file_write_tool",
        "astrbot_file_edit_tool",
        "astrbot_grep_tool",
        "astr_kb_search",
        # 4.29.4 computer_tools/cua.py：截图 / 鼠标 / 键盘，属「电脑使用」
        "astrbot_cua_screenshot",
        "astrbot_cua_mouse_click",
        "astrbot_cua_keyboard_type",
        # 4.29.4 shipyard_neo/neo_skills.py：技能候选的创建、评测与发布回滚。
        # 与同目录 browser 工具同源（同一 computer-use 运行时），同类一并拒绝。
        "astrbot_get_execution_history",
        "astrbot_annotate_execution",
        "astrbot_create_skill_payload",
        "astrbot_get_skill_payload",
        "astrbot_create_skill_candidate",
        "astrbot_list_skill_candidates",
        "astrbot_evaluate_skill_candidate",
        "astrbot_promote_skill_candidate",
        "astrbot_list_skill_releases",
        "astrbot_rollback_skill_release",
        "astrbot_sync_skill_release",
        # 4.30 astr_agent_runtime subagent tools can launch work or send to
        # other agent sessions; proactive replies must not inherit them.
        "spawn_agent",
        "list_agents",
        "get_agent_result",
        "wait_agent",
        "send_message",
        "cancel_agent",
    }
)


EVENT_CLEANUP_INTERVAL_SEC = 3600
# 事件表条数硬上限。必须显著高于插件支持的白名单规模：超出即按最旧淘汰，
# 而被淘汰的会话在 `_patrol_one_session` 第一道判据（没有事件就跳过）会从此
# 巡检不到，直到该群再来新消息。100 会让一百多个会话的白名单里靠后的群静默失联。
MAX_CACHED_EVENTS = 1200
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
- 默认保守克制：只在接话自然、有信息量时 should_reply=true。
- 群友明确点名 Bot 昵称并要求接话、回复、发表情包、找图、发图时，优先判 true。
  这是群友的直接邀请，除非最后一条消息本身带明显的排斥意味（明确说不要 Bot 接话、
  辱骂、驱赶），否则不该因为"Bot 刚回复过"就拒绝。点名本身就是还要再回一次的理由。
- 其余可以回复的情况（满足其一即可，但必须不打扰当前对话节奏）:
  1. 群友在讨论技术/知识/工具类问题，Bot 能补充一个简短有用的信息、提醒或判断。
  2. 群聊明显冷场，最后一条是开放式提问、评价或吐槽，轻短接一句能自然续上话题。

- 判断时还需考虑以下倾向 should_reply=false 的信号，但它们都是权衡项，不是硬性禁止：
  - 群友间正在密集互动、互问互答、热烈讨论中，插话会显得突兀。
  - 对话明显是针对某个具体人的提问，或群友间的私人话题。
  - 最近消息只是简单附和、表情包刷屏、哈哈哈/嗯/好/草/确实/6 等无实质内容的闲聊。
  - Bot 最近刚回复过，且没有人接着 Bot 的话继续聊。
  - 纯主观/个人话题（八卦、情感、个人生活细节），Bot 没有立场也没有价值。
  - 最后一条消息是自洽的陈述或结论，没有留下接话的自然入口。
- 特别注意：
  - “不好说吧”“怎么说呢”“我说真的”“别说了”等普通句子不算要求 Bot 接话。
  - 只有明确出现 Bot 昵称并要求 Bot 说话/回复/发图/发表情包，才算“点名 Bot 接话”。
  - 对方点名要求回复时，"Bot 刚回复过""对方可能在测试"都不构成拒绝理由。
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
PRINTABLE_CHAR_MIN = 32
# 孤立代理项（U+D800–U+DFFF）：能进内存，但 UTF-8 编码必失败。一旦落进配置，
# config_revision 与 write_json_atomic 都会抛，设置页从此读不出配置。
_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF
# 用 str.translate 批量过滤，减少配置解析占用事件循环的时间。
_SURROGATE_CHARS = {code: None for code in range(_SURROGATE_MIN, _SURROGATE_MAX + 1)}
_UNPRINTABLE_CHARS = _SURROGATE_CHARS | {code: None for code in range(PRINTABLE_CHAR_MIN)}

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


# 布尔键的已知文本取值。分成两张表的理由不是解析，而是诊断：落在两张表之外
# 的文本既不是「开」也不是「关」，是写错了，必须留痕（手改配置打错字会让
# enabled 静默变 false，插件看着像坏了）。
BOOL_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enable", "enabled", "启用", "开启", "是"})
BOOL_FALSE_VALUES = frozenset(
    {"0", "false", "no", "off", "disable", "disabled", "禁用", "关闭", "否"}
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
    return str(value).strip().lower() in BOOL_TRUE_VALUES


def as_int(value: Any, default: int, minimum: int = 0, maximum: int = 100000) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(maximum, parsed))


# 宿主 provider 请求的默认超时（astrbot openai_source: provider_config.get("timeout", 120)）。
# 主模型超时后宿主才会切备用模型，所以主动生成的总预算必须大于「这个超时 + 切模型耗时」，
# 否则预算永远先到期，备用模型还没来得及被试就已经被放弃——用户看到的就是一句都不回。
_PROVIDER_DEFAULT_TIMEOUT_SEC = 120


def _coerce_provider_timeout(raw: Any) -> float:
    """配置里的超时可能是字符串；缺失、解析不了、非正数都按宿主默认值算。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(_PROVIDER_DEFAULT_TIMEOUT_SEC)
    if not math.isfinite(value) or value <= 0:
        return float(_PROVIDER_DEFAULT_TIMEOUT_SEC)
    return value


def provider_timeouts(providers: Any) -> list[float]:
    """从宿主 provider 列表里取各自的请求超时（取不到就用宿主默认值）。"""
    timeouts: list[float] = []
    try:
        items = providers.values() if isinstance(providers, dict) else providers
        for provider in items or ():
            config = getattr(provider, "provider_config", None)
            raw: Any = config.get("timeout") if isinstance(config, dict) else None
            timeouts.append(_coerce_provider_timeout(raw))
    except Exception:
        return []
    return timeouts


def chain_timeout_budget(providers: Any, *, request_attempts: int = 1) -> float | None:
    """一条候选链中所有请求尝试都跑满超时所需的总时长。

    宿主是串行试的：主模型超时之后才开始下一个，每个都可能耗满自己的超时。
    请求尝试数包含首次请求；这里不含宿主的重试等待间隔。
    """
    timeouts = provider_timeouts(providers)
    return sum(timeouts) * max(1, request_attempts) if timeouts else None


def proactive_build_config_kwargs(
    provider_settings: dict[str, Any] | None = None,
    *,
    model_settings: dict[str, Any] | None = None,
    persona_settings: dict[str, Any] | None = None,
    tool_schema_mode: str = "full",
    add_cron_tools: bool = False,
    request_max_retries: int = 1,
) -> dict[str, Any]:
    """主动运行传给宿主 ``MainAgentBuildConfig`` 的键集合（唯一来源）。

    ``generation.main_agent_build_config`` 与 ``runtime_adapter`` 的加载期探针
    共用：探针用同一表达式试实例化，宿主不接受某个键时**加载期**即红。缺少它
    时契约只核验 build/run 的签名，插件装得上、每次主动生成都在运行期
    TypeError（一条回复都产不出，且只在真实宿主上可见）。
    """
    settings = provider_settings or {}
    model = model_settings or {}
    persona = persona_settings or {}
    fallback_provider_ids = model.get("fallback_provider_ids", [])
    # Safe Mode 的默认值跟普通人格一致：宿主在普通回复里从人格配置取它
    # （4.29.4 astr_agent_tool_exec.py:609、4.30 main_agent/config.py:128），
    # 而主动回复带显式 ``req`` 走的是宿主不代读的路径。写死 True 时，人格关掉
    # Safe Mode 的用户会看到主动回复多出一段 Safe Mode 系统提示：system 前缀
    # 与普通回复不同，缓存无法复用，两条路径的行为也不一致。
    safety_default = as_bool(persona.get("safety_mode"), True)
    strategy_default = str(persona.get("safety_mode_strategy") or "system_prompt")
    return {
        # 夹取必须能防抛：provider_settings 是宿主配置里可手改的外部输入，
        # "60s" 之类的非数字串若在 try 之外 int() 抛出，会让所有主动生成
        # 整体失败，"拿不到会话配置就退化到宿主默认"的承诺随之失效。
        "tool_call_timeout": as_int(
            settings.get("tool_call_timeout") or 60, 60, minimum=1, maximum=86400
        ),
        "tool_schema_mode": tool_schema_mode,
        "provider_wake_prefix": "",
        "streaming_response": False,
        "sanitize_context_by_modalities": bool(
            settings.get("sanitize_context_by_modalities", False)
        ),
        "kb_agentic_mode": False,
        "file_extract_enabled": False,
        "llm_safety_mode": as_bool(settings.get("llm_safety_mode"), safety_default),
        "safety_mode_strategy": str(settings.get("safety_mode_strategy") or strategy_default),
        "computer_use_runtime": "none",
        "add_cron_tools": add_cron_tools,
        # 4.27.2 从 provider_settings 读取这两个键，4.23.3 不读取重试次数；
        # 4.29.4 改读构造字段。两处都写以兼容支持它们的宿主版本。
        "provider_settings": {
            **settings,
            "request_max_retries": as_int(request_max_retries, 1, minimum=1, maximum=10),
        },
        # 回退链必须透传：宿主 runner 的候选集是 ``[provider, *fallback_providers]``，
        # 该键缺省为空列表时只剩单个候选，任何异常或错误响应直接终止而不换模型。
        # 非 list 交给宿主的既有校验（它会记 warning 并跳过）。
        "fallback_provider_ids": (
            fallback_provider_ids if isinstance(fallback_provider_ids, list) else []
        ),
        # 主动回复用自己的请求尝试次数：宿主默认 5 次会把 APITimeoutError 重试四遍，
        # 单模型就能耗到 600 秒，备用模型永远排不上。
        "request_max_retries": as_int(request_max_retries, 1, minimum=1, maximum=10),
    }


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
            line = "".join(char for char in line if ord(char) >= PRINTABLE_CHAR_MIN)
            line = INLINE_SPACE_PATTERN.sub(" ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    text = "".join(char for char in text if ord(char) >= PRINTABLE_CHAR_MIN)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


@dataclass
class MessageRecord:
    role: str
    name: str
    text: str
    at: float = field(default_factory=now_ts)


# 取不到发送者名字时的兜底显示名：提示词历史行与事件侧必须同一称呼。
FALLBACK_SENDER_NAME = "用户"

# 助手在提示词历史里的固定称呼。单独命名是因为它同时是"用户昵称不得冒充"的
# 比较基准（见 history_display_name），散成字面量会让这条约束无从查找。
ASSISTANT_HISTORY_NAME = "Bot"


def history_display_name(role: str, name: str | None = None) -> str:
    """历史展示名：助手固定 Bot，其他人用名字，缺名才回落用户。"""
    if role == "assistant":
        return ASSISTANT_HISTORY_NAME
    # 单行化标签：昵称里的换行会让用户行在**下一行**渲染出与助手轮次同形的
    # "Bot: ..."（"小明\nBot"），只比较整串是否等于 Bot 时看不出来。
    label = WHITESPACE_PATTERN.sub(" ", str(name or FALLBACK_SENDER_NAME)).strip()
    # 群成员可自选昵称。取成助手称呼时，他那一行与真正的助手轮次完全同形
    # （``Bot: <他写的话>``），提示词注入面随之从"聊天内容"扩大到"助手身份"——
    # 判断与生成两条路径都吃这个标签。撞上就改写成带兜底称呼的形态，让任何
    # 用户行都不可能渲染成 ``Bot:`` 开头。
    #
    # 只在真撞上时改写：其余会话的提示词逐字节不变，判断模型已形成的行为不受
    # 影响。比较用 strip 后的值，``"Bot "`` 这类带空白的昵称同样算撞上。
    if label.casefold() == ASSISTANT_HISTORY_NAME.casefold() or label.casefold().startswith(
        f"{ASSISTANT_HISTORY_NAME}:".casefold()
    ):
        return f"{FALLBACK_SENDER_NAME}({label})"
    return label


class ReadHistoryCallback(Protocol):
    """宿主历史读取回调（limit 关键字调用）。"""

    def __call__(self, umo: str, *, limit: int) -> Awaitable[list[MessageRecord]]: ...


class ImageContextCallback(Protocol):
    """Vision 描述上下文回调；before 限定本轮可见的图片活动时间。"""

    def __call__(
        self, umo: str, *, enabled: bool, provider_id: str, before: float | None = None
    ) -> Awaitable[str]: ...


class LocalGateCallback(Protocol):
    """局部闸门回调（state 位置 + force 关键字调用，返回跳过原因或空串）。"""

    def __call__(
        self,
        state: SessionState,
        *,
        force: bool,
    ) -> str: ...


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
    调用方要区分的成因放这里，``detail`` 只进日志。
    """

    STOPPING = "stopping"
    GENERATION_CHANGED = "generation_changed"
    BUDGET = "budget"
    GATE_REJECTED = "gate_rejected"
    HOOK_STOPPED = "hook_stopped"


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
        """Apply an adapter outcome, returning false for a sealed late result.

        Seal 之后证据冻结：seal() 已把在途一律悲观记 UNKNOWN，迟到结论一律丢弃，
        否则封账后的 derived 证据（额度/去重基准）会被改写，与已 apply 的状态不一致。
        """
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
    ) -> float:
        """距最后一次活动的剩余静默；从未活跃按 0 计。"""
        if not self.last_active_at:
            return 0.0
        return max(0.0, min_silence_sec - (now - self.last_active_at))

    def age_sec(self, now: float) -> float:
        """距上次活跃的经过秒数（从未活跃按 0 计）。"""
        return now - self.last_active_at if self.last_active_at else 0.0


@dataclass(frozen=True)
class ConfigSpec:
    """单个配置键的完整规格。

    同一个键散在多处声明时，漏一处就静默失效（面板上能改、保存返回成功、
    值不生效）。本表驱动 ``Settings`` 字段表、``from_config``、
    ``to_config_dict``、``_parse_config_updates`` 与 ``_AUDITED_CONFIG_KEYS``；
    ``_conf_schema.json`` 保留独立文件（承载 UI 文案），两处必须同步改。表只收
    机器可校验的语义，UI 拷贝不进表。

    字段语义：
        key: 配置键名（= schema 键名）。
        kind: ``bool`` / ``int`` / ``float`` / ``str`` / ``enum`` / ``text`` / ``list``。
        default: 缺键时的默认值，须与 schema 的 ``default`` 一致。
        minimum/maximum: 数值夹取边界，须与 schema ``slider`` 的 min/max 一致。
        step: slider 步长，须与 schema 与设置页控件一致。
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
        MIN_RECENT_TEXT_RECORDS,
        # 下限 0 是合法语义：min_text_records=0 表示「本地有多少用多少、
        # 不回宿主补历史」。提示词读取条数另由 DECISION_HISTORY_FLOOR 兜底，
        # 两者是不同维度，不要把下限抬到 FLOOR。
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
    ConfigSpec("patrol_inactive_after_sec", "int", 1800, 0, MAX_PATROL_INACTIVE_SEC, step=3600),
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
    ConfigSpec("generation_timeout_sec", "float", 300.0, 1, 600, step=1),
    ConfigSpec(
        "proactive_provider_id",
        "str",
        "",
        special="select_provider",
        max_len=MAX_PROVIDER_ID_LEN,
        surfaces=_PANEL,
    ),
    ConfigSpec("proactive_request_max_retries", "int", 1, 1, 10, step=1, surfaces=_PANEL),
    ConfigSpec(
        "proactive_fallback_provider_ids",
        "list",
        [],
        container="list",
        max_items=MAX_FALLBACK_PROVIDERS,
        item_max_len=MAX_STRING_LIST_ITEM_LEN,
        item_pattern=STRING_LIST_ILLEGAL_RE.pattern,
        surfaces=_PANEL,
    ),
    ConfigSpec("proactive_inherit_tools", "bool", False, audited=True, surfaces=_PANEL),
    ConfigSpec("proactive_preserve_tool_schema", "bool", False, audited=True, surfaces=_PANEL),
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
        "vision_timeout_sec", "float", 20.0, 1, MAX_VISION_TIMEOUT_SEC, step=1, surfaces=_PANEL
    ),
)

CONFIG_SPEC_BY_KEY: dict[str, ConfigSpec] = {spec.key: spec for spec in CONFIG_SPECS}


def panel_config_specs() -> tuple[ConfigSpec, ...]:
    """Specs exposed on the custom settings page and GET /config."""
    return tuple(spec for spec in CONFIG_SPECS if "panel" in spec.surfaces)


# 设置页 textarea 的分隔符（pages/主动回复设置/config-form.mjs 的
# parseWhitelist 同一表达式）：页面把「a,b」当两条展示与保存，后端 api 模式
# 必须同口径切分，否则一个会话被拆成两个、计数与磁盘三方不一致。
_LIST_SEPARATOR_PATTERN = re.compile(r"[\n,，]+")


def _list_items(raw: Any, *, split_items: bool = False) -> list[Any]:
    if isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
        if not split_items:
            return items
        expanded: list[Any] = []
        for item in items:
            if isinstance(item, str):
                expanded.extend(_LIST_SEPARATOR_PATTERN.split(item))
            else:
                expanded.append(item)
        return expanded
    if isinstance(raw, str):
        return _LIST_SEPARATOR_PATTERN.split(raw)
    raise ValueError("配置列表类型无效")


def _normalize_list_item(spec: ConfigSpec, raw: Any, mode: str) -> tuple[str | None, int, int]:
    """Normalize one list item and return ``(value, dropped, adjusted)``."""
    if not isinstance(raw, str):
        # 非字符串条目一律不当成活条目：str(None) 会变成 "None" 持久化进
        # 白名单/别名，而集合比较通过、UI 无感知。api 模式拒收（400），
        # disk 模式对不可信落盘数据保持宽松：丢弃。
        if mode == "api":
            raise ValueError(f"{spec.key} 条目必须是字符串")
        return None, 1, 0
    text = js_trim(raw)
    # 长度上限前置：下面的编码检查与正则要扫全文，单条 2MB 就能在事件循环上停
    # 近 1s。api 模式超长一律 400，与后面那条同一口径；判定用 trim 后的长度，
    # 与后面那条一致（前后空白不算超长）。
    if mode == "api" and spec.item_max_len is not None and len(text) > spec.item_max_len:
        raise ValueError(f"{spec.key} 条目过长")
    sanitized = 0
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # 全量判定要过 10 万条时，逐字符 is_utf8_encodable 是 Python 级循环，
        # 会成为主耗时；encode 在 C 层拒绝同一批字符（孤立代理项），判定等价。
        if mode == "api":
            raise ValueError(f"{spec.key} 条目含非法字符") from None
        # 孤立代理项能进内存但编不了 UTF-8：留着会让整份配置摘要与落盘一起失败。
        text = js_trim("".join(char for char in text if is_utf8_encodable(char)))
        sanitized = 1
    if not text:
        # 空条目一律丢弃：白名单与别名列表都无意义。
        return None, 1, 0
    if spec.item_pattern and re.search(spec.item_pattern, text):
        if mode == "api":
            raise ValueError(f"{spec.key} 条目含非法字符")
        return None, 1, 0
    if spec.item_max_len is not None and len(text) > spec.item_max_len:
        # api 模式已在入口按 len(js_trim(raw)) 拒绝，这里只剩 disk 的宽松截断。
        return text[: spec.item_max_len].rstrip(_JS_TRIM_CHARS), 0, 1
    return text, 0, sanitized


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
    # 全量判定：容量不能前移成「只校验前 max_items 条」。尾部非法项（None、超长、
    # 非法字符）必须照样被拒，否则截断自己就把违规项吞了。选择留给下面的有界
    # nsmallest，规模代价因此不随条目数退化成全量排序。
    for item in _list_items(raw, split_items=mode == "api"):
        text, item_dropped, item_adjusted = _normalize_list_item(spec, item, mode)
        dropped += item_dropped
        adjusted += item_adjusted
        if text is not None:
            items.append(text)

    if spec.key == "proactive_fallback_provider_ids":
        unique_items = list(dict.fromkeys(items))
        dropped += len(items) - len(unique_items)
        items = unique_items

    if spec.container == "set":
        # 先去重并按 canonical 排序，再执行容量上限——否则提交排列会
        # 决定实际保留值。只取前 N 个用有界选择，避免为容量上限全量排序。
        unique_values = set(items)
        dropped += len(items) - len(unique_values)
        if spec.max_items is not None and len(unique_values) > spec.max_items:
            dropped += len(unique_values) - spec.max_items
            items = heapq.nsmallest(spec.max_items, unique_values)
        else:
            items = sorted(unique_values)
    elif spec.max_items is not None and len(items) > spec.max_items:
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
        "[%s] %s too long (%d chars), truncated to %d chars",
        PLUGIN_ID,
        spec.key,
        len(text),
        spec.max_len,
    )
    return text[: spec.max_len].rstrip()


def is_utf8_encodable(char: str) -> bool:
    """单字符能否编成 UTF-8（排除孤立代理项）。"""
    return not (_SURROGATE_MIN <= ord(char) <= _SURROGATE_MAX)


def _warn_damaged_variant(spec: ConfigSpec, raw: Any) -> None:
    """类型损坏的配置值：降级到 fallback，但必须留痕。

    判据是「静默降级但用户可感知」：enabled: [] 被静默判成 False
    等于关掉整个插件，而首启规范化随后会把原始损坏值直接覆盖掉，事后无从
    定位。只记键名与类型，不记值本身。
    """
    logger.warning(
        "[%s] %s has a damaged value type (%s); using the fallback value",
        PLUGIN_ID,
        spec.key,
        type(raw).__name__,
    )


def coerce_config_value(spec: ConfigSpec, raw: Any, fallback: Any) -> Any:
    """按规格把一个原始配置值强制成目标类型并夹取边界。

    ``fallback`` 与 ``raw`` 分开传：旧键回退时强制失败要落回同一个旧键的值
    而非静态默认（``vision_enabled`` 迁移到两个新开关的语义）。截断是防
    OOM 与 token 滥用的硬边界，静默生效但必须留 warning。
    """
    if spec.kind == "bool":
        # 显式 null 是合法形状（回规格默认），不算损坏：漏掉这个守卫会让每个空值
        # 布尔键每次加载各刷一条假告警，把告警通道淹掉。
        if raw is not None and not isinstance(raw, (bool, int, float, str)):
            # as_bool 对容器/对象一律 False：那是「损坏」而不是「关掉」，
            # 必须沿 legacy fallback 读取。
            _warn_damaged_variant(spec, raw)
            return bool(fallback)
        if (
            isinstance(raw, str)
            and raw.strip()
            and raw.strip().lower() not in BOOL_TRUE_VALUES | BOOL_FALSE_VALUES
        ):
            # 已知取值之外的文本按「关」处理（as_bool 的既有口径），但要留痕：
            # 否则 enabled: "ture" 会让插件静默停用，日志里一个字都没有。
            _warn_damaged_variant(spec, raw)
        return as_bool(raw, bool(fallback))
    if spec.kind == "int":
        if spec.minimum is None or spec.maximum is None:
            raise RuntimeError(f"{spec.key}: int spec missing minimum/maximum")
        # bool 是 int 的子类：as_int 拒收它（真值落到 default），谓词必须一起拒，
        # 否则 true 写进整数键会静默回默认值、留不下任何线索。
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float, str))):
            _warn_damaged_variant(spec, raw)
            raw = fallback
        return as_int(raw, int(fallback), int(spec.minimum), int(spec.maximum))
    if spec.kind == "float":
        if spec.minimum is None or spec.maximum is None:
            raise RuntimeError(f"{spec.key}: float spec missing minimum/maximum")
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, (int, float, str))):
            _warn_damaged_variant(spec, raw)
            raw = fallback
        return as_float(raw, float(fallback), float(spec.minimum), float(spec.maximum))
    if spec.kind == "enum":
        if raw is not None and not isinstance(raw, str):
            _warn_damaged_variant(spec, raw)
            raw = fallback
        return choice(raw, set(spec.options), str(fallback))
    if spec.kind == "text":
        if raw is not None and not isinstance(raw, str):
            # 容器被 str() 会落盘成 Python repr（"['x']"）并进提示词。
            _warn_damaged_variant(spec, raw)
            raw = fallback
        # 空值回落默认模板（面板留空即复位）：唯一实现点在读侧，复位值单源
        # 于规格表 reset_default。
        text = str(raw or "").translate(_SURROGATE_CHARS).strip()
        return _truncate_text(spec, text or spec.reset_value)
    if spec.kind == "list":
        try:
            return normalize_string_list(spec, raw, mode="disk")
        except ValueError:
            # 只有 raw 类型非法才走到这里；disk 模式对合法 list 不抛。
            logger.warning("[%s] %s list value invalid; using fallback", PLUGIN_ID, spec.key)
            return normalize_string_list(spec, fallback, mode="disk")
    if spec.kind == "str":
        if raw is not None and not isinstance(raw, str):
            _warn_damaged_variant(spec, raw)
            raw = fallback
        # 单行标识（三个 provider id）剔除控制字符：它们经 %s 进日志，带换行就能
        # 伪造出一行假日志。与列表条目同一条规则（STRING_LIST_ILLEGAL_RE 排除
        # C0 控制字符）。孤立代理项一并剔除：留着会让落盘与配置摘要一起失败。
        text = str(raw or "").strip().translate(_UNPRINTABLE_CHARS)
        return _truncate_text(spec, text).strip()
    # 规格表写错 kind 不得静默降级：那会让 int/bool 键带着非法值落盘并显示成
    # 正常值。与上面缺边界的自检同口径，加载期响。
    raise RuntimeError(f"{spec.key}: 未知配置 kind {spec.kind!r}")


def read_config_value(spec: ConfigSpec, config: Any) -> Any:
    """从宿主配置对象读一个键：正式键优先，缺失时按旧键顺序回退。

    只强制转换一次：把旧键值 coerce 成 fallback 再二次 coerce 会让同一份值
    走两遍边界与截断。``fallback`` 的语义是「``raw`` 强制失败时落回哪个值」：
    正式键存在时落回旧键的值而非静态默认。
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
    proactive_provider_id: str
    proactive_request_max_retries: int
    proactive_fallback_provider_ids: list[str]
    proactive_inherit_tools: bool
    proactive_preserve_tool_schema: bool
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

        判断阶段触发频率高，允许单独指定更便宜的识图模型；留空跟随判断模型
        本身（``judge_provider_id``），后者留空则由 adapter 回落当前会话模型。
        """
        return self.vision_judge_provider_id or self.judge_provider_id

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
        default_prompt = CONFIG_SPEC_BY_KEY["decision_prompt_template"].reset_value
        return self.decision_prompt_template != default_prompt

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
    """Hash normalized persistent config; Settings serializes sets before this boundary."""
    payload = config.to_config_dict() if isinstance(config, Settings) else dict(config)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
