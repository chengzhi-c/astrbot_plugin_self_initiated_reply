"""会话级"是否接话"裁决。

只负责裁决时序与闸门：判断模型调用（超时/失败分类）、判断提示词构建与
注入清理、局部闸门判定（免打扰/日配额/静默/冷却/观察窗口）。模型解析/
生成、历史读取、Vision 描述经注入回调执行，可脱离插件实例独立单测。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from .models import (
    CONFIG_SPEC_BY_KEY,
    CONTEXT_CAP_MARKER,
    DECISION_JSON_CONTRACT,
    PLUGIN_ID,
    CheckTrigger,
    ImageContextCallback,
    ReadHistoryCallback,
    SessionState,
    Settings,
    duration,
    now_ts,
    sanitize_prompt_variable,
)
from .utils import (
    build_history_text,
    cap_context_text,
    latest_user_text,
    parse_decision_json,
    redact_exc_text,
    response_text,
)

# 放弃 provider 任务后给它的收敛宽限：宿主 SDK 吞掉 CancelledError 时留下的
# 才是孤儿。与生成路径 GRACEFUL_STOP_GRACE_SEC 同值但语义独立，可各自调整。
DECISION_CONVERGE_GRACE_SEC = 3.0

DECISION_SYSTEM_PROMPT = "你是群聊主动回复时机判断器。只输出严格 JSON，不要输出解释。"
# 裁决只输出短 JSON，120 token 足够且把判断调用成本封顶。
DECISION_MAX_TOKENS = 120
# 判断上下文（多行聊天记录）的字符预算：判断只需回答"此刻该不该接"，
# 输入越短越省越快。超预算时保尾：越新的消息越重要。
MAX_DECISION_CONTEXT_CHARS = 2000
# 引用决定的可选输出约定，只在 quote_mode=model 时追加：判断模型是高频调用，
# 不用引用的用户不该多背一个输出字段。
QUOTE_DECISION_HINT = (
    "另请在 JSON 中给出可选字段 quote：true 表示这句回复应该引用最后一条消息"
    "（例如在回应某个人的具体问题、对话已往下走了几句、或需要点明在接谁的话时），"
    "false 表示不必引用。"
)
# 免打扰时段的时/分上下界（HH:MM 解析后的合法性校验）。
_MAX_QUIET_HOUR = 23
_MAX_QUIET_MINUTE = 59
# 判断路径读宿主历史的下限：默认提示词明示「优先参考最近至少 8 条」，读少于
# 8 条会让模型反复回宿主补历史。与生成路径的 MIN_RECENT_TEXT_RECORDS 刻意
# 不同源：8 条是提示词契约，生成阈值跟着用户的缓存上限走。
DECISION_HISTORY_FLOOR = 8


def _localtime_minutes() -> int:
    now = time.localtime()
    return now.tm_hour * 60 + now.tm_min


class DecisionMaker:
    """ "是否接话"裁决：闸门判定、提示词构建与模型调用。"""

    def __init__(
        self,
        *,
        settings: Settings,
        clock: Callable[[], float] = now_ts,
        minutes_now: Callable[[], int] = _localtime_minutes,
        resolve_provider: Callable[[str], Awaitable[str]],
        llm_generate: Callable[[str, str], Awaitable[Any]],
        read_history: ReadHistoryCallback,
        build_image_context: ImageContextCallback,
        quarantine_task: Callable[[asyncio.Task[Any], str], None] | None = None,
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._minutes_now = minutes_now
        # 超时后仍未收敛的 provider 任务交给生命周期的隔离登记（可选注入：
        # 测试与不关心生命周期的调用方可不传，此时退化为记 WARNING）。
        self._quarantine_task = quarantine_task
        self._resolve_provider = resolve_provider
        self._llm_generate = llm_generate
        self._read_history = read_history
        self._build_image_context = build_image_context
        self._invalid_quiet_hours_logged: set[str] = set()

    async def _converge_provider_task(self, task: asyncio.Task[Any], reason: str) -> None:
        """收敛一个超时/被取消的 provider 任务：取消 → 宽限 → 未退才隔离登记。

        ``cancel()`` 是异步投递的，刚调用时 ``task.done()`` 必然为 False，
        故照 ``generation._graceful_stop`` 的形状给一个宽限窗口，只有宽限
        耗尽仍未收敛（宿主 provider 吞掉 CancelledError）才交隔离登记。
        """
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=DECISION_CONVERGE_GRACE_SEC)
        if done or task.done():
            return
        if self._quarantine_task is not None:
            self._quarantine_task(task, reason)
            return
        logger.warning(
            "[%s] provider task ignored cancellation and is unregistered: %s",
            PLUGIN_ID,
            reason,
        )

    # ------------------------------------------------------------------
    # 局部闸门判定（发送前重查；force 跳过全部闸门）
    # ------------------------------------------------------------------

    def local_gate(
        self,
        state: SessionState,
        *,
        force: bool,
        silence_active_at: float | None = None,
    ) -> str:
        if force:
            return ""
        if self.in_quiet_hours():
            return "免打扰时段。"
        if self.settings.max_daily_replies_per_session and (
            state.daily_count >= self.settings.max_daily_replies_per_session
        ):
            return "今日主动回复次数已达上限。"
        active_for_silence = (
            state.last_active_at if silence_active_at is None else silence_active_at
        )
        if not active_for_silence:
            # 从未活跃与"静默中"是两回事：无活动记录连判定基线都没有，
            # 沿用"静默不足"文案会误导运营以为配置没生效。
            return "会话暂无活动记录，无法判断静默。"
        silence_left = state.remaining_silence_sec(
            self.settings.min_silence_sec, self._clock(), active_at=active_for_silence
        )
        if silence_left > 0:
            # max(0, ...)：载入时时间戳被钳到 now + MAX_CLOCK_SKEW_SEC，
            # 差值可能为负，不能向运营者显示负的剩余静默。
            elapsed = max(0, int(self.settings.min_silence_sec - silence_left))
            return f"静默时间不足：{elapsed}s / {self.settings.min_silence_sec}s。"
        cooldown_left = self.settings.cooldown_sec - (self._clock() - state.last_proactive_at)
        if cooldown_left >= 1:
            # 不足整秒直接放行：亚秒剩余在文案上等于「还剩 0s」，语义矛盾。
            return f"冷却中：还剩 {duration(cooldown_left)}。"
        if state.last_proactive_observed_at >= state.last_active_at:
            return "这条消息之后已经主动回复过。"
        return ""

    def in_quiet_hours(self) -> bool:
        current = self._minutes_now()
        for item in self.settings.quiet_hours:
            parsed = self.parse_quiet_hour(item)
            if parsed is None:
                continue
            begin, finish = parsed
            if (begin <= finish and begin <= current <= finish) or (
                begin > finish and (current >= begin or current <= finish)
            ):
                return True
        return False

    def parse_quiet_hour(self, item: str) -> tuple[int, int] | None:
        raw = str(item or "").strip()
        match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", raw)
        if not match:
            self._warn_invalid_quiet_hour(raw)
            return None
        sh, sm, eh, em = (int(part) for part in match.groups())
        if (
            sh > _MAX_QUIET_HOUR
            or eh > _MAX_QUIET_HOUR
            or sm > _MAX_QUIET_MINUTE
            or em > _MAX_QUIET_MINUTE
        ):
            self._warn_invalid_quiet_hour(raw)
            return None
        return sh * 60 + sm, eh * 60 + em

    def _warn_invalid_quiet_hour(self, item: str) -> None:
        key = item or "<empty>"
        if key in self._invalid_quiet_hours_logged:
            return
        self._invalid_quiet_hours_logged.add(key)
        logger.warning("[%s] invalid quiet_hours item ignored: %s", PLUGIN_ID, key)

    # ------------------------------------------------------------------
    # 裁决入口
    # ------------------------------------------------------------------

    async def decide(
        self,
        umo: str,
        state: SessionState,
        *,
        trigger: str,
        force: bool,
    ) -> dict[str, Any] | str:
        """产生一次判断：通过返回 decision dict，早退返回跳过原因。"""
        decision: dict[str, Any]
        if force:
            decision = {"should_reply": True, "reason": "手动强制检查", "elapsed_sec": 0.0}
        else:
            decision = await self.ask_decision_model(umo, state, trigger=trigger)
        if not decision.get("should_reply"):
            return f"判断不回复：{decision.get('reason') or '未说明'}"
        # quote 是可选字段：只有真被模型裁决过的路径才带值，强制路径缺省
        # None =「模型没说」，由投递侧按 quote_mode 兜底。
        decision.setdefault("quote", None)
        return decision

    # ------------------------------------------------------------------
    # 判断模型调用（超时/失败分类；文案冻结于行为契约 §7）
    # ------------------------------------------------------------------

    async def ask_decision_model(
        self, umo: str, state: SessionState, *, trigger: str
    ) -> dict[str, Any]:
        """问判断模型「这轮该不该主动接话」，返回 should_reply / reason / elapsed_sec。

        判断模型关闭时按触发源分流：``patrol`` 放行（巡检本身即意图），其余拒绝
        （不打扰普通消息）。

        失败时一律 fail-closed 返回 ``should_reply=False``，并用 reason 区分五种
        原因，便于 /status 面板归因：provider 解析失败（业务故障，与"未找到"分开
        以免误导运维）、未找到可用模型、超时、模型异常、返回非法 JSON。
        每条出口都带 elapsed_sec，异常路径也不例外。
        """
        started = self._clock()
        if not self.settings.decision_model_enabled:
            if trigger == CheckTrigger.PATROL:
                return {
                    "should_reply": True,
                    "reason": "判断模型关闭，后台巡检触发",
                    "elapsed_sec": 0.0,
                }
            return {
                "should_reply": False,
                "reason": "判断模型关闭，非巡检触发拒绝",
                "elapsed_sec": 0.0,
            }
        provider_id = ""
        try:
            provider_id = await self._resolve_provider(umo)
        except Exception as exc:
            # provider 解析链路的业务故障单独分类，避免被当作"未找到"误导运维。
            logger.error(
                "[%s] resolve decision provider failed: %s",
                PLUGIN_ID,
                redact_exc_text(exc),
            )
            return {
                "should_reply": False,
                "reason": "判断模型解析失败",
                "elapsed_sec": self._clock() - started,
            }
        if not provider_id:
            return {
                "should_reply": False,
                "reason": "未找到可用判断模型",
                "elapsed_sec": self._clock() - started,
            }
        prompt = await self.build_decision_prompt(umo, state, trigger)
        response: Any = None
        # 超时用 asyncio.wait 而非 wait_for：wait_for 会取消内层并**等它真正
        # 结束**，宿主 provider 若吞掉 CancelledError，判断任务永不返回 →
        # 会话锁与运行标记永不释放。wait 只等待、不含取消语义，未收敛的任务
        # 交隔离登记（同款取舍见 generation._graceful_stop）。
        task = asyncio.ensure_future(self._llm_generate(provider_id, prompt))
        try:
            done, _ = await asyncio.wait({task}, timeout=self.settings.decision_timeout_sec)
            if not done:
                await self._converge_provider_task(task, "decision model timeout")
                return {
                    "should_reply": False,
                    "reason": "判断模型超时",
                    "elapsed_sec": self._clock() - started,
                }
            response = task.result()
        except asyncio.CancelledError:
            # wait 不把调用方的取消传给内层，必须自己传。
            await self._converge_provider_task(task, "decision cancelled")
            raise
        except Exception as exc:
            # 异常文本常带请求 URL（含凭证），reason 会经 GET /status 出网，
            # 与日志共用同一脱敏口径。
            detail = redact_exc_text(exc)
            logger.warning("[%s] decision model failed: %s", PLUGIN_ID, detail)
            return {
                "should_reply": False,
                "reason": f"判断模型异常：{detail}",
                "elapsed_sec": self._clock() - started,
            }

        raw = response_text(response)

        # 严格 JSON 解析器，带类型校验
        parsed = parse_decision_json(raw)
        if parsed is None:
            return {
                "should_reply": False,
                "reason": "判断模型未返回有效 JSON",
                "elapsed_sec": self._clock() - started,
            }
        return {
            "should_reply": parsed["should_reply"],
            "reason": parsed["reason"],
            "quote": parsed["quote"],
            "elapsed_sec": self._clock() - started,
        }

    # ------------------------------------------------------------------
    # 提示词构建与注入清理（不可信用户内容不能改变任务边界）
    # ------------------------------------------------------------------

    async def build_decision_prompt(self, umo: str, state: SessionState, trigger: str) -> str:
        aliases = "、".join(self.settings.bot_aliases) or "未配置"
        recent = await self.build_recent_messages(
            umo,
            state,
            # 下限见 DECISION_HISTORY_FLOOR 的取值理由；配置高于下限时尊重配置。
            limit=max(DECISION_HISTORY_FLOOR, self.settings.decision_history_min_messages),
        )
        image_context = await self._build_image_context(
            umo,
            enabled=self.settings.vision_judge_enabled,
            provider_id=self.settings.vision_judge_provider_resolved,
        )
        if image_context:
            recent = f"{recent}\n\n{image_context}" if recent else image_context
        latest = latest_user_text(list(state.recent))

        # 清理所有用户输入变量，防止提示词注入
        values = {
            "session": sanitize_prompt_variable(umo, max_length=200),
            "trigger": sanitize_prompt_variable(trigger, max_length=50),
            "bot_aliases": sanitize_prompt_variable(aliases, max_length=200),
            "last_message_age_sec": str(int(state.age_sec(self._clock()))),
            "last_reply_age_sec": str(
                int(self._clock() - state.last_proactive_at) if state.last_proactive_at else -1
            ),
            "latest_message": sanitize_prompt_variable(latest, max_length=500),
            # recent_messages 是多行聊天记录：保留换行才能区分发言人与轮次；
            # 超预算时保尾（不能用 sanitize 自带的保头截断，那会先丢最新几条，
            # 与模板「优先参考最近至少 8 条」的要求相反）。
            "recent_messages": cap_context_text(
                sanitize_prompt_variable(recent, max_length=None, allow_newlines=True),
                MAX_DECISION_CONTEXT_CHARS,
                marker=CONTEXT_CAP_MARKER,
            ),
        }
        # 回落取 ``ConfigSpec.reset_value``：与读侧落盘、面板「恢复默认」同一表达式。
        raw = (
            str(self.settings.decision_prompt_template or "").strip()
            or CONFIG_SPEC_BY_KEY["decision_prompt_template"].reset_value
        )
        rendered = re.sub(
            r"\{([a-zA-Z0-9_]+)\}",
            lambda match: str(values.get(match.group(1), match.group(0))),
            raw,
        )
        if "{recent_messages}" not in raw and "{latest_message}" not in raw:
            rendered = rendered.strip() + "\n\n最近消息:\n" + values["recent_messages"]
        # 契约是否缺失只看**模板**（``raw``）：看 ``rendered`` 的话，群友只要在
        # 消息里写出 "should_reply" 就会让契约注入被静默抑制。
        if "should_reply" not in raw or "reason" not in raw:
            rendered = rendered.rstrip() + "\n\n" + DECISION_JSON_CONTRACT
        # 引用决定只在 model 模式下要求模型给出；用户模板里若已自带 quote 约定
        # 就不再追加（避免重复指令）。
        if self.settings.quote_mode == "model" and "quote" not in raw:
            rendered = rendered.rstrip() + "\n\n" + QUOTE_DECISION_HINT
        return rendered.strip()

    async def build_recent_messages(self, umo: str, state: SessionState, *, limit: int) -> str:
        return await build_history_text(
            umo=umo,
            local_records=list(state.recent)[-limit:],
            read_history=self._read_history,
            limit=limit,
            min_text_records=self.settings.decision_history_min_messages,
        )
