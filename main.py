from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from quart import request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, register
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.astr_main_agent import MainAgentBuildConfig, _get_session_conv, build_main_agent
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_handler import EventType
from astrbot.core.pipeline.context import call_event_hook

from .adapters import AstrBotBridge
from .commands import (
    debug_text,
    help_text,
    list_text,
    parse_command_text,
    status_text,
    strip_command_prefix,
)
from .models import (
    COMMAND_HANDLED_KEY,
    DECISION_JSON_CONTRACT,
    DEFAULT_DECISION_PROMPT_TEMPLATE,
    EVENT_CLEANUP_INTERVAL_SEC,
    MAX_AGENT_STEPS,
    MAX_CACHED_EVENTS,
    PATROL_BACKOFF_DELAY_SEC,
    PLUGIN_ID,
    PLUGIN_VERSION,
    REPLY_REQUEST_WINDOW_SEC,
    MessageRecord,
    SessionState,
    Settings,
    duration,
    now_ts,
)
from .storage import load_config_data, load_sessions, migrate_config_file, save_sessions, sync_config_whitelist
from .unified_manager import UnifiedManagerApi
from .utils import (
    clean_chat_text,
    clean_reply,
    count_text_records,
    dedupe_message_records,
    event_sender_id,
    event_sender_name,
    event_text,
    event_umo,
    format_message_records,
    is_admin_event,
    is_explicit_direct_call,
    is_self_message,
    latest_user_text,
    looks_like_reply_request,
    parse_json,
    session_whitelisted,
    whitelist_storage_key,
)

ADMIN_COMMAND_ACTIONS = {"status", "list", "add", "remove", "check", "on", "off", "debug"}
# MAX_AGENT_STEPS / REPLY_REQUEST_WINDOW_SEC / MAX_CACHED_EVENTS 统一从 models 导入，
# 此处不再重复定义，避免同名常量遮蔽。


@register(
    PLUGIN_ID,
    "chengzhi-c/Codex",
    "精简主动回复插件：白名单会话内，避开 @Bot/命令后自然接话",
    PLUGIN_VERSION,
)
class SelfInitiatedReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._storage_path = Path.home() / ".astrbot" / "data" / "plugin_data" / PLUGIN_ID / "state.json"
        self._config_path = Path.home() / ".astrbot" / "data" / "config" / f"{PLUGIN_ID}_config.json"

        config_data = load_config_data(self._config_path, self.config)
        self.settings = Settings.from_config(config_data)
        self.runtime_enabled = self.settings.enabled

        # 只保留历史记录桥接；表情包和 livingmemory 不再由本插件直连，
        # 改为通过 AstrBot 正常 LLM 管线自动触发，行为更接近 @Bot 回复。
        self.bridge = AstrBotBridge(context)
        self.unified_manager = UnifiedManagerApi(self)

        migrate_config_file(self._config_path, self.config, self.settings)

        self.sessions = load_sessions(
            self._storage_path,
            self.settings.whitelist,
            self.settings.recent_message_limit,
        )
        self._last_events: dict[str, AstrMessageEvent] = {}
        self._whitelist_runtime_umos: dict[str, str] = {}
        self._delay_tasks: dict[str, asyncio.Task[Any]] = {}
        self._running_sessions: set[str] = set()
        self._patrol_task: asyncio.Task[Any] | None = None
        self._stopping = False
        self._save_lock = asyncio.Lock()
        self._invalid_quiet_hours_logged: set[str] = set()
        self._admin_ids = self._load_global_admin_ids()
        self._last_event_cleanup = now_ts()  # 事件清理时间戳

        self._save_storage_sync()
        self._ensure_patrol_task()
        logger.info(
            "[%s] v%s enabled=%s whitelist=%d message_trigger=%s patrol_trigger=%s pipeline_mode=true",
            PLUGIN_ID,
            PLUGIN_VERSION,
            self.runtime_enabled,
            len(self.settings.whitelist),
            self.settings.enabled_message_trigger,
            self.settings.enabled_patrol_trigger,
        )
        self._register_web_apis()

    def _load_global_admin_ids(self) -> set[str]:
        path = self._storage_path.parents[2] / "cmd_config.json"
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                admins = data.get("admins_id", []) if isinstance(data, dict) else []
                return {str(item).strip() for item in admins if str(item).strip()}
        except Exception as exc:
            logger.debug("[%s] load admins failed path=%s error=%s", PLUGIN_ID, path, exc)
        return set()

    def _state_for(self, umo: str) -> SessionState:
        state = self.sessions.get(umo)
        if state is None:
            state = SessionState(recent=deque(maxlen=self.settings.recent_message_limit))
            self.sessions[umo] = state
        return state

    def _runtime_umo_for_whitelist_item(self, item: str) -> str:
        value = str(item or "").strip()
        if ":" in value:
            return value
        return self._whitelist_runtime_umos.get(value, "")

    def _save_storage_sync(self) -> None:
        save_sessions(
            self._storage_path,
            self.sessions,
            self.settings.whitelist,
            self.settings.recent_message_limit,
        )

    async def _save_storage(self) -> None:
        async with self._save_lock:
            await asyncio.to_thread(
                save_sessions,
                self._storage_path,
                self.sessions,
                self.settings.whitelist,
                self.settings.recent_message_limit,
            )

    def _sync_whitelist(self) -> None:
        sync_config_whitelist(self._config_path, self.config, self.settings)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        text = event_text(event).strip()
        if self._event_extra(event, COMMAND_HANDLED_KEY, False):
            return
        parsed = parse_command_text(text)
        if parsed is not None:
            await self._handle_inline_command(event, parsed)
            return

        if not self.runtime_enabled or event.is_stopped():
            return
        umo = event_umo(event)
        if not session_whitelisted(umo, self.settings.whitelist):
            return
        state_key = whitelist_storage_key(umo, self.settings.whitelist)
        self._whitelist_runtime_umos[state_key] = umo
        if self._should_ignore_event(event, text):
            return

        clean_text = clean_chat_text(text)
        if not clean_text:
            return
        state = self._state_for(state_key)
        state.last_active_at = now_ts()
        state.last_active_sender_id = event_sender_id(event)
        state.recent.append(
            MessageRecord(
                role="user",
                name=event_sender_name(event),
                sender_id=state.last_active_sender_id,
                text=clean_text,
                at=state.last_active_at,
            )
        )

        # 缓存该会话最近一次可用 event，供消息触发与后台巡检共用。
        # 后台巡检是独立定时器，需要一个不随单次 delay 生命周期消失的 event 引用，
        # 否则巡检判断通过后拿不到 event 会静默失败。清理交给 _cleanup_old_events_if_needed。
        self._last_events[umo] = event
        self._cleanup_old_events_if_needed()

        if self.settings.enabled_message_trigger:
            trigger = "reply_request" if looks_like_reply_request(clean_text, self.settings.bot_aliases) else "message_delay"
            delay = self._message_trigger_delay(trigger)
            self._schedule_delayed_check(umo, delay_sec=delay, trigger=trigger, force=False)

    def _should_ignore_event(self, event: AstrMessageEvent, text: str) -> bool:
        if is_self_message(event):
            return True
        if not text or text.startswith("/"):
            return True
        if event_sender_id(event) in self.settings.ignored_sender_ids:
            return True
        return is_explicit_direct_call(event, text)

    def _message_trigger_delay(self, trigger: str) -> int:
        min_silence = max(0, int(self.settings.min_silence_sec))
        if trigger == "reply_request":
            return min_silence
        return max(int(self.settings.message_delay_sec), min_silence)

    def _schedule_delayed_check(
        self,
        umo: str,
        *,
        delay_sec: int | None,
        trigger: str,
        force: bool,
    ) -> None:
        old_task = self._delay_tasks.pop(umo, None)
        if old_task and not old_task.done():
            old_task.cancel()
        task = asyncio.create_task(self._delayed_check(umo, delay_sec=delay_sec, trigger=trigger, force=force))
        self._delay_tasks[umo] = task
        task.add_done_callback(lambda done, session=umo: self._discard_delay_task(session, done))

    async def _delayed_check(
        self,
        umo: str,
        *,
        delay_sec: int | None = None,
        trigger: str = "message_delay",
        force: bool = False,
    ) -> None:
        try:
            delay = self.settings.message_delay_sec if delay_sec is None else max(0, delay_sec)
            if delay > 0:
                await asyncio.sleep(delay)
            if self._stopping or not self.runtime_enabled:
                return
            state = self._state_for(whitelist_storage_key(umo, self.settings.whitelist))
            silence_left = self._remaining_silence_sec(state)
            while not force and silence_left > 0:
                logger.info(
                    "[%s] wait for minimum silence session=%s trigger=%s remaining=%.2fs",
                    PLUGIN_ID,
                    umo,
                    trigger,
                    silence_left,
                )
                await asyncio.sleep(silence_left + 0.1)
                if self._stopping or not self.runtime_enabled:
                    return
                silence_left = self._remaining_silence_sec(state)
            result = await self._check_session(umo, trigger=trigger, force=force)
            logger.debug("[%s] check result session=%s trigger=%s result=%s", PLUGIN_ID, umo, trigger, result)
            # 不在此处 pop event：巡检触发与消息触发共用最近 event，
            # 陈旧引用由 _cleanup_old_events_if_needed 按间隔与数量上限统一清理。
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("[%s] delayed check failed session=%s error=%s", PLUGIN_ID, umo, exc)

    def _discard_delay_task(self, umo: str, task: asyncio.Task[Any]) -> None:
        if self._delay_tasks.get(umo) is task:
            self._delay_tasks.pop(umo, None)

    def _cleanup_old_events_if_needed(self) -> None:
        """定期清理陈旧事件，防止内存泄漏"""
        now = now_ts()
        if now - self._last_event_cleanup < EVENT_CLEANUP_INTERVAL_SEC:
            return

        self._last_event_cleanup = now

        # 清理不在运行中的会话事件
        stale_keys = [
            umo for umo in list(self._last_events.keys())
            if umo not in self._running_sessions
        ]

        # 每次清理一半陈旧事件
        for key in stale_keys[:len(stale_keys) // 2]:
            self._last_events.pop(key, None)

        # 硬性上限保护
        if len(self._last_events) > MAX_CACHED_EVENTS:
            excess = len(self._last_events) - MAX_CACHED_EVENTS
            sorted_keys = sorted(self._last_events.keys())[:excess]
            for key in sorted_keys:
                if key not in self._running_sessions:
                    self._last_events.pop(key, None)
            logger.info(
                "[%s] cleaned up %d cached events (total: %d)",
                PLUGIN_ID,
                excess,
                len(self._last_events),
            )

    def _ensure_patrol_task(self) -> None:
        if not self.settings.enabled_patrol_trigger or self._stopping or not self.runtime_enabled:
            return
        if self._patrol_task is None or self._patrol_task.done():
            self._patrol_task = asyncio.create_task(self._patrol_loop())

    async def _patrol_loop(self) -> None:
        while not self._stopping and self.runtime_enabled and self.settings.enabled_patrol_trigger:
            try:
                await asyncio.sleep(self.settings.check_interval_sec)
                now = now_ts()
                for item in list(self.settings.whitelist):
                    try:
                        umo = self._runtime_umo_for_whitelist_item(item)
                        if not umo:
                            continue
                        state = self._state_for(whitelist_storage_key(umo, self.settings.whitelist))
                        if self.settings.patrol_inactive_after_sec and (
                            not state.last_active_at or now - state.last_active_at > self.settings.patrol_inactive_after_sec
                        ):
                            continue
                        if umo in self._running_sessions:
                            continue
                        result = await self._check_session(umo, trigger="patrol", force=False)
                        logger.debug("[%s] patrol result session=%s result=%s", PLUGIN_ID, umo, result)
                    except Exception as exc:
                        logger.warning("[%s] patrol session failed session=%s error=%s", PLUGIN_ID, umo, exc, exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[%s] patrol loop failed error=%s", PLUGIN_ID, exc, exc_info=True)
                # 添加退避延迟，避免错误循环
                await asyncio.sleep(min(PATROL_BACKOFF_DELAY_SEC, self.settings.check_interval_sec))

    async def _check_session(self, umo: str, *, trigger: str, force: bool) -> str:
        if self._stopping or (not force and not self.runtime_enabled):
            return "插件未启用。"
        if not force and not session_whitelisted(umo, self.settings.whitelist):
            return "会话不在主动回复白名单。"
        if umo in self._running_sessions:
            return "已有判断任务在运行。"
        state = self._state_for(whitelist_storage_key(umo, self.settings.whitelist))

        # 刷新日期后做一次本地闸门检查。单线程 asyncio 下，闸门检查与后续判断之间
        # 若无 await 就不存在竞争，无需重复检查。
        state.refresh_day()
        gate = self._local_gate(state, force=force)
        if gate:
            logger.info("[%s] skip session=%s trigger=%s reason=%s", PLUGIN_ID, umo, trigger, gate)
            return gate

        self._running_sessions.add(umo)
        try:
            # 强制检查直接进入主 Agent；普通触发优先识别明确请求，否则交给判断模型。
            if force:
                decision = {"should_reply": True, "reason": "手动强制检查", "elapsed_sec": 0.0}
            else:
                intent_reason = "" if trigger == "patrol" else self._recent_reply_request_reason(state)
                decision = (
                    {"should_reply": True, "reason": intent_reason, "elapsed_sec": 0.0}
                    if intent_reason
                    else await self._ask_decision_model(umo, state, trigger=trigger)
                )

            logger.info(
                "[%s] decision session=%s trigger=%s should_reply=%s elapsed=%.2fs reason=%s",
                PLUGIN_ID,
                umo,
                trigger,
                decision.get("should_reply"),
                float(decision.get("elapsed_sec") or 0.0),
                decision.get("reason") or "-",
            )

            if not decision.get("should_reply"):
                return f"判断不回复：{decision.get('reason') or '未说明'}"

            # 使用正常 AstrBot 管线生成回复（包含所有工具）
            reply = await self._generate_reply_via_pipeline(umo, state)
            
            if not reply:
                return "管线未生成内容。"

            gate = self._local_gate(state, force=force)
            if gate:
                logger.info("[%s] skip before send session=%s trigger=%s reason=%s", PLUGIN_ID, umo, trigger, gate)
                return gate

            # 发送回复
            sent = await self._send_reply(umo, reply)
            if not sent:
                return "主动发送失败。"

            if self.settings.log_reply_content:
                preview = reply if len(reply) <= 80 else reply[:80] + "…"
                logger.info(
                    "[%s] proactive reply sent session=%s chars=%d text=%s",
                    PLUGIN_ID, umo, len(reply), preview,
                )
            else:
                logger.info("[%s] proactive reply sent session=%s chars=%d", PLUGIN_ID, umo, len(reply))
            
            # 更新状态
            state.last_proactive_at = now_ts()
            state.last_proactive_observed_at = state.last_active_at
            state.last_proactive_text = reply
            state.daily_count += 1
            state.recent.append(
                MessageRecord(
                    role="assistant",
                    name="Bot",
                    text=reply,
                    at=state.last_proactive_at,
                )
            )
            await self._save_storage()
            return "已主动回复。"
        finally:
            self._running_sessions.discard(umo)

    def _local_gate(self, state: SessionState, *, force: bool) -> str:
        if force:
            return ""
        if self._in_quiet_hours():
            return "免打扰时段。"
        if self.settings.max_daily_replies_per_session and (
            state.daily_count >= self.settings.max_daily_replies_per_session
        ):
            return "今日主动回复次数已达上限。"
        silence = now_ts() - state.last_active_at if state.last_active_at else 0
        if silence < self.settings.min_silence_sec:
            return f"静默时间不足：{int(silence)}s / {self.settings.min_silence_sec}s。"
        cooldown_left = self.settings.cooldown_sec - (now_ts() - state.last_proactive_at)
        if cooldown_left >= 1:
            return f"冷却中：还剩 {duration(cooldown_left)}。"
        if state.last_proactive_observed_at >= state.last_active_at:
            return "这条消息之后已经主动回复过。"
        return ""

    def _remaining_silence_sec(self, state: SessionState) -> float:
        if not state.last_active_at:
            return 0.0
        silence_left = self.settings.min_silence_sec - (now_ts() - state.last_active_at)
        return max(0.0, silence_left)

    async def _generate_reply_via_pipeline(self, umo: str, state: SessionState) -> str:
        """通过 AstrBot 主 Agent 生成回复，使 llm_tool/on_llm_request/on_llm_response 正常生效。"""
        last_event = self._last_events.get(umo)
        if not last_event:
            logger.warning("[%s] no last event for session=%s", PLUGIN_ID, umo)
            return ""

        context_text = await self._build_context_text(umo, state)
        length_hint = {
            "short": "回复要非常简短，控制在一句话或几个字，像随口搭一句。",
            "balanced": "回复自然均衡，一两句话即可，不要长篇大论。",
            "expressive": "可以稍微展开，但仍保持群聊口吻，最多两三句。",
        }.get(self.settings.reply_length_mode, "回复自然均衡，一两句话即可，不要长篇大论。")
        system_hint = (
            "你正在群聊中主动接话。请根据最近的聊天记录自然地回复一句话，像群友聊天一样。"
            f"{length_hint}"
            "如果最近用户明确要求表情包/动图/发图，优先调用 search_emoji 搜索表情包候选，再调用 send_emoji_by_id 发送表情包。"
            "其他情绪合适的场景，也可以自然地调用表情包工具。"
            "可以使用 LivingMemory/记忆工具检索和保存有价值的信息。"
            "不要解释你为什么出现，不要提系统/模型/API/插件。"
        )
        prompt = f"{system_hint}\n\n最近聊天:\n{context_text}\n\n请自然地接一句话。"

        try:
            req = ProviderRequest()
            req.prompt = prompt
            req.image_urls = []
            req.audio_urls = []
            req.session_id = umo
            try:
                conversation = await _get_session_conv(last_event, self.context)
                req.conversation = conversation
                req.contexts = json.loads(conversation.history)
            except Exception as exc:
                logger.debug("[%s] load conversation failed session=%s error=%s", PLUGIN_ID, umo, exc)
            last_event.set_extra("provider_request", req)
            last_event.set_extra("self_initiated_reply", True)

            build_result = await build_main_agent(
                event=last_event,
                plugin_context=self.context,
                config=self._main_agent_build_config(),
                req=req,
                apply_reset=False,
            )
            if build_result is None:
                return ""

            if await call_event_hook(last_event, EventType.OnLLMRequestEvent, build_result.provider_request):
                if build_result.reset_coro:
                    build_result.reset_coro.close()
                return ""
            if build_result.reset_coro:
                await build_result.reset_coro

            async def _run() -> None:
                async for _ in run_agent(
                    build_result.agent_runner,
                    max_step=MAX_AGENT_STEPS,
                    show_tool_use=False,
                    show_tool_call_result=False,
                    stream_to_general=False,
                    show_reasoning=False,
                    buffer_intermediate_messages=True,
                ):
                    pass

            await asyncio.wait_for(_run(), timeout=self.settings.generation_timeout_sec)
            response = build_result.agent_runner.get_final_llm_resp()
            reply_text = str(getattr(response, "completion_text", "") or "").strip()
            if not reply_text and getattr(response, "result_chain", None):
                try:
                    reply_text = response.result_chain.get_plain_text().strip()
                except Exception:
                    reply_text = ""
            if reply_text:
                reply_text = clean_reply(
                    reply_text,
                    allow_multiline=self.settings.allow_multiline_reply,
                    max_chars=self.settings.max_reply_chars,
                )
            return reply_text

        except asyncio.TimeoutError:
            logger.warning(
                "[%s] main-agent generation timeout session=%s timeout=%.1fs",
                PLUGIN_ID,
                umo,
                self.settings.generation_timeout_sec,
            )
            return ""
        except Exception as exc:
            logger.warning("[%s] main-agent generation failed session=%s error=%s", PLUGIN_ID, umo, exc, exc_info=True)
            return ""
        finally:
            try:
                last_event.set_extra("provider_request", None)
            except Exception:
                pass

    def _main_agent_build_config(self) -> MainAgentBuildConfig:
        provider_settings = {}
        try:
            provider_settings = dict(self.context.astrbot_config.get("provider_settings", {}) or {})
        except Exception:
            pass
        return MainAgentBuildConfig(
            tool_call_timeout=int(provider_settings.get("tool_call_timeout", 60) or 60),
            tool_schema_mode=str(provider_settings.get("tool_schema_mode", "full") or "full"),
            provider_wake_prefix="",
            streaming_response=False,
            sanitize_context_by_modalities=bool(provider_settings.get("sanitize_context_by_modalities", False)),
            kb_agentic_mode=False,
            file_extract_enabled=False,
            llm_safety_mode=bool(provider_settings.get("llm_safety_mode", True)),
            safety_mode_strategy=str(provider_settings.get("safety_mode_strategy", "system_prompt") or "system_prompt"),
            computer_use_runtime="none",
            add_cron_tools=False,
            provider_settings=provider_settings,
        )

    async def _send_reply(self, umo: str, reply: str) -> bool:
        """发送主动回复消息，并尽量走发送前/发送后钩子。"""
        last_event = self._last_events.get(umo)
        if last_event:
            sent = False
            try:
                last_event.set_result(
                    MessageEventResult()
                    .message(reply)
                    .set_result_content_type(ResultContentType.LLM_RESULT)
                )
                await call_event_hook(last_event, EventType.OnDecoratingResultEvent)
                result = last_event.get_result()
                if result is None or not result.chain:
                    try:
                        last_event.clear_result()
                    except Exception:
                        pass
                    return False
                await last_event.send(result)
                sent = True
                await call_event_hook(last_event, EventType.OnAfterMessageSentEvent)
                last_event.clear_result()
                return True
            except Exception as exc:
                logger.warning("[%s] event send reply failed session=%s error=%s", PLUGIN_ID, umo, exc, exc_info=True)
                try:
                    last_event.clear_result()
                except Exception:
                    pass
                if sent:
                    return True
        try:
            ok = await self.context.send_message(umo, MessageChain().message(reply))
            return bool(ok)
        except Exception as exc:
            logger.warning("[%s] send reply failed session=%s error=%s", PLUGIN_ID, umo, exc)
            return False

    def _in_quiet_hours(self) -> bool:
        now = time.localtime()
        current = now.tm_hour * 60 + now.tm_min
        for item in self.settings.quiet_hours:
            parsed = self._parse_quiet_hour(item)
            if parsed is None:
                continue
            begin, finish = parsed
            if (begin <= finish and begin <= current <= finish) or (
                begin > finish and (current >= begin or current <= finish)
            ):
                return True
        return False

    def _parse_quiet_hour(self, item: str) -> tuple[int, int] | None:
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

    def _recent_reply_request_reason(self, state: SessionState, *, window_sec: int = REPLY_REQUEST_WINDOW_SEC) -> str:
        now = now_ts()
        for item in reversed(list(state.recent)):
            if item.role != "user":
                continue
            if item.at <= state.last_proactive_observed_at or now - item.at > window_sec:
                break
            if looks_like_reply_request(item.text, self.settings.bot_aliases):
                return f"最近 {int(now - item.at)}s 内有人明确让 Bot 接话：{item.text[:40]}"
        return ""

    async def _ask_decision_model(self, umo: str, state: SessionState, *, trigger: str) -> dict[str, Any]:
        started = now_ts()
        if not self.settings.decision_model_enabled:
            if trigger == "patrol":
                return {"should_reply": True, "reason": "判断模型关闭，后台巡检触发", "elapsed_sec": 0.0}
            return {"should_reply": False, "reason": "判断模型关闭且未检测到明确请求", "elapsed_sec": 0.0}
        provider_id = await self.bridge.resolve_provider_id(umo, self.settings.judge_provider_id)
        if not provider_id:
            return {"should_reply": False, "reason": "未找到可用判断模型", "elapsed_sec": now_ts() - started}
        prompt = await self._build_decision_prompt(umo, state, trigger)
        try:
            response = await asyncio.wait_for(
                self.bridge.llm_generate(
                    provider_id=provider_id,
                    prompt=prompt,
                    system_prompt="你是群聊主动回复时机判断器。只输出严格 JSON，不要输出解释。",
                    temperature=self.settings.decision_temperature,
                    max_tokens=120,
                ),
                timeout=self.settings.decision_timeout_sec,
            )
        except asyncio.TimeoutError:
            return {"should_reply": False, "reason": "判断模型超时", "elapsed_sec": now_ts() - started}
        except Exception as exc:
            logger.warning("[%s] decision model failed: %s", PLUGIN_ID, exc)
            return {"should_reply": False, "reason": f"判断模型异常：{exc}", "elapsed_sec": now_ts() - started}

        raw = str(getattr(response, "completion_text", "") or "").strip()
        if not raw:
            result_chain = getattr(response, "result_chain", None)
            get_plain_text = getattr(result_chain, "get_plain_text", None)
            if callable(get_plain_text):
                raw = str(get_plain_text() or "").strip()
        parsed = parse_json(raw)
        if not isinstance(parsed, dict):
            return {"should_reply": False, "reason": "判断模型未返回有效 JSON", "elapsed_sec": now_ts() - started}
        value = parsed.get("should_reply")
        if not isinstance(value, bool):
            value = str(value).strip().lower() in {"true", "1", "yes", "是"}
        return {
            "should_reply": bool(value),
            "reason": str(parsed.get("reason") or "").strip(),
            "elapsed_sec": now_ts() - started,
        }

    async def _build_decision_prompt(self, umo: str, state: SessionState, trigger: str) -> str:
        aliases = "、".join(self.settings.bot_aliases) or "未配置"
        recent = await self._build_recent_messages(umo, state, limit=max(8, self.settings.decision_history_min_messages))
        latest = latest_user_text(list(state.recent))
        values = {
            "session": umo,
            "trigger": trigger,
            "bot_aliases": aliases,
            "last_message_age_sec": str(int(now_ts() - state.last_active_at) if state.last_active_at else 0),
            "last_reply_age_sec": str(int(now_ts() - state.last_proactive_at) if state.last_proactive_at else -1),
            "latest_message": latest,
            "recent_messages": recent,
        }
        raw = str(self.settings.decision_prompt_template or "").strip() or DEFAULT_DECISION_PROMPT_TEMPLATE
        rendered = re.sub(
            r"\{([a-zA-Z0-9_]+)\}",
            lambda match: str(values.get(match.group(1), match.group(0))),
            raw,
        )
        if "{recent_messages}" not in raw and "{latest_message}" not in raw:
            rendered = rendered.strip() + "\n\n最近消息:\n" + recent
        if "should_reply" not in rendered or "reason" not in rendered:
            rendered = rendered.rstrip() + "\n\n" + DECISION_JSON_CONTRACT
        return rendered.strip()

    async def _build_recent_messages(self, umo: str, state: SessionState, *, limit: int) -> str:
        local_records = list(state.recent)[-limit:]
        records: list[MessageRecord] = []
        if count_text_records(local_records) < self.settings.decision_history_min_messages:
            records.extend(await self.bridge.read_astrbot_history(umo, limit=limit))
        records.extend(local_records)
        return format_message_records(dedupe_message_records(records), limit=limit)

    async def _build_context_text(self, umo: str, state: SessionState) -> str:
        records = list(state.recent)[-self.settings.recent_message_limit :]
        if count_text_records(records) < min(5, self.settings.recent_message_limit):
            records = await self.bridge.read_astrbot_history(umo, limit=self.settings.recent_message_limit) + records
        records = dedupe_message_records(records)
        return format_message_records(records, limit=self.settings.recent_message_limit)

    async def _add_whitelist_session(self, umo: str) -> bool:
        existed = session_whitelisted(umo, self.settings.whitelist)
        self.settings.whitelist.add(umo)
        self._state_for(whitelist_storage_key(umo, self.settings.whitelist))
        self._sync_whitelist()
        await self._save_storage()
        logger.info("[%s] whitelist add session=%s existed=%s total=%d", PLUGIN_ID, umo, existed, len(self.settings.whitelist))
        return not existed

    async def _remove_whitelist_session(self, umo: str) -> bool:
        key = whitelist_storage_key(umo, self.settings.whitelist)
        existed = session_whitelisted(umo, self.settings.whitelist)
        self.settings.whitelist.discard(key)
        self._last_events.pop(umo, None)
        self._last_events.pop(key, None)
        self._whitelist_runtime_umos.pop(key, None)
        task = self._delay_tasks.pop(umo, None)
        if task and not task.done():
            task.cancel()
        self._sync_whitelist()
        await self._save_storage()
        logger.info("[%s] whitelist remove session=%s existed=%s total=%d", PLUGIN_ID, umo, existed, len(self.settings.whitelist))
        return existed

    async def _handle_inline_command(self, event: AstrMessageEvent, parsed: tuple[str, str]) -> None:
        action, arg = parsed
        self._set_command_handled(event)
        if action in ADMIN_COMMAND_ACTIONS and not is_admin_event(event, self._admin_ids):
            await self._send_command_text(event, "没有权限执行该主动回复管理指令。")
            return
        await self._send_command_text(event, await self._command_text(event, action, arg))

    async def _command_text(self, event: AstrMessageEvent, action: str, arg: str = "") -> str:
        umo = event_umo(event)
        if action == "help":
            return help_text()
        if action == "status":
            state = self._state_for(whitelist_storage_key(umo, self.settings.whitelist)) if umo else SessionState()
            return status_text(self.settings, event, state, self.runtime_enabled)
        if action == "list":
            return list_text(self.settings)
        if not umo:
            return "无法识别当前会话。"
        if action == "add":
            added = await self._add_whitelist_session(umo)
            return f"已将当前会话加入主动回复白名单：{umo}" if added else f"当前会话已在主动回复白名单中：{umo}"
        if action == "remove":
            removed = await self._remove_whitelist_session(umo)
            return f"已移出主动回复白名单：{umo}" if removed else f"当前会话本不在主动回复白名单：{umo}"
        if action == "check":
            self._last_events[umo] = event
            state = self._state_for(whitelist_storage_key(umo, self.settings.whitelist))
            text = clean_chat_text(arg or strip_command_prefix(event_text(event)))
            if text:
                state.last_active_at = now_ts()
                state.last_active_sender_id = event_sender_id(event)
                state.recent.append(MessageRecord(role="user", name=event_sender_name(event), text=text, at=state.last_active_at))
            try:
                result = await self._check_session(umo, trigger="manual", force=True)
            finally:
                # 手动 check 用完即清理临时 event 引用，与消息触发路径对称。
                self._last_events.pop(umo, None)
            return f"主动回复检查结果：{result}"
        if action == "on":
            self.runtime_enabled = True
            self._ensure_patrol_task()
            return "主动回复插件已临时启用。"
        if action == "off":
            self.runtime_enabled = False
            self._cancel_delay_tasks()
            await self._stop_patrol_task()
            return "主动回复插件已临时暂停。"
        if action == "debug":
            return debug_text(self.settings, event, ignored_sender=event_sender_id(event) in self.settings.ignored_sender_ids)
        return help_text()

    async def _send_command_text(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain().message(text))
        except Exception as exc:
            logger.debug("[%s] inline command send failed: %s", PLUGIN_ID, exc)
            try:
                event.set_result(event.plain_result(text))
            except Exception:
                pass
        try:
            event.stop_event()
        except Exception:
            pass

    @filter.command_group("selfreply")
    async def selfreply(self, event: AstrMessageEvent):
        """主动回复：查看指令说明。"""
        self._set_command_handled(event)
        yield event.plain_result(help_text())

    @selfreply.command("help", alias={"h"})
    async def selfreply_help(self, event: AstrMessageEvent):
        """帮助：显示主动回复指令说明。"""
        self._set_command_handled(event)
        yield event.plain_result(help_text())

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("status", alias={"stat"})
    async def selfreply_status(self, event: AstrMessageEvent):
        """状态：查看运行状态、判断模型和白名单信息。"""
        self._set_command_handled(event)
        umo = event_umo(event)
        state = self._state_for(whitelist_storage_key(umo, self.settings.whitelist)) if umo else SessionState()
        yield event.plain_result(status_text(self.settings, event, state, self.runtime_enabled))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("list", alias={"ls", "whitelist"})
    async def selfreply_list(self, event: AstrMessageEvent):
        """列表：查看主动回复白名单。"""
        self._set_command_handled(event)
        yield event.plain_result(list_text(self.settings))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("add")
    async def selfreply_add(self, event: AstrMessageEvent):
        """加入：将当前会话加入主动回复白名单。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "add"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("remove", alias={"rm", "del", "delete"})
    async def selfreply_remove(self, event: AstrMessageEvent):
        """移除：将当前会话移出主动回复白名单。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "remove"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("check", alias={"test"})
    async def selfreply_check(self, event: AstrMessageEvent):
        """检查：手动测试一次主动回复，可附带测试内容。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "check"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("on", alias={"enable", "start"})
    async def selfreply_on(self, event: AstrMessageEvent):
        """开启：临时启用主动回复运行。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "on"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("off", alias={"disable", "pause", "stop"})
    async def selfreply_off(self, event: AstrMessageEvent):
        """关闭：临时暂停主动回复运行。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "off"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("debug", alias={"diag", "diagnose"})
    async def selfreply_debug(self, event: AstrMessageEvent):
        """调试：查看当前会话、发送者和触发识别信息。"""
        self._set_command_handled(event)
        yield event.plain_result(debug_text(self.settings, event, ignored_sender=event_sender_id(event) in self.settings.ignored_sender_ids))

    def _event_extra(self, event: AstrMessageEvent, key: str, default: Any = None) -> Any:
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

    def _set_command_handled(self, event: AstrMessageEvent) -> None:
        try:
            event.set_extra(COMMAND_HANDLED_KEY, True)
        except Exception:
            pass

    def _cancel_delay_tasks(self) -> None:
        for task in list(self._delay_tasks.values()):
            if task and not task.done():
                task.cancel()
        self._delay_tasks.clear()

    async def _stop_patrol_task(self) -> None:
        task = self._patrol_task
        self._patrol_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _register_web_apis(self) -> None:
        """注册统一管理页面所需的 Web API。"""
        register = self.context.register_web_api
        route = f"/{PLUGIN_ID}"
        register(
            f"{route}/config",
            self._api_get_config,
            ["GET"],
            "获取主动回复插件配置",
        )
        register(
            f"{route}/config",
            self._api_post_config,
            ["POST"],
            "更新主动回复插件配置",
        )
        register(
            f"{route}/status",
            self._api_status,
            ["GET"],
            "获取插件集成状态",
        )
        register(
            f"{route}/providers",
            self._api_providers,
            ["GET"],
            "获取可选判断模型 Provider",
        )
        self.unified_manager.register(self.context, route)

    @staticmethod
    def _config_value(config: Any, key: str, default: Any = "") -> Any:
        if isinstance(config, dict):
            return config.get(key, default)
        return getattr(config, key, default)

    def _provider_config(self, provider: Any) -> Any:
        if isinstance(provider, dict):
            return provider.get("provider_config") or provider.get("config") or provider
        return getattr(provider, "provider_config", None) or getattr(provider, "config", None) or {}

    def _provider_id(self, provider: Any, fallback_id: str = "") -> str:
        config = self._provider_config(provider)
        return str(
            self._config_value(config, "id")
            or self._config_value(config, "provider_id")
            or getattr(provider, "id", "")
            or getattr(provider, "provider_id", "")
            or fallback_id
            or ""
        ).strip()

    def _provider_label(self, provider: Any, provider_id: str) -> str:
        config = self._provider_config(provider)
        label = str(
            self._config_value(config, "display_name")
            or self._config_value(config, "name")
            or self._config_value(config, "model")
            or self._config_value(config, "model_name")
            or getattr(provider, "display_name", "")
            or getattr(provider, "name", "")
            or provider_id
        ).strip()
        return f"{label} ({provider_id})" if label and label != provider_id else label or provider_id

    def _provider_option(self, provider: Any, fallback_id: str = "") -> dict[str, str] | None:
        provider_id = self._provider_id(provider, fallback_id)
        if not provider_id:
            return None
        return {"id": provider_id, "label": self._provider_label(provider, provider_id)}

    @staticmethod
    def _provider_items(source: Any) -> list[Any]:
        if isinstance(source, dict):
            return list(source.items())
        return list(source or [])

    def _collect_provider_options(self) -> list[dict[str, str]]:
        providers: list[Any] = []
        get_all = getattr(self.context, "get_all_providers", None)
        if callable(get_all):
            try:
                providers = self._provider_items(get_all())
            except Exception as exc:
                logger.debug("[%s] get_all_providers failed: %s", PLUGIN_ID, exc)

        if not providers:
            providers = self._providers_from_manager()

        options: list[dict[str, str]] = []
        seen: set[str] = set()
        for provider in providers:
            fallback_id = ""
            if isinstance(provider, tuple) and len(provider) == 2:
                fallback_id = str(provider[0] or "")
                provider = provider[1]
            option = self._provider_option(provider, fallback_id)
            if not option or option["id"] in seen:
                continue
            seen.add(option["id"])
            options.append(option)
        return sorted(options, key=lambda item: item["label"].lower())

    def _providers_from_manager(self) -> list[Any]:
        provider_manager = getattr(self.context, "provider_manager", None)
        inst_map = getattr(provider_manager, "inst_map", None)
        if isinstance(inst_map, dict):
            return self._provider_items(inst_map)
        return []

    async def _api_get_config(self):
        """返回当前配置。"""
        try:
            min_context_messages = self.settings.decision_history_min_messages
            return {
                "enabled": self.runtime_enabled,
                "decision_model_enabled": self.settings.decision_model_enabled,
                "judge_provider_id": self.settings.judge_provider_id,
                "decision_prompt_template": self.settings.decision_prompt_template,
                "decision_prompt_default": DEFAULT_DECISION_PROMPT_TEMPLATE,
                "decision_temperature": self.settings.decision_temperature,
                "decision_timeout_sec": self.settings.decision_timeout_sec,
                "min_context_messages": min_context_messages,
                # Backward-compatible alias for older unified-manager frontend builds.
                "proactive_threshold": min_context_messages,
                "message_delay_sec": self.settings.message_delay_sec,
                "min_silence_sec": self.settings.min_silence_sec,
                "cooldown_sec": self.settings.cooldown_sec,
                "patrol_inactive_after_sec": self.settings.patrol_inactive_after_sec,
                # Backward-compatible aliases for older custom-page builds.
                "idle_trigger_seconds": self.settings.message_delay_sec,
                "cooldown_seconds": self.settings.cooldown_sec,
                "whitelist": list(self.settings.whitelist),
                "pipeline_mode": True,
            }
        except Exception as exc:
            logger.warning("[%s] api get config failed: %s", PLUGIN_ID, exc)
            return

    async def _api_providers(self):
        """返回当前可选聊天 Provider。"""
        try:
            return {"ok": True, "providers": self._collect_provider_options()}
        except Exception as exc:
            logger.warning("[%s] api providers failed: %s", PLUGIN_ID, exc)
            return {"ok": False, "providers": [], "error": str(exc)}

    async def _api_post_config(self):
        """更新配置。"""
        try:
            data = await request.get_json(silent=True) or {}
            # 先在临时 dict 中校验所有字段，全部通过后再一次性赋值
            updates: dict[str, Any] = {}
            if "enabled" in data:
                updates["enabled"] = bool(data["enabled"])
            if "decision_model_enabled" in data:
                updates["decision_model_enabled"] = bool(data["decision_model_enabled"])
            if "judge_provider_id" in data:
                updates["judge_provider_id"] = str(data["judge_provider_id"] or "").strip()
            if "decision_prompt_template" in data:
                prompt = str(data["decision_prompt_template"] or "").strip()
                updates["decision_prompt_template"] = prompt or DEFAULT_DECISION_PROMPT_TEMPLATE
            if "decision_temperature" in data:
                updates["decision_temperature"] = max(0.0, min(2.0, float(data["decision_temperature"])))
            if "decision_timeout_sec" in data:
                updates["decision_timeout_sec"] = max(1.0, min(300.0, float(data["decision_timeout_sec"])))
            cooldown_value = data.get("cooldown_sec", data.get("cooldown_seconds", None))
            if cooldown_value is not None:
                updates["cooldown_sec"] = max(0, min(86400, int(cooldown_value)))
            message_delay_value = data.get("message_delay_sec", data.get("idle_trigger_seconds", None))
            if message_delay_value is not None:
                updates["message_delay_sec"] = max(5, min(86400, int(message_delay_value)))
            if "min_silence_sec" in data:
                updates["min_silence_sec"] = max(0, min(86400, int(data["min_silence_sec"])))
            if "patrol_inactive_after_sec" in data:
                updates["patrol_inactive_after_sec"] = max(0, min(604800, int(data["patrol_inactive_after_sec"])))
            min_context_value = data.get("min_context_messages", data.get("proactive_threshold", None))
            if min_context_value is not None:
                updates["decision_history_min_messages"] = max(0, min(30, int(min_context_value)))
            if "whitelist" in data:
                updates["whitelist"] = set(str(s).strip() for s in data["whitelist"] if str(s).strip())

            # 原子赋值：使用异常处理确保一致性
            old_enabled = self.runtime_enabled
            try:
                # 先应用到 settings（除了 enabled）
                for key in ["decision_model_enabled", "judge_provider_id", "decision_prompt_template",
                            "decision_temperature", "decision_timeout_sec", "cooldown_sec",
                            "message_delay_sec", "min_silence_sec", "patrol_inactive_after_sec",
                            "decision_history_min_messages", "whitelist"]:
                    if key in updates:
                        setattr(self.settings, key, updates[key])

                if "enabled" in updates:
                    self.settings.enabled = updates["enabled"]

                # 持久化成功后再更新运行时状态
                if updates:
                    self._sync_whitelist()
                    await self._save_storage()

                # 最后更新运行时启用状态，并与命令路径一致地启停后台任务
                if "enabled" in updates:
                    self.runtime_enabled = updates["enabled"]
                    if self.runtime_enabled:
                        self._ensure_patrol_task()
                    else:
                        self._cancel_delay_tasks()
                        await self._stop_patrol_task()

                return {"ok": True}
            except Exception:
                # 发生异常时回滚运行时状态
                self.runtime_enabled = old_enabled
                raise
        except Exception as exc:
            logger.warning("[%s] api post config failed: %s", PLUGIN_ID, exc)
            return {"ok": False, "error": str(exc)}

    async def _api_status(self):
        """返回插件集成状态。"""
        return {
            "loaded": True,
            "runtime_enabled": self.runtime_enabled,
            "whitelist_count": len(self.settings.whitelist),
            "pipeline_mode": True,
            "decision_model_enabled": self.settings.decision_model_enabled,
        }

    async def terminate(self) -> None:
        self._stopping = True
        self._cancel_delay_tasks()
        await self._stop_patrol_task()
        self._last_events.clear()
        await self._save_storage()
        logger.info("[%s] terminated", PLUGIN_ID)
