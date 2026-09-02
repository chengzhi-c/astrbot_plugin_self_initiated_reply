"""会话级"是否接话"裁决。

只负责裁决时序与闸门：判断模型调用（超时/失败分类）、判断提示词构建与
注入清理、明确请求窗口检测、局部闸门判定（免打扰/日配额/静默/冷却/观察窗口）。
对外只暴露一个裁决入口 ``decide``，入参为会话状态与触发类型，出参为
"回复/跳过+原因"。模型解析/生成、历史读取、Vision 描述经注入回调执行，
因此可脱离插件实例独立单测（注入假判断模型与假时钟）。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from .models import (
    DECISION_JSON_CONTRACT,
    DEFAULT_DECISION_PROMPT_TEMPLATE,
    PLUGIN_ID,
    REPLY_REQUEST_WINDOW_SEC,
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
    latest_user_text,
    looks_like_reply_request,
    parse_decision_json,
    response_text,
)

DECISION_SYSTEM_PROMPT = "你是群聊主动回复时机判断器。只输出严格 JSON，不要输出解释。"
DECISION_MAX_TOKENS = 120


def _localtime_minutes() -> int:
    now = time.localtime()
    return now.tm_hour * 60 + now.tm_min


class DecisionMaker:
    """ "是否接话"裁决：闸门判定、明确请求窗口、提示词构建与模型调用。"""

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
    ) -> None:
        self.settings = settings
        self._clock = clock
        self._minutes_now = minutes_now
        self._resolve_provider = resolve_provider
        self._llm_generate = llm_generate
        self._read_history = read_history
        self._build_image_context = build_image_context
        self._invalid_quiet_hours_logged: set[str] = set()

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
        silence_left = state.remaining_silence_sec(
            self.settings.min_silence_sec, self._clock(), active_at=active_for_silence
        )
        if silence_left > 0 or not active_for_silence:
            # max(0, ...)：silence_left 可以大于 min_silence_sec
            # ——载入时时间戳被钳到 now + MAX_CLOCK_SKEW_SEC，最多仍能超出一个偏移量，
            # 差值为负会向运营者显示「静默时间不足：-300s / 45s」这种自相矛盾的文案。
            elapsed = (
                max(0, int(self.settings.min_silence_sec - silence_left)) if silence_left else 0
            )
            return f"静默时间不足：{elapsed}s / {self.settings.min_silence_sec}s。"
        cooldown_left = self.settings.cooldown_sec - (self._clock() - state.last_proactive_at)
        if cooldown_left >= 1:
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
        if sh > 23 or eh > 23 or sm > 59 or em > 59:
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
    # 明确请求窗口检测
    # ------------------------------------------------------------------

    def recent_reply_request_reason(
        self, state: SessionState, *, window_sec: int = REPLY_REQUEST_WINDOW_SEC
    ) -> str:
        now = self._clock()
        for item in reversed(list(state.recent)):
            if item.role != "user":
                continue
            if item.at <= state.last_proactive_observed_at or now - item.at > window_sec:
                break
            if looks_like_reply_request(item.text, self.settings.bot_aliases):
                # reason 面向运营者（INFO 日志与 GET /status），允许引用 40 字用户原文。
                # 与 log_reply_content（回复正文）不是同一开关：state.json 本就持久化 recent 全文。
                return f"最近 {int(now - item.at)}s 内有人明确让 Bot 接话：{item.text[:40]}"
        return ""

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
            intent_reason = (
                "" if trigger == CheckTrigger.PATROL else self.recent_reply_request_reason(state)
            )
            decision = (
                {"should_reply": True, "reason": intent_reason, "elapsed_sec": 0.0}
                if intent_reason
                else await self.ask_decision_model(umo, state, trigger=trigger)
            )
        if not decision.get("should_reply"):
            return f"判断不回复：{decision.get('reason') or '未说明'}"
        return decision

    # ------------------------------------------------------------------
    # 判断模型调用（超时/失败分类；文案冻结于行为契约 §7）
    # ------------------------------------------------------------------

    async def ask_decision_model(
        self, umo: str, state: SessionState, *, trigger: str
    ) -> dict[str, Any]:
        """问判断模型「这轮该不该主动接话」，返回 should_reply / reason / elapsed_sec。

        判断模型关闭时按触发源分流：``patrol`` 放行（巡检本身即意图），其余拒绝
        （无明确请求不打扰）。

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
                "reason": "判断模型关闭且未检测到明确请求",
                "elapsed_sec": 0.0,
            }
        provider_id = ""
        try:
            provider_id = await self._resolve_provider(umo)
        except Exception as exc:
            # provider 解析链路的业务故障（配置坏/DB 错）在此分类，避免被当作
            # "不存在"而输出误导性的"未找到可用判断模型"。
            logger.error("[%s] resolve decision provider failed: %s", PLUGIN_ID, exc)
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
        try:
            response = await asyncio.wait_for(
                self._llm_generate(provider_id, prompt),
                timeout=self.settings.decision_timeout_sec,
            )
        except TimeoutError:
            return {
                "should_reply": False,
                "reason": "判断模型超时",
                "elapsed_sec": self._clock() - started,
            }
        except Exception as exc:
            logger.warning("[%s] decision model failed: %s", PLUGIN_ID, exc)
            return {
                "should_reply": False,
                "reason": f"判断模型异常：{exc}",
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
            "elapsed_sec": self._clock() - started,
        }

    # ------------------------------------------------------------------
    # 提示词构建与注入清理（不可信用户内容不能改变任务边界）
    # ------------------------------------------------------------------

    async def build_decision_prompt(self, umo: str, state: SessionState, trigger: str) -> str:
        aliases = "、".join(self.settings.bot_aliases) or "未配置"
        recent = await self.build_recent_messages(
            umo, state, limit=max(8, self.settings.decision_history_min_messages)
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
            # recent_messages 是多行聊天记录，保留换行才能让模型区分发言人和轮次
            "recent_messages": sanitize_prompt_variable(
                recent, max_length=2000, allow_newlines=True
            ),
        }
        raw = (
            str(self.settings.decision_prompt_template or "").strip()
            or DEFAULT_DECISION_PROMPT_TEMPLATE
        )
        rendered = re.sub(
            r"\{([a-zA-Z0-9_]+)\}",
            lambda match: str(values.get(match.group(1), match.group(0))),
            raw,
        )
        if "{recent_messages}" not in raw and "{latest_message}" not in raw:
            rendered = rendered.strip() + "\n\n最近消息:\n" + values["recent_messages"]
        if "should_reply" not in rendered or "reason" not in rendered:
            rendered = rendered.rstrip() + "\n\n" + DECISION_JSON_CONTRACT
        return rendered.strip()

    async def build_recent_messages(self, umo: str, state: SessionState, *, limit: int) -> str:
        return await build_history_text(
            umo=umo,
            local_records=list(state.recent)[-limit:],
            read_history=self._read_history,
            limit=limit,
            min_text_records=self.settings.decision_history_min_messages,
        )
