"""插件入口与装配层。

拥有：唯一的 ``Star`` 子类、宿主事件接入（``on_message``）、``/selfreply``
指令处理器、生命周期（``terminate`` 与优雅停止），以及把各协作者接线成一个
流程的构造顺序。

业务规则不在这里：是否接话属 ``decision``，正文生成属 ``generation``，发送
状态机属 ``delivery``，定时与巡检属 ``scheduler``，会话状态属
``session_coordinator``。本文件只回答「谁先造、谁依赖谁、事件从哪进」。

模块顶部的 import 被 ``_AGENT_RUNTIME`` 分成两段（故 ruff 对本文件忽略
E402）：宿主私有符号必须先经适配层探测并绑到模块级名字，后续模块才能拿到
可被测试替换的那几个名字；整体上移会断掉这条测试缝。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, register

from .runtime_adapter import AstrBotRuntimeAdapter
from .scheduler import SessionScheduler
from .session_coordinator import SessionCoordinator
from .session_gate import SessionGate

if TYPE_CHECKING:
    from astrbot.api.event import MessageEventResult

    # 指令处理器的产出类型：每个 @selfreply.command 处理器都是 async generator，
    # 逐条 yield event.plain_result(...)。宿主侧契约是
    # AsyncGenerator[MessageEventResult | str | None]，本插件只 yield 前者。
    #
    # 只在 TYPE_CHECKING 下可见（纯文档用途，不引入加载期宿主依赖）。别改成运行时
    # import：宿主注册指令时只读 signature.parameters，从不读 return_annotation，
    # 也不用 get_type_hints/eval_str，故运行时无需此名字；而运行时 import 会让本文件
    # 多一条宿主符号硬依赖，且测试替身未导出该符号。
    CommandReply = AsyncGenerator[MessageEventResult, None]

_AGENT_RUNTIME = AstrBotRuntimeAdapter.from_host()

# 宿主私有符号收敛（ticket 13）：值全部来自适配层探测，本文件不再直接
# import 宿主私有层（astrbot.core.*）；模块级名字保留供测试替换与旧引用，
# 加载期缺失由 _validate_agent_api 的契约断言兜底（缺失即红，拒绝加载并
# 提示修复方向）。
call_event_hook = _AGENT_RUNTIME.capabilities.call_event_hook
get_astrbot_config_path = _AGENT_RUNTIME.capabilities.config_path_fn
get_astrbot_plugin_data_path = _AGENT_RUNTIME.capabilities.plugin_data_path_fn

from .adapters import AstrBotBridge
from .commands import (
    debug_text,
    help_text,
    list_text,
    parse_command_text,
    status_text,
    strip_command_prefix,
)
from .decision import DECISION_MAX_TOKENS, DECISION_SYSTEM_PROMPT, DecisionMaker
from .delivery import DeliveryRunner
from .generation import GenerationRunner
from .image import ImageExtractor, ImageInfo, ImageParser, format_image_context
from .image.recorder_bridge import get_recorder_bridge
from .models import (
    ADMIN_COMMAND_ACTIONS,
    ADMIN_REFRESH_WINDOW_SEC,
    COMMAND_HANDLED_KEY,
    GRACEFUL_STOP_GRACE_SEC,
    PLUGIN_ID,
    PLUGIN_VERSION,
    SESSION_CANCEL_COMMAND_ACTIONS,
    STALE_TASK_MESSAGE,
    MessageRecord,
    PipelineReply,
    SendOutcome,
    SessionState,
    Settings,
    now_ts,
)
from .storage import (
    build_sessions_payload,
    load_config_data,
    load_sessions,
    migrate_config_file,
    sync_config_whitelist,
    write_sessions_payload,
)
from .utils import (
    clean_chat_text,
    event_extra,
    event_sender_id,
    event_sender_name,
    event_text,
    event_umo,
    is_admin_event,
    is_at_or_wake_command_event,
    is_explicit_direct_call,
    is_self_message,
    looks_like_reply_request,
    session_group_id,
    session_whitelisted,
    should_ignore_event,
    whitelist_storage_key,
)
from .webapi import UnifiedManagerApi, bind_api_handlers, load_ui_theme, register_web_apis
from .whitelist import WhitelistManager

# ADMIN_COMMAND_ACTIONS 与 GRACEFUL_STOP_GRACE_SEC 统一从 models 导入，
# 避免同名常量在多处定义。


@register(
    PLUGIN_ID,
    "chengzhi-c",
    "精简主动回复插件：白名单会话内，避开 @Bot/命令后自然接话",
    PLUGIN_VERSION,
)
class SelfInitiatedReplyPlugin(Star):
    # 运行时绑定的四个 Web API 处理器（0.9.4 阶段 2.2）。它们不在本类里 def，而是由
    # webapi.bind_api_handlers 在 __init__ 末尾以 partial(...) 挂到实例上，保留历史
    # 方法名供测试与外部以 plugin._api_* 调用（约 30 处调用点，见 AGENTS.md）。
    #
    # 这里只写**裸注解、不赋值**：裸注解不创建类属性，运行时行为与此前完全一致
    # （既不会遮蔽 partial 绑定，也不会让 hasattr 提前为真），纯粹是给读者和编辑器
    # 的声明。此前读者在本类里搜 `_api_post_config` 是搜不到的，只能反查 webapi。
    #
    # 刻意说明它**不是**为 mypy 加的：实测 mypy 对这四处赋值从不报错——
    # ignore_missing_imports 让 Star 解析为 Any，整个子类坍缩成 Any，任何属性名都合法
    # （详见 webapi.py 的 TYPE_CHECKING 块）。所以这组声明的价值是导航，不是检查。
    #
    # 与 bind_api_handlers 实际绑定集合的一致性由
    # tests/test_webapi_fixes.py::test_bound_api_handlers_match_class_declarations 钉住：
    # 那边新增一个 partial 绑定而这里忘了声明（或反之）就变红。
    _api_get_config: Callable[[], Awaitable[dict[str, Any]]]
    _api_post_config: Callable[[], Awaitable[dict[str, Any]]]
    _api_get_ui_theme: Callable[[], Awaitable[dict[str, Any]]]
    _api_post_ui_theme: Callable[[], Awaitable[dict[str, Any]]]

    def __init__(
        self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None
    ) -> None:
        """装配插件：校验宿主 API → 解析路径 → 载入配置与状态 → 组装协作对象。

        顺序有硬依赖：``_validate_agent_api`` 必须最先（宿主不兼容时应在加载期
        就失败，而非运行到一半）；路径解析先于 ``load_sessions``；``settings``
        先于所有以它为入参的协作对象（scheduler/decision/delivery/generation）。
        协作对象共享状态容器的引用而非副本，因此测试替换实例属性后仍指向最新值。

        失败时：宿主 API 缺失直接抛出（拒绝以半可用状态加载）；图片缓存目录
        不可建仅告警并继续（视觉功能降级，主动回复本身不受影响）；状态文件
        损坏由 ``load_sessions`` 内部备份后返回空态，不阻断启动。
        """
        self._validate_agent_api()
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self._config_path, self._storage_path = self._resolve_paths(self.config)
        self._data_path = self._storage_path.parents[2]

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
        self._last_event_at: dict[str, float] = {}
        self._recent_image_events: dict[str, deque[tuple[float, list[ImageInfo]]]] = {}
        self._image_cache_dir = self._storage_path.parent / "image_cache"
        try:
            self._image_cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[%s] image cache directory unavailable: %s", PLUGIN_ID, exc)
        # UI 偏好（主题）：AstrBot 插件页面以 iframe 嵌入 Dashboard，localStorage
        # 不可用，主题必须持久化在后端 JSON 文件（与 state.json 同目录）。
        self._ui_prefs_path = self._storage_path.parent / "ui_prefs.json"
        self._ui_theme = load_ui_theme(self)
        self._image_parsers: dict[str, ImageParser] = {}
        self._image_parser_timeout: float | None = None
        self._whitelist_runtime_umos: dict[str, set[str]] = {}
        self._delay_tasks: dict[str, asyncio.Task[Any]] = {}
        self._running_check_tasks: dict[str, asyncio.Task[Any]] = {}
        # 全局单调代次计数器：白名单移除/重加不会再产生 ABA，旧任务持有的
        # token 永远小于会话当前 token，任何 check 点都会拒绝它。
        self._gate = SessionGate()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False
        self._save_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._admin_file_mtime: float | None = None
        self._admin_ids: set[str] = set()
        self._admin_probe_ts = 0.0  # 探测窗口起点：0 保证首次调用必探
        self._refresh_admin_ids()
        # 调试面板最近裁决（ticket 14）：每会话最近一条裁决的触发/原因，
        # 供 /status 导出；仅存内存，不落盘。
        self._last_decisions: dict[str, dict[str, Any]] = {}
        # 调度职责（延迟检查/巡检/清理）迁入 SessionScheduler（ticket 02）。
        # 状态容器经引用共享：测试对 _delay_tasks 等属性断言保持有效；
        # 回调经 lambda 运行时查找，测试替换实例方法后仍指向最新实现。
        self._scheduler = SessionScheduler(
            settings=self.settings,
            gate=self._gate,
            image_cache_dir=self._image_cache_dir,
            spawn=self._track_background_task,
            should_run=lambda: not self._stopping and self.runtime_enabled,
            state_for=lambda umo: self._state_for(umo),
            check_session=lambda umo, trigger, force, expected_generation: self._check_session(
                umo,
                trigger=trigger,
                force=force,
                expected_generation=expected_generation,
            ),
            clear_cached_event=lambda umo: self._clear_cached_event(umo),
            last_events=self._last_events,
            last_event_at=self._last_event_at,
            recent_image_events=self._recent_image_events,
            whitelist_runtime_umos=self._whitelist_runtime_umos,
            delay_tasks=self._delay_tasks,
            running_check_tasks=self._running_check_tasks,
            background_tasks=self._background_tasks,
        )
        self._scheduler.last_cleanup_at = now_ts()  # 事件清理时间戳

        # 裁决职责（判断模型调用/提示词构建/明确请求窗口/局部闸门）迁入
        # DecisionMaker（ticket 03）。桥接调用经 lambda 运行时查找，测试替换
        # 实例方法或 bridge 后仍指向最新实现。
        self._decision = DecisionMaker(
            settings=self.settings,
            resolve_provider=lambda umo: self.bridge.resolve_provider_id(
                umo, self.settings.judge_provider_id
            ),
            llm_generate=lambda provider_id, prompt: self.bridge.llm_generate(
                provider_id=provider_id,
                prompt=prompt,
                system_prompt=DECISION_SYSTEM_PROMPT,
                temperature=self.settings.decision_temperature,
                max_tokens=DECISION_MAX_TOKENS,
            ),
            read_history=lambda umo, limit: self.bridge.read_astrbot_history(umo, limit=limit),
            build_image_context=lambda umo, enabled, provider_id: self._build_image_context(
                umo, enabled=enabled, provider_id=provider_id
            ),
        )

        # 生成职责（工具边界/策略强制/超时收敛/直发追踪）迁入 GenerationRunner
        # （ticket 04）。runtime 经 getter 动态读取 main 模块的 _AGENT_RUNTIME，
        # 测试替换该全局后仍生效；工具策略经 self 回调运行时查找，测试替换
        # 实例方法后仍命中。
        self._generation = GenerationRunner(
            settings=self.settings,
            context=self.context,
            runtime=lambda: _AGENT_RUNTIME,
            gate=self._gate,
            local_gate=lambda state, force: self._decision.local_gate(state, force=force),
            enforce_policy=lambda req, inherit_tools: self._enforce_final_tool_policy(
                req, inherit_tools
            ),
            call_hook=lambda event, event_type, req: call_event_hook(event, event_type, req),
            grace_stop_sec=lambda: GRACEFUL_STOP_GRACE_SEC,
            background_tasks=self._background_tasks,
            discard_background=self._discard_background_task,
            read_history=lambda umo, limit: self.bridge.read_astrbot_history(umo, limit=limit),
            build_image_context=lambda umo, enabled, provider_id: self._build_image_context(
                umo, enabled=enabled, provider_id=provider_id
            ),
            last_events=self._last_events,
        )

        # 会话协作（事件/时间/图片缓存 + 失效级联单点 + 阶段投影）迁入
        # SessionCoordinator（ticket 07）。状态容器经引用共享，测试直连
        # _last_events 等属性保持原字段名访问。
        self._coordinator = SessionCoordinator(
            events=self._last_events,
            event_at=self._last_event_at,
            images=self._recent_image_events,
            gate=self._gate,
            cancel_delay=lambda umo, force: self._cancel_delay_task(umo, force=force),
            notify_silence=lambda umo: self._scheduler.notify_activity(umo),
        )

        # 投递职责（门卫/钩子/发送分类/UNKNOWN 语义/状态记录）迁入
        # DeliveryRunner（ticket 05）。钩子与 context 发送经 lambda 运行时
        # 查找，测试替换 main.call_event_hook 或插件 context 后仍指向最新实现。
        # local_gate 原先是全部注入回调里唯一的绑定方法（构造期即解析），迫使
        # DecisionMaker 必须早于 GenerationRunner 与 DeliveryRunner 构造，否则
        # 重排顺序会静默 AttributeError。现已与邻居统一为 lambda 运行时查找，
        # 该顺序约束随之解除——两处注入点（此处与 GenerationRunner）都要保持
        # lambda 形态，只改一处约束依旧成立。
        self._delivery = DeliveryRunner(
            settings=self.settings,
            gate=self._gate,
            local_gate=lambda state, force: self._decision.local_gate(state, force=force),
            last_events=self._last_events,
            call_hook=lambda event, event_type: call_event_hook(event, event_type),
            context_send=lambda umo, message: self.context.send_message(umo, message),
            send_reply=lambda umo, reply, expected_generation: self._send_reply(
                umo, reply, expected_generation=expected_generation
            ),
            save_storage=lambda: self._save_storage(),
            runtime=lambda: _AGENT_RUNTIME,
        )

        # 白名单职责（替换/增删/双写回滚）迁入 WhitelistManager（ticket 06）。
        # 状态容器经引用共享（sessions/runtime_umos），失效与代次清理经回调。
        self._whitelist = WhitelistManager(
            settings=self.settings,
            sync_whitelist=lambda: self._sync_whitelist(),
            save_storage=lambda: self._save_storage(),
            ensure_state=lambda key: self._state_for(key),
            invalidate=lambda umo: self._invalidate_session(umo),
            prune=lambda umo: self._prune_session(umo),
            sessions=self.sessions,
            tracked_umos=lambda: (
                set(self._last_events)
                | set(self._delay_tasks)
                | set(self._running_sessions)
                | set(self._session_locks)
            ),
            runtime_umos=self._whitelist_runtime_umos,
        )

        self._save_storage_sync()
        try:
            # Reload/startup is also a maintenance boundary: remove old orphaned
            # cache files immediately instead of waiting for the first interval.
            self._cleanup_image_sources(now=now_ts())
        except Exception as exc:
            logger.warning("[%s] startup image cache cleanup failed: %s", PLUGIN_ID, exc)
        self._ensure_patrol_task()
        self._ensure_image_cleanup_task()
        logger.info(
            "[%s] v%s enabled=%s whitelist=%d message_trigger=%s patrol_trigger=%s"
            " pipeline_mode=true",
            PLUGIN_ID,
            PLUGIN_VERSION,
            self.runtime_enabled,
            len(self.settings.whitelist),
            self.settings.enabled_message_trigger,
            self.settings.enabled_patrol_trigger,
        )
        logger.info(
            "[%s] vision judge=%s main=%s skip_stickers=%s provider=%s judge_provider=%s",
            PLUGIN_ID,
            self.settings.vision_judge_enabled,
            self.settings.vision_main_enabled,
            self.settings.vision_skip_stickers,
            self.settings.vision_provider_id or "<current>",
            self.settings.vision_judge_provider_resolved or "<current>",
        )
        bind_api_handlers(self)
        register_web_apis(self)

    @staticmethod
    def _validate_agent_api() -> None:
        _AGENT_RUNTIME.validate()

    def _install_agent_tool_boundary(
        self, event: AstrMessageEvent, inherit_tools: bool
    ) -> dict[str, Any]:
        """Limit a proactive run to built-in low-side-effect tools by default."""
        return self._generation.install_agent_tool_boundary(event, inherit_tools)

    @staticmethod
    def _restore_agent_tool_boundary(event: AstrMessageEvent, state: dict[str, Any]) -> None:
        GenerationRunner.restore_agent_tool_boundary(event, state)

    @staticmethod
    def _resolve_paths(config_obj: Any) -> tuple[Path, Path]:
        """Resolve paths from AstrBot's configured root, with a legacy fallback."""
        configured_path = getattr(config_obj, "config_path", None)
        if configured_path:
            config_path = Path(str(configured_path)).expanduser()
        elif callable(get_astrbot_config_path):
            config_path = (
                Path(str(get_astrbot_config_path())).expanduser() / f"{PLUGIN_ID}_config.json"
            )
        else:
            config_path = Path.home() / ".astrbot" / "data" / "config" / f"{PLUGIN_ID}_config.json"

        if callable(get_astrbot_plugin_data_path):
            plugin_data_path = Path(str(get_astrbot_plugin_data_path())).expanduser() / PLUGIN_ID
        else:
            plugin_data_path = config_path.parent.parent / "plugin_data" / PLUGIN_ID
        return config_path, plugin_data_path / "state.json"

    def _refresh_admin_ids(self) -> set[str]:
        """时间窗 + mtime 双缓存热读管理员列表，运行期改管理员窗口内生效。"""
        now = now_ts()
        if now - self._admin_probe_ts < ADMIN_REFRESH_WINDOW_SEC:
            return self._admin_ids
        self._admin_probe_ts = now
        path = self._data_path / "cmd_config.json"
        try:
            if path.exists():
                mtime = path.stat().st_mtime
                if mtime == self._admin_file_mtime:
                    return self._admin_ids
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                admins = data.get("admins_id", []) if isinstance(data, dict) else []
                self._admin_ids = {str(item).strip() for item in admins if str(item).strip()}
                self._admin_file_mtime = mtime
        except Exception as exc:
            logger.debug("[%s] load admins failed path=%s error=%s", PLUGIN_ID, path, exc)
        return self._admin_ids

    def _state_for(self, umo: str) -> SessionState:
        state = self.sessions.get(umo)
        if state is None:
            legacy_key = session_group_id(umo)
            if legacy_key:
                state = self.sessions.pop(legacy_key, None)
        if state is None:
            state = SessionState(recent=deque(maxlen=self.settings.recent_message_limit))
            self.sessions[umo] = state
        else:
            self.sessions[umo] = state
            # deque 的 maxlen 是构造期常量：recent_message_limit 热更新（设置页
            # 保存不重载插件）对存量会话不生效，调大后新上限永不兑现。此处按
            # 读取路径惰性重建，无需 apply 侧显式遍历全部会话。
            limit = self.settings.recent_message_limit
            if state.recent.maxlen != limit:
                state.recent = deque(state.recent, maxlen=limit)
        # 所有读取路径统一刷新跨天计数（幂等），避免 status/持久化显示昨日数据
        state.refresh_day()
        return state

    def _save_storage_sync(self) -> None:
        if not self._save_storage_snapshot():
            logger.warning("[%s] initial state save failed path=%s", PLUGIN_ID, self._storage_path)

    def _save_storage_snapshot(self) -> bool:
        try:
            payload = build_sessions_payload(
                self.sessions,
                self.settings.whitelist,
                self.settings.recent_message_limit,
            )
            return write_sessions_payload(self._storage_path, payload)
        except Exception as exc:
            logger.error("[%s] failed to prepare state snapshot: %s", PLUGIN_ID, exc, exc_info=True)
            return False

    async def _save_storage(self) -> None:
        async with self._save_lock:
            payload = build_sessions_payload(
                self.sessions,
                self.settings.whitelist,
                self.settings.recent_message_limit,
            )
            write_task = asyncio.create_task(
                asyncio.to_thread(write_sessions_payload, self._storage_path, payload)
            )
            try:
                success = await asyncio.shield(write_task)
            except asyncio.CancelledError:
                # Do not let a cancelled owner leave an old snapshot writing
                # after terminate() has started its final save.
                success = await write_task
                if not success:
                    raise OSError(f"状态文件写入失败：{self._storage_path}") from None
                raise
            if not success:
                raise OSError(f"状态文件写入失败：{self._storage_path}")

    def _sync_whitelist(self) -> None:
        if not sync_config_whitelist(self._config_path, self.config, self.settings):
            raise OSError(f"配置文件写入失败：{self._config_path}")

    async def _persist_enabled(self, enabled: bool) -> None:
        """把 ``/on`` ``/off`` 的开关落盘，使其跨宿主重启保持（决策 5）。

        原先只改 ``runtime_enabled`` 这个纯内存量，重启后回落到持久 ``enabled``：
        用户打完 ``/off`` 以为「别再主动说话了」，宿主一重启插件又开始发言，而
        用户不会知道要再打一次。这是静默违背用户意图，故改为双写。

        失败按 §6 白名单变更的同一套纪律处理：内存回滚 → 重写 → 仍失败告警并
        上抛，不留下「内存已关、磁盘仍开」的中间态。``runtime_enabled`` 保留为
        独立字段而非并进 ``settings.enabled``，因为 webapi 的 GET config 要能
        区分两者（``test_off_keeps_persisted_enabled_in_config`` 看守该契约）。
        """
        old_enabled = self.settings.enabled
        old_runtime = self.runtime_enabled
        self.settings.enabled = enabled
        self.runtime_enabled = enabled
        try:
            self._sync_whitelist()
        except Exception:
            self.settings.enabled = old_enabled
            self.runtime_enabled = old_runtime
            try:
                self._sync_whitelist()
            except Exception as rollback_exc:
                logger.error(
                    "[%s] enabled=%s rollback persistence failed: %s",
                    PLUGIN_ID,
                    enabled,
                    rollback_exc,
                )
            raise

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """全事件入口：指令分流 → 白名单过滤 → 记录上下文 → 排延迟检查。

        顺序是安全边界，不可重排：指令已处理标记与指令分流必须在白名单判定
        之前（指令在非白名单会话也要能用），忽略判定必须在写入 recent 之前
        （否则被忽略的发送者内容仍进上下文）。

        失败时：本函数不向上抛异常——事件管线的其他插件不应被本插件的故障
        阻断。图片快照失败降级为「本次不带图」（debug 日志），提取到空列表
        只记 debug；任一早退分支都会先 ``_invalidate_session`` 推进代次，
        使在途的延迟检查任务自然失效，不留孤儿回复。
        """
        text = event_text(event).strip()
        if event_extra(event, COMMAND_HANDLED_KEY, False):
            return
        parsed = parse_command_text(text)
        if parsed is not None and self._is_command_entry(event, text):
            await self._handle_inline_command(event, parsed)
            return

        if self._stopping or not self.runtime_enabled or event.is_stopped():
            return
        umo = event_umo(event)

        if not session_whitelisted(umo, self.settings.whitelist):
            return
        state_key = whitelist_storage_key(umo)
        self._whitelist_runtime_umos.setdefault(state_key, set()).add(umo)
        group_id = session_group_id(umo)
        if group_id:
            self._whitelist_runtime_umos.setdefault(group_id, set()).add(umo)

        clean_text = clean_chat_text(text)
        # Compute Vision eligibility once and pass it through the generic event
        # gate; the capture path below reuses the same decision.
        has_images = self.settings.vision_enabled and ImageExtractor.has_images(
            event,
            skip_stickers=self.settings.vision_skip_stickers,
        )
        if self._should_ignore_event(event, text, vision_has_images=has_images):
            self._invalidate_session(umo)
            if not is_self_message(event) and is_explicit_direct_call(event, text):
                state = self._state_for(state_key)
                state.last_active_at = now_ts()
                state.last_active_sender_id = event_sender_id(event)
            return

        if not clean_text and not has_images:
            self._invalidate_session(umo)
            return
        if not clean_text:
            # Image-only events remain observable when Vision is explicitly enabled.
            clean_text = "[图片]"

        generation = self._gate.advance(umo)
        active_at = now_ts()
        state = self._state_for(state_key)
        state.last_active_at = active_at
        state.last_active_sender_id = event_sender_id(event)
        state.recent.append(
            MessageRecord(
                role="user",
                name=event_sender_name(event),
                sender_id=state.last_active_sender_id,
                text=clean_text,
                at=active_at,
            )
        )

        # Keep one recent event for message-triggered and patrol checks. The
        # timestamp lets cleanup retain events that are still useful to a task.
        self._coordinator.record_event(umo, event, active_at)
        if has_images:
            # Capture only the amount that a later Vision request can consume.
            # Local host files are snapshotted below while the event is alive;
            # slow CDN downloads remain in a tracked background task.
            images = ImageExtractor.extract_images(
                event,
                sender_id=event_sender_id(event),
                timestamp=active_at,
                skip_stickers=self.settings.vision_skip_stickers,
            )[: max(1, int(self.settings.vision_max_images))]
            if images:
                # AstrBot 的归一化 Image 可能指向只在当前事件阶段有效的
                # 临时文件。先复制宿主本地源，再把远程下载和索引写入放到后台。
                parser = self._get_image_parser()
                if parser is not None:
                    try:
                        await parser.snapshot_local_sources(images, max_concurrent=2)
                    except Exception as exc:
                        logger.debug("[%s] local image snapshot stage failed: %s", PLUGIN_ID, exc)
                self._track_background_task(
                    self._prepare_images_for_session(
                        umo,
                        generation=generation,
                        active_at=active_at,
                        images=images,
                    )
                )
            else:
                logger.debug(
                    "[%s] has_images=True but extract_images returned empty for umo=%s",
                    PLUGIN_ID,
                    umo,
                )
        self._cleanup_old_events_if_needed()

        if self.settings.enabled_message_trigger:
            trigger = (
                "reply_request"
                if looks_like_reply_request(clean_text, self.settings.bot_aliases)
                else "message_delay"
            )
            delay = self._scheduler.message_trigger_delay(trigger)
            self._schedule_delayed_check(
                umo,
                delay_sec=delay,
                trigger=trigger,
                force=False,
                generation=generation,
            )

    @staticmethod
    def _is_command_entry(event: AstrMessageEvent, text: str) -> bool:
        """Require an explicit command entry before consuming the event.

        Without this gate any group member could send the bare word
        ``selfreply`` and make the bot emit the whole help text and then call
        ``stop_event()``, swallowing the message for every other plugin. A
        leading slash or an actual mention/wake word is required; anything else
        is treated as ordinary chat text.
        """
        if str(text or "").lstrip().startswith("/"):
            return True
        return is_at_or_wake_command_event(event) or is_explicit_direct_call(event, text)

    def _should_ignore_event(
        self,
        event: AstrMessageEvent,
        text: str,
        *,
        vision_has_images: bool,
    ) -> bool:
        """忽略判定（自消息/命令/纯图无识图/忽略名单/直接点名）。（委托壳，逻辑在 utils.py）"""
        return should_ignore_event(
            event,
            text,
            vision_has_images=vision_has_images,
            ignored_sender_ids=self.settings.ignored_sender_ids,
        )

    # 只读视图：数据归属 SessionGate，以下 property 供既有调用点与测试
    # 以原字段名访问，避免同步迁移动辄数十处引用面。回滚整表覆盖
    # 经 SessionGate.restore 封装，不再暴露 setter；读侧返回只读视图
    # （MappingProxyType / frozenset），外部误写会在运行时直接抛错。
    @property
    def _session_generation(self) -> MappingProxyType[str, int]:
        return self._gate.generation_view

    @property
    def _running_sessions(self) -> frozenset[str]:
        return self._gate.running_sessions_view

    @property
    def _session_locks(self) -> MappingProxyType[str, asyncio.Lock]:
        return self._gate.locks_view

    @property
    def _patrol_task(self) -> asyncio.Task[Any] | None:
        return self._scheduler.patrol_task

    def _track_background_task(self, coro: Any) -> asyncio.Task[Any] | None:
        if self._stopping:
            # Spawn barrier: once terminate() has begun, no new background
            # work may start; the coroutine is closed instead of run.
            try:
                coro.close()
            except Exception:
                # close() 对已开始执行或已关闭的协程会抛 RuntimeError；此处目的仅是
                # 避免 "coroutine was never awaited" 警告，关不掉也不影响停止语义。
                pass
            return None
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._discard_background_task)
        return task

    def _discard_background_task(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)

    async def _prepare_images_for_session(
        self,
        umo: str,
        *,
        generation: int,
        active_at: float,
        images: list[ImageInfo],
    ) -> None:
        try:
            parser = self._get_image_parser()
            if parser is None:
                return
            prepared = await asyncio.wait_for(
                parser.prepare_batch(images, max_concurrent=2),
                timeout=max(5.0, min(30.0, float(self.settings.vision_timeout_sec) * 2)),
            )
            # A stale freeze must not mutate the current session's image index.
            if self._stopping or not self._gate.is_current(umo, generation):
                return
            cached_images = [image for image, ok in zip(images, prepared, strict=True) if ok]
            if not cached_images:
                logger.warning(
                    "[%s] extracted %s images but none could be frozen for umo=%s",
                    PLUGIN_ID,
                    len(images),
                    umo,
                )
                return
            self._coordinator.capture_images(umo, active_at, cached_images)
            logger.debug(
                "[%s] captured %s/%s images into local vision cache for umo=%s",
                PLUGIN_ID,
                len(cached_images),
                len(images),
                umo,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("[%s] image capture timed out for umo=%s", PLUGIN_ID, umo)
        except Exception as exc:
            logger.warning("[%s] image capture failed for umo=%s error=%s", PLUGIN_ID, umo, exc)

    def _cancel_delay_task(self, umo: str, *, force: bool = False) -> None:
        self._scheduler.cancel_delay(umo, force=force)

    def _clear_cached_event(self, umo: str) -> None:
        """清缓存事件（委托壳，逻辑在 session_coordinator.py）。"""
        self._coordinator.clear(umo)

    def _invalidate_session(self, umo: str, *, force_cancel: bool = False) -> int:
        """会话失效单点入口：代次推进 + 延迟取消 + 协作资源级联清理。"""
        return self._coordinator.invalidate(umo, force_cancel=force_cancel)

    def _prune_session(self, umo: str) -> None:
        """会话回收单点：代次/锁/运行标记/最近裁决 + 会话状态内存回收。

        白名单移除（WhitelistManager.replace）与非白名单 force-check 的
        finally 共用本入口；磁盘由 build_sessions_payload 写盘时过滤非白名单
        条目，重启后不会复活（0.8.8 单点化，此前 sessions 回收散在两处）。
        """
        self._gate.prune(umo)
        self._last_decisions.pop(umo, None)
        self.sessions.pop(umo, None)
        self.sessions.pop(session_group_id(umo), None)

    def _cancel_event_session(self, event: AstrMessageEvent) -> None:
        umo = event_umo(event)
        if umo and session_whitelisted(umo, self.settings.whitelist):
            self._invalidate_session(umo, force_cancel=True)

    def _schedule_delayed_check(
        self,
        umo: str,
        *,
        delay_sec: int | None,
        trigger: str,
        force: bool,
        generation: int | None = None,
    ) -> None:
        self._scheduler.schedule_delayed_check(
            umo,
            delay_sec=delay_sec,
            trigger=trigger,
            force=force,
            generation=generation,
        )

    def _cleanup_image_sources(self, *, now: float | None = None) -> int:
        """清理过期图片索引和插件临时缓存，保护仍在有效窗口内的源。"""
        return self._scheduler.cleanup_image_sources(now=now)

    async def _run_image_cleanup(self) -> int:
        """Serialize manual and periodic cleanup requests."""
        return await self._scheduler.run_image_cleanup()

    def _cleanup_old_events_if_needed(self) -> None:
        """定期清理没有任务或运行中的陈旧事件。"""
        self._scheduler.cleanup_events_if_needed()

    def _ensure_image_cleanup_task(self) -> None:
        self._scheduler.ensure_image_cleanup()

    def _ensure_patrol_task(self) -> None:
        self._scheduler.ensure_patrol()

    async def _check_session(
        self,
        umo: str,
        *,
        trigger: str,
        force: bool,
        expected_generation: int | None = None,
    ) -> str:
        lock = self._gate.lock_for(umo)
        if lock.locked():
            return "已有判断任务在运行。"
        async with lock:
            return await self._check_session_locked(
                umo,
                trigger=trigger,
                force=force,
                expected_generation=expected_generation,
            )

    async def _check_session_locked(
        self,
        umo: str,
        *,
        trigger: str,
        force: bool,
        expected_generation: int | None = None,
    ) -> str:
        """单会话检查主链：闸门 → 本地裁决 → 模型裁决 → 生成 → 投递，返回结果文案。

        调用方已持有该会话的检查锁（故名 locked）。代次基线：无显式代次的调用
        （patrol/manual）会绑定任务开始时的世代，防止会话移出白名单后重加（ABA）
        时旧任务越过代次门复活发送。

        失败时：任一阶段返回字符串即为终止原因（闸门拒绝/本地跳过/裁决否决），
        直接回传给调用方而不抛出。``finally`` 无条件 ``unmark_running``——运行标记
        泄漏会让该会话后续所有检查永久排队。工具已直发但最终文本重复时丢弃文本，
        避免同一句话在群里出现两次。
        """
        guard = self._session_check_guard(umo, force=force, expected_generation=expected_generation)
        if guard is not None:
            return guard
        if expected_generation is None:
            # 任务代次基线：无代次调用（patrol/手动）且会话已有代次记录时，
            # 绑定任务开始时的世代，防止会话移除后重加（ABA）时旧任务越过
            # 代次门复活发送。无记录（0）的会话保持 None 放行（兼容历史语义）。
            baseline = self._gate.current(umo)
            if baseline:
                expected_generation = baseline
        state = self._state_for(whitelist_storage_key(umo))
        observed_active_at = state.last_active_at

        state.refresh_day()
        gate = self._decision.local_gate(state, force=force)
        if gate:
            logger.debug("[%s] skip session=%s trigger=%s reason=%s", PLUGIN_ID, umo, trigger, gate)
            return gate

        self._gate.mark_running(umo)
        try:
            decision = await self._decide_session_reply(
                umo,
                state,
                trigger=trigger,
                force=force,
                expected_generation=expected_generation,
            )
            if isinstance(decision, str):
                return decision

            pipeline_reply = await self._generate_reply_via_pipeline(
                umo,
                state,
                expected_generation=expected_generation,
                force=force,
            )
            reply = pipeline_reply.text.strip()
            direct_send_count = pipeline_reply.direct_send_count
            if reply and pipeline_reply.direct_texts:
                normalized_reply = re.sub(r"\s+", " ", reply).strip()
                if any(
                    normalized_reply == re.sub(r"\s+", " ", text).strip()
                    for text in pipeline_reply.direct_texts
                ):
                    logger.info(
                        "[%s] suppress duplicate final text after tool direct send session=%s",
                        PLUGIN_ID,
                        umo,
                    )
                    reply = ""
            if not reply and not direct_send_count:
                return "管线未生成内容。"

            return await self._deliver_session_reply(
                umo,
                state,
                reply,
                direct_send_count,
                expected_generation=expected_generation,
                observed_active_at=observed_active_at,
                force=force,
                trigger=trigger,
            )
        finally:
            self._gate.unmark_running(umo)

    def _session_check_guard(
        self, umo: str, *, force: bool, expected_generation: int | None
    ) -> str | None:
        """会话级前置门卫：全部通过返回 None，否则返回跳过原因。"""
        if self._stopping or (not force and not self.runtime_enabled):
            return "插件未启用。"
        if not force and not session_whitelisted(umo, self.settings.whitelist):
            return "会话不在主动回复白名单。"
        if not self._gate.is_current(umo, expected_generation):
            return STALE_TASK_MESSAGE
        if not force and not self._last_events.get(umo):
            return "没有可用的最近消息事件。"
        if self._gate.is_running(umo):
            return "已有判断任务在运行。"
        return None

    def _record_decision(self, umo: str, trigger: str, *, should_reply: bool, reason: str) -> None:
        """调试面板最近裁决记录（仅内存，随 _prune_session 回收）。"""
        self._last_decisions[umo] = {
            "at": round(now_ts(), 3),
            "trigger": trigger,
            "should_reply": should_reply,
            "reason": reason,
        }

    async def _decide_session_reply(
        self,
        umo: str,
        state: SessionState,
        *,
        trigger: str,
        force: bool,
        expected_generation: int | None,
    ) -> dict[str, Any] | str:
        """产生一次判断：通过返回 decision dict，早退返回跳过原因。"""
        decision = await self._decision.decide(umo, state, trigger=trigger, force=force)
        if isinstance(decision, str):
            self._record_decision(umo, trigger, should_reply=False, reason=decision)
            return decision

        if not self._gate.is_current(umo, expected_generation):
            return STALE_TASK_MESSAGE
        self._record_decision(
            umo,
            trigger,
            should_reply=bool(decision.get("should_reply")),
            reason=str(decision.get("reason") or ""),
        )
        logger.debug(
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
        return decision

    async def _deliver_session_reply(
        self,
        umo: str,
        state: SessionState,
        reply: str,
        direct_send_count: int,
        *,
        expected_generation: int | None,
        observed_active_at: float | None,
        force: bool,
        trigger: str,
    ) -> str:
        """发送前门卫与发送状态机；返回结果消息。（委托壳，逻辑在 delivery.py）"""
        return await self._delivery.deliver_reply(
            umo,
            state,
            reply,
            direct_send_count,
            expected_generation=expected_generation,
            observed_active_at=observed_active_at,
            force=force,
            trigger=trigger,
        )

    async def _generate_reply_via_pipeline(
        self,
        umo: str,
        state: SessionState,
        *,
        expected_generation: int | None = None,
        force: bool = False,
    ) -> PipelineReply:
        """Run AstrBot's main Agent and account for tool-side direct sends."""
        return await self._generation.generate(
            umo,
            state,
            expected_generation=expected_generation,
            force=force,
        )

    def _enforce_final_tool_policy(self, req: Any, inherit_tools: bool) -> bool:
        """Enforce the proactive tool allowlist; abort the run when unverifiable."""
        return self._generation.enforce_final_tool_policy(req, inherit_tools)

    async def _send_reply(
        self, umo: str, reply: str, *, expected_generation: int | None = None
    ) -> SendOutcome:
        """Send one proactive reply without retrying an unknown submission.

        （委托壳，逻辑在 delivery.py）
        """
        return await self._delivery.send_reply(umo, reply, expected_generation=expected_generation)

    def _get_image_parser(self, provider_id: str = "") -> ImageParser | None:
        """Return a cached Vision parser for one provider, if Vision is enabled.

        Parsers are cached per resolved provider ID so that the judge and main
        paths can use different Vision models. When both paths resolve to the
        same provider they share one instance, and therefore one description
        cache, so an image is only described once.

        Args:
            provider_id: Resolved Vision provider ID. Empty means the adapter
                falls back to the current session model.

        Returns:
            A parser instance, or ``None`` when no Vision path is enabled.
        """
        if not self.settings.vision_enabled:
            return None
        timeout = float(self.settings.vision_timeout_sec)
        # 超时值变化时整体重建，避免旧实例带着过期的超时设置
        if self._image_parser_timeout != timeout:
            self._image_parsers.clear()
            self._image_parser_timeout = timeout
        key = str(provider_id or "").strip()
        parser = self._image_parsers.get(key)
        if parser is None:
            parser = ImageParser(
                self.bridge,
                provider_id=key,
                recorder_bridge=get_recorder_bridge(self.context),
                timeout_sec=timeout,
                source_cache_dir=self._image_cache_dir,
                # 宿主 <data> 根：合法适配器写的裸绝对路径图片都在它下面
                # （wecom <data>/temp、webchat <data>/webchat）。阶段 1.1 起
                # 本地读取只认 allowlist，不再信提取层的可信推断。
                data_root=self._data_path,
            )
            self._image_parsers[key] = parser
        return parser

    def _recent_images_for(self, umo: str) -> list[ImageInfo]:
        """Return distinct, recent image references for one session.

        Image event objects are intentionally short-lived and never persisted.
        （委托壳，逻辑在 session_coordinator.py）
        """
        return self._coordinator.images_for(
            umo,
            vision_age_sec=float(self.settings.vision_image_age_sec),
            vision_skip_stickers=self.settings.vision_skip_stickers,
            vision_max_images=self.settings.vision_max_images,
        )

    async def _build_image_context(self, umo: str, *, enabled: bool, provider_id: str = "") -> str:
        """Describe recent images for prompt context without persisting the result.

        Args:
            umo: Session UMO.
            enabled: Whether this path's Vision is enabled.
            provider_id: Vision provider for this path. Empty lets the adapter
                fall back to the current session model.

        Returns:
            Formatted image context string or empty.
        """
        if not enabled:
            return ""
        parser = self._get_image_parser(provider_id)
        if parser is None:
            return ""
        images = self._recent_images_for(umo)
        if not images:
            return ""
        descriptions = await parser.parse_batch(
            images,
            umo=umo,
            max_concurrent=min(2, self.settings.vision_max_images),
        )
        return format_image_context(descriptions)

    def _replace_whitelist(self, whitelist: set[str]) -> None:
        """整表替换白名单，并回收被移出会话的内存状态。（委托壳，逻辑在 whitelist.py）"""
        self._whitelist.replace(whitelist)

    async def _add_whitelist_session(self, umo: str) -> bool:
        async with self._config_lock:
            if self._stopping:
                raise RuntimeError("插件正在关闭，无法修改白名单")
            return await self._whitelist.add(umo)

    async def _remove_whitelist_session(self, umo: str) -> bool:
        async with self._config_lock:
            if self._stopping:
                raise RuntimeError("插件正在关闭，无法修改白名单")
            return await self._whitelist.remove(umo)

    async def _handle_inline_command(
        self, event: AstrMessageEvent, parsed: tuple[str, str]
    ) -> None:
        action, arg = parsed
        self._set_command_handled(event)
        if action in ADMIN_COMMAND_ACTIONS and not is_admin_event(event, self._refresh_admin_ids()):
            await self._send_command_text(event, "没有权限执行该主动回复管理指令。")
            return
        # 越权拒绝先行：写操作才取消在途回复，只读动作不打断进行中的检查。
        if action in SESSION_CANCEL_COMMAND_ACTIONS:
            self._cancel_event_session(event)
        await self._send_command_text(event, await self._command_text(event, action, arg))

    async def _command_text(self, event: AstrMessageEvent, action: str, arg: str = "") -> str:
        """把已解析的指令动作分派为回显文本（help/status/list/add/remove/check/on/off/debug）。

        只读动作（help/status/list/debug）不要求会话可识别；写动作在 umo 为空时
        直接回「无法识别当前会话」。``on``/``off`` 在 ``_config_lock`` 内改运行开关，
        避免与配置热重载交错。

        失败时：``check`` 是唯一有副作用的分支，它强制检查后在 ``finally`` 里回收
        缓存事件，并对非白名单会话额外 ``_prune_session``（force 检查可能发生在
        白名单外，不回收会留下代次/锁/运行标记）。未知 action 回落 help 而非报错。
        """
        umo = event_umo(event)
        if action == "help":
            return help_text()
        if action == "status":
            state = self._state_for(whitelist_storage_key(umo)) if umo else SessionState()
            return status_text(self.settings, event, state, self.runtime_enabled)
        if action == "list":
            return list_text(self.settings)
        if not umo:
            return "无法识别当前会话。"
        if action == "add":
            added = await self._add_whitelist_session(umo)
            return (
                f"已将当前会话加入主动回复白名单：{umo}"
                if added
                else f"当前会话已在主动回复白名单中：{umo}"
            )
        if action == "remove":
            removed = await self._remove_whitelist_session(umo)
            return (
                f"已移出主动回复白名单：{umo}"
                if removed
                else f"当前会话本不在主动回复白名单：{umo}"
            )
        if action == "check":
            # 立即强制检查：取消待执行的延迟检查并清空旧缓存（写操作语义）。
            generation = self._invalidate_session(umo)
            self._coordinator.record_event(umo, event, now_ts())
            state = self._state_for(whitelist_storage_key(umo))
            text = clean_chat_text(arg or strip_command_prefix(event_text(event)))
            if text:
                state.last_active_at = now_ts()
                state.last_active_sender_id = event_sender_id(event)
                state.recent.append(
                    MessageRecord(
                        role="user",
                        name=event_sender_name(event),
                        text=text,
                        at=state.last_active_at,
                    )
                )
            try:
                result = await self._check_session(
                    umo,
                    trigger="manual",
                    force=True,
                    expected_generation=generation,
                )
            finally:
                if self._last_events.get(umo) is event:
                    self._clear_cached_event(umo)
                # force 检查可能发生在非白名单会话：结束后统一回收
                # 代次/锁/运行标记与 release 事件
                if not session_whitelisted(umo, self.settings.whitelist):
                    self._prune_session(umo)
            return f"主动回复检查结果：{result}"
        if action == "on":
            async with self._config_lock:
                await self._persist_enabled(True)
                self._ensure_patrol_task()
                self._ensure_image_cleanup_task()
            return "主动回复插件已启用（重启后保持）。"
        if action == "off":
            async with self._config_lock:
                await self._persist_enabled(False)
                self._cancel_delay_tasks()
                await self._stop_patrol_task()
            return "主动回复插件已暂停（重启后保持）。"
        if action == "debug":
            return debug_text(
                self.settings,
                event,
                ignored_sender=event_sender_id(event) in self.settings.ignored_sender_ids,
            )
        return help_text()

    async def _send_command_text(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain().message(text))
        except Exception as exc:
            logger.debug("[%s] inline command send failed: %s", PLUGIN_ID, exc)
            try:
                event.set_result(event.plain_result(text))
            except Exception:
                # 主动 send 已失败，set_result 是最后一层兜底；两条路都不通说明事件
                # 已被宿主终结，此时无处投递指令回显，只能放弃（丢回显 > 抛异常打断管道）。
                pass
        try:
            event.stop_event()
        except Exception:
            # 事件可能已被宿主或上游插件终结，重复 stop 无意义；指令回显已完成，
            # 此处失败不改变指令的执行结果。
            pass

    # 注意：permission_type 必须在 command_group 内层。真实宿主（4.26.8/4.27.0
    # 已验证）的 register_permission_type 会对被装饰对象调用 get_handler_full_name（访问
    # __name__），而 command_group 返回的 RegisteringCommandable 没有 __name__；
    # 顺序反了插件加载即报 AttributeError（0.7.15 曾因此线上安装失败）。
    @filter.command_group("selfreply")
    @permission_type(PermissionType.ADMIN)
    async def selfreply(self, event: AstrMessageEvent) -> CommandReply:
        """主动回复：查看指令说明。"""
        self._set_command_handled(event)
        yield event.plain_result(help_text())

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("help", alias={"h"})
    async def selfreply_help(self, event: AstrMessageEvent) -> CommandReply:
        """帮助：显示主动回复指令说明。"""
        self._set_command_handled(event)
        yield event.plain_result(help_text())

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("status", alias={"stat"})
    async def selfreply_status(self, event: AstrMessageEvent) -> CommandReply:
        """状态：查看运行状态、判断模型和白名单信息。"""
        self._set_command_handled(event)
        umo = event_umo(event)
        state = self._state_for(whitelist_storage_key(umo)) if umo else SessionState()
        yield event.plain_result(status_text(self.settings, event, state, self.runtime_enabled))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("list", alias={"ls", "whitelist"})
    async def selfreply_list(self, event: AstrMessageEvent) -> CommandReply:
        """列表：查看主动回复白名单。"""
        self._set_command_handled(event)
        yield event.plain_result(list_text(self.settings))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("add")
    async def selfreply_add(self, event: AstrMessageEvent) -> CommandReply:
        """加入：将当前会话加入主动回复白名单。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "add"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("remove", alias={"rm", "del", "delete"})
    async def selfreply_remove(self, event: AstrMessageEvent) -> CommandReply:
        """移除：将当前会话移出主动回复白名单。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "remove"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("check", alias={"test"})
    async def selfreply_check(self, event: AstrMessageEvent) -> CommandReply:
        """检查：手动测试一次主动回复，可附带测试内容。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "check"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("on", alias={"enable", "start"})
    async def selfreply_on(self, event: AstrMessageEvent) -> CommandReply:
        """开启：启用主动回复运行，跨宿主重启保持（决策 5）。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "on"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("off", alias={"disable", "pause", "stop"})
    async def selfreply_off(self, event: AstrMessageEvent) -> CommandReply:
        """关闭：暂停主动回复运行，跨宿主重启保持（决策 5）。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "off"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("debug", alias={"diag", "diagnose"})
    async def selfreply_debug(self, event: AstrMessageEvent) -> CommandReply:
        """调试：查看当前会话、发送者和触发识别信息。"""
        self._set_command_handled(event)
        yield event.plain_result(
            debug_text(
                self.settings,
                event,
                ignored_sender=event_sender_id(event) in self.settings.ignored_sender_ids,
            )
        )

    def _set_command_handled(self, event: AstrMessageEvent) -> None:
        try:
            event.set_extra(COMMAND_HANDLED_KEY, True)
        except Exception:
            # 老宿主可能未实现 set_extra。标记丢失只会让同一事件在后续 on_message
            # 少一层去重保护（仍有指令前缀判定兜底），不足以让指令本身失败。
            pass

    def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        for task in list(self._background_tasks):
            if task is current or task.done():
                continue
            task.cancel()

    def _cancel_delay_tasks(self) -> None:
        sessions = (
            set(self._delay_tasks) | set(self._running_sessions) | set(self._running_check_tasks)
        )
        for umo in sessions:
            self._invalidate_session(umo, force_cancel=True)
        self._delay_tasks.clear()
        self._cancel_background_tasks()

    async def _wait_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task for task in list(self._background_tasks) if task is not current and not task.done()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.difference_update(tasks)

    async def _stop_patrol_task(self) -> None:
        await self._scheduler.stop_patrol()

    async def terminate(self) -> None:
        self._stopping = True
        # 最终落盘必须与任务收敛同处 _config_lock 内：Dashboard 的 POST /config
        # 在同一把锁下改 settings/whitelist（webapi._apply_config_updates），
        # 锁外快照会读到半更新的白名单，把刚加/刚删的会话错误过滤掉。
        # 锁序恒为 _config_lock → _save_lock（两条路径一致），无反向持有。
        async with self._config_lock:
            self._cancel_delay_tasks()
            await self._stop_patrol_task()
            await self._wait_background_tasks()
            self._coordinator.reset_all()
            try:
                # 记录点已逐次落盘，此处兜底覆盖「最后一次记录之后又有内存
                # 变更」（白名单回收、跨天刷新）。
                await self._save_storage()
            except Exception as exc:
                logger.warning("[%s] final state save failed: %s", PLUGIN_ID, exc)
        logger.info("[%s] terminated", PLUGIN_ID)
