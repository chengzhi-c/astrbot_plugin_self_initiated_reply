"""插件入口与装配层。

拥有：唯一的 ``Star`` 子类、宿主事件接入（``on_message``）、``/selfreply``
指令处理器、生命周期（``terminate`` 与优雅停止），以及把各协作者接线成
一个流程的构造顺序。业务规则不在这里：判断属 ``decision``，生成属
``generation``，发送状态机属 ``delivery``，定时与巡检属 ``scheduler``，
会话状态属 ``session_coordinator``。

模块顶部的 import 被 ``_AGENT_RUNTIME`` 分成两段（故 ruff 对本文件忽略
E402）：宿主私有符号必须先经适配层探测并绑到模块级名字，后续模块才能拿到
可被测试替换的那几个名字；整体上移会断掉这条测试缝。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Coroutine
from types import MappingProxyType
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, register

from .message_ingress import handle_incoming_message
from .runtime_adapter import AstrBotRuntimeAdapter
from .session_gate import SessionGate

# 指令处理器的产出类型：每个 @selfreply.command 处理器都是 async generator，
# 逐条 yield event.plain_result(...)。
#
# **必须是运行时可解析的名字，不能放回 TYPE_CHECKING 块**：宿主注册处理器时调
# `inspect.signature(handler, eval_str=True)`（4.27.2 起），会把字符串注解真的
# eval 一遍，TYPE_CHECKING-only 的名字在那一步 NameError，整个插件拒绝加载。
# 守卫：scripts/compat_check.py::_handler_signature_gaps。
CommandReply = AsyncGenerator[Any, None]

_AGENT_RUNTIME = AstrBotRuntimeAdapter.from_host()

# 宿主私有符号收敛：值全部来自适配层探测；模块级名字保留供测试替换，
# 加载期缺失由 AstrBotRuntimeAdapter.validate() 的契约断言兜底。
call_event_hook = _AGENT_RUNTIME.capabilities.call_event_hook
get_astrbot_config_path = _AGENT_RUNTIME.capabilities.config_path_fn
get_astrbot_plugin_data_path = _AGENT_RUNTIME.capabilities.plugin_data_path_fn

from .adapters import AstrBotBridge
from .commands import (
    dispatch_command_action,
    help_text,
)
from .decision import DECISION_MAX_TOKENS, DECISION_SYSTEM_PROMPT, DecisionMaker
from .delivery import DeliveryRunner
from .generation import GenerationRunner
from .image import ImageInfo
from .image.vision_runtime import VisionService
from .models import (
    ADMIN_COMMAND_ACTIONS,
    COMMAND_HANDLED_KEY,
    GRACEFUL_STOP_GRACE_SEC,
    PLUGIN_ID,
    PLUGIN_VERSION,
    SESSION_CANCEL_COMMAND_ACTIONS,
    TERMINATE_TASK_TIMEOUT_SEC,
    PluginLifecycle,
    SessionState,
    Settings,
    now_ts,
)
from .plugin_state import (
    async_sync_whitelist,
    persist_enabled,
    refresh_admin_ids,
    resolve_paths,
    save_storage,
    save_storage_sync,
    state_for,
    track_background_task,
    track_critical_task,
)
from .scheduler import SessionScheduler
from .session_coordinator import SessionCoordinator
from .session_pipeline import SessionPipeline
from .storage import (
    config_file_matches,
    load_config_data,
    load_sessions,
    persist_settings_config,
)
from .utils import (
    consume_task_result,
    event_umo,
    is_admin_event,
    is_explicit_direct_call,
    session_group_id,
    session_whitelisted,
)
from .webapi import load_ui_prefs, register_web_apis
from .whitelist import WhitelistManager


@register(
    PLUGIN_ID,
    "chengzhi-c",
    "精简主动回复插件：白名单会话内，避开 @Bot/命令后自然接话",
    PLUGIN_VERSION,
)
class SelfInitiatedReplyPlugin(Star):
    _coordinator: SessionCoordinator
    _decision: DecisionMaker
    _delivery: DeliveryRunner
    _generation: GenerationRunner
    _pipeline: SessionPipeline
    _scheduler: SessionScheduler
    _vision: VisionService
    _whitelist: WhitelistManager

    def __init__(
        self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None
    ) -> None:
        _AGENT_RUNTIME.validate()
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self._config_path, self._storage_path, self._data_root = resolve_paths(
            self.config,
            get_config_path=get_astrbot_config_path,
            get_plugin_data_path=get_astrbot_plugin_data_path,
        )

        config_data = load_config_data(self._config_path, self.config)
        self.settings = Settings.from_config(config_data)
        self.runtime_enabled = self.settings.enabled

        # 桥只作历史记录读取用；表情包与 livingmemory 走 AstrBot 正常 LLM 管线。
        self.bridge = AstrBotBridge(context)

        # 首次规范化落盘的判定在这里，落盘本体延后到构造末尾与其余启动
        # 磁盘 IO 一起执行：此处 spawn 路径不可用，且写盘含 fsync，
        # 在会话循环上就地执行会阻塞所有会话。
        self._pending_normalize_config = not config_file_matches(self._config_path, self.settings)

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
        # UI 偏好：AstrBot 插件页面以 iframe 嵌入 Dashboard，localStorage
        # 不可靠，主题/压暗/粗体写入后端 JSON。
        self._ui_prefs_path = self._storage_path.parent / "ui_prefs.json"
        self._ui_theme, self._ui_dim, self._ui_bold = load_ui_prefs(self)
        self._whitelist_runtime_umos: dict[str, set[str]] = {}
        self._delay_tasks: dict[str, asyncio.Task[Any]] = {}
        self._running_check_tasks: dict[str, asyncio.Task[Any]] = {}
        # 全局单调代次计数器：白名单移除/重加不会再产生 ABA。
        self._gate = SessionGate()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._critical_tasks: set[asyncio.Task[Any]] = set()
        self._quarantined_tasks: dict[asyncio.Task[Any], str] = {}
        self._lifecycle_state = PluginLifecycle.RUNNING
        self._stopping = False
        # 跨实例落盘闸门：本实例被判"最终落盘超时"后置位，仍在跑的写盘在
        # os.replace 前自我放弃，避免用陈旧快照覆盖新实例写出的 state.json。
        self._abandon_disk_writes = False
        self._save_lock = asyncio.Lock()
        self._config_lock = asyncio.Lock()
        self._admin_file_mtime: float | None = None
        self._admin_ids: set[str] = set()
        self._admin_probe_ts = 0.0  # 探测窗口起点：0 保证首次调用必探
        self._last_decisions: dict[str, dict[str, Any]] = {}
        self._refresh_admin_ids()

        self._assemble_components()

        # 启动期磁盘 IO 统一在此执行（配置规范化落盘、状态落盘、图片缓存清理）。
        # 它们都含 fsync / 大目录遍历，跑在宿主事件循环上会阻塞该进程内
        # 所有会话与 Web 面板（契约见 tests/test_cleanup_nonblocking）。
        # - 有运行中的循环 → 全部交后台任务（磁盘部分内部走 to_thread）。
        # - 无循环（同步加载的宿主）→ 原地同步执行，且**不得**在此 spawn。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._pending_normalize_config:
                self._normalize_config_sync()
            self._save_storage_sync()
            try:
                self._scheduler.cleanup_image_sources(now=now_ts())
            except Exception as exc:
                logger.warning("[%s] startup image cache cleanup failed: %s", PLUGIN_ID, exc)
        else:
            self._track_background_task(self._startup_disk_writes())
            self._scheduler.ensure_patrol()
            self._scheduler.ensure_image_cleanup()
        logger.info(
            "[%s] v%s enabled=%s whitelist=%d message_trigger=%s patrol_trigger=%s",
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
        register_web_apis(self)

    def _startup_disk_writes(self) -> Coroutine[Any, Any, None]:
        """构造期磁盘 IO 的就绪协程：配置规范化落盘 + 状态落盘 + 图片缓存清理。

        只构造、**不** spawn：调用方拿到协程后自行决定交给
        ``_track_background_task``（有循环）还是关闭（同步加载路径不会走到这里）。
        构造即就绪，离开本方法前无人 await 它，未消费就是"从未开始的协程"，
        调用方若丢弃必须 close。
        """

        async def run() -> None:
            if self._pending_normalize_config:
                await self._normalize_config_off_loop()
            try:
                # 本任务已在事件循环之外的目的地，to_thread 内执行，
                # 既不阻塞循环也不失去"内容一致即跳过"的语义。
                await asyncio.to_thread(self._save_storage_sync)
            except Exception as exc:
                logger.warning("[%s] startup state save failed: %s", PLUGIN_ID, exc)
            try:
                await self._scheduler.run_image_cleanup()
            except Exception as exc:
                logger.warning("[%s] startup image cache cleanup failed: %s", PLUGIN_ID, exc)

        return run()

    def _assemble_components(self) -> None:
        """接线协作对象。须在 gate/状态容器就绪之后、ensure_task 之前调用。"""
        self._coordinator = SessionCoordinator(
            last_events=self._last_events,
            last_event_at=self._last_event_at,
            recent_image_events=self._recent_image_events,
            gate=self._gate,
            cancel_delay=lambda umo, force: self._scheduler.cancel_delay(umo, force=force),
            notify_silence=lambda umo: self._scheduler.notify_activity(umo),
        )
        self._vision = VisionService(
            settings=self.settings,
            bridge=self.bridge,
            context=self.context,
            source_cache_dir=self._image_cache_dir,
            data_root=self._data_root,
            coordinator=self._coordinator,
            gate=self._gate,
            is_stopping=lambda: self._stopping,
            track_background_task=self._track_background_task,
        )
        self._scheduler = SessionScheduler(
            settings=self.settings,
            gate=self._gate,
            image_cache_dir=self._image_cache_dir,
            spawn=self._track_background_task,
            should_run=lambda: self._can_start_tasks() and self.runtime_enabled,
            state_for=lambda umo: self._state_for(umo),
            check_session=lambda umo, trigger, force, expected_generation: (
                self._pipeline.check_session(
                    umo,
                    trigger=trigger,
                    force=force,
                    expected_generation=expected_generation,
                )
            ),
            clear_event=self._coordinator.clear_event,
            drop_older_images=self._coordinator.drop_older_than,
            last_events=self._last_events,
            last_event_at=self._last_event_at,
            recent_image_events=self._recent_image_events,
            whitelist_runtime_umos=self._whitelist_runtime_umos,
            delay_tasks=self._delay_tasks,
            running_check_tasks=self._running_check_tasks,
            background_tasks=self._background_tasks,
            quarantine_task=self._quarantine_task,
        )

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
            build_image_context=self._vision.build_context,
            # 判断超时后仍未收敛的 provider 任务交同一隔离登记（与生成路径同源）。
            quarantine_task=self._quarantine_task,
        )

        self._generation = GenerationRunner(
            settings=self.settings,
            context=self.context,
            runtime=lambda: _AGENT_RUNTIME,
            gate=self._gate,
            local_gate=self._local_gate,
            call_hook=lambda event, event_type, req: call_event_hook(event, event_type, req),
            grace_stop_sec=lambda: GRACEFUL_STOP_GRACE_SEC,
            background_tasks=self._background_tasks,
            discard_background=self._background_tasks.discard,
            read_history=lambda umo, limit: self.bridge.read_astrbot_history(umo, limit=limit),
            build_image_context=self._vision.build_context,
            last_events=self._last_events,
            is_stopping=lambda: self._stopping,
            quarantine_task=self._quarantine_task,
        )

        self._delivery = DeliveryRunner(
            settings=self.settings,
            gate=self._gate,
            local_gate=self._local_gate,
            last_events=self._last_events,
            call_hook=lambda event, event_type: call_event_hook(event, event_type),
            context_send=lambda umo, message: self.context.send_message(umo, message),
            save_storage=lambda: self._save_storage(),
            runtime=lambda: _AGENT_RUNTIME,
            is_stopping=lambda: self._stopping,
        )

        self._whitelist = WhitelistManager(
            settings=self.settings,
            sync_whitelist=self._persist_config,
            save_storage=lambda: self._save_storage(),
            ensure_state=lambda umo: self._state_for(umo),
            invalidate=lambda umo: self._coordinator.invalidate(umo),
            prune=lambda umo: self._prune_session(umo),
            sessions=self.sessions,
            whitelist_runtime_umos=self._whitelist_runtime_umos,
            tracked_umos=lambda: (
                set(self._last_events)
                | set(self._delay_tasks)
                | set(self._running_sessions)
                | set(self._session_locks)
            ),
        )

        self._pipeline = SessionPipeline(
            state_for=lambda umo: self._state_for(umo),
            generation=self._generation,
            delivery=self._delivery,
            is_stopping=lambda: self._stopping,
            is_enabled=lambda: self.runtime_enabled,
            settings=self.settings,
            gate=self._gate,
            decision=self._decision,
            last_events=self._last_events,
            last_decisions=self._last_decisions,
            track_critical_task=self._track_critical_task,
        )

    def _local_gate(
        self,
        state: SessionState,
        *,
        force: bool,
        silence_active_at: float | None = None,
    ) -> str:
        """generation/delivery 共用的局部闸门回调（``LocalGateCallback`` 形状）。

        经实例属性惰性查找 ``self._decision``：测试替换 _decision 后仍指向最新实现，
        与装配层其余回调的测试缝一致。
        """
        return self._decision.local_gate(state, force=force, silence_active_at=silence_active_at)

    def _state_for(self, umo: str) -> SessionState:
        return state_for(self, umo)

    def _refresh_admin_ids(self) -> set[str]:
        return refresh_admin_ids(self)

    def _save_storage_sync(self) -> None:
        save_storage_sync(self)

    def _normalize_config_sync(self) -> None:
        """同步的配置规范化落盘（无事件循环的宿主，或后台任务内部调用）。"""
        if not persist_settings_config(self._config_path, self.config, self.settings):
            logger.error(
                "[%s] config normalization write failed, running with the loaded config: %s",
                PLUGIN_ID,
                self._config_path,
            )

    async def _normalize_config_off_loop(self) -> None:
        """把规范化落盘移出事件循环线程（fsync 不得阻塞所有会话）。"""
        try:
            await asyncio.to_thread(self._normalize_config_sync)
        except Exception as exc:
            logger.warning(
                "[%s] config normalization task error: %s",
                PLUGIN_ID,
                exc,
            )

    async def _save_storage(self) -> None:
        await save_storage(self)

    async def _persist_config(self) -> bool:
        return await async_sync_whitelist(self)

    async def _persist_enabled(self, enabled: bool) -> None:
        await persist_enabled(self, enabled)

    def _track_background_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
        return track_background_task(self, coro)

    def _track_critical_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        return track_critical_task(self, coro)

    @property
    def lifecycle_state(self) -> str:
        """Return the lifecycle state for diagnostics and entry-point gates."""
        return self._lifecycle_state.value

    def _can_start_tasks(self) -> bool:
        """Return whether new plugin-owned work may be scheduled.

        不做隔离注册表的容量判定：首例隔离即永久 DEGRADED，任何容量条件
        都会被先行短路，留着只会误导读者以为它参与门禁。
        """
        return self._lifecycle_state is PluginLifecycle.RUNNING and not self._stopping

    def _reject_if_not_running(self, action: str) -> None:
        """拒绝非 RUNNING 态下的写操作，并按实际生命周期给出准确原因
        （DEGRADED 是永久降级需重启，STOPPING 才是真的在关闭）。"""
        if self._lifecycle_state is PluginLifecycle.DEGRADED:
            raise RuntimeError(f"插件已降级，无法{action}（需重启插件恢复）")
        if self._stopping:
            raise RuntimeError(f"插件正在关闭，无法{action}")

    def _mark_degraded(self, reason: str) -> None:
        """Quarantine failure state and permanently close new work for this instance."""
        if self._lifecycle_state is PluginLifecycle.DEGRADED:
            return
        self._lifecycle_state = PluginLifecycle.DEGRADED
        self._stopping = True
        logger.error(
            "[%s] lifecycle degraded quarantined=%d reason=%s",
            PLUGIN_ID,
            len(self._quarantined_tasks),
            reason,
        )

    def _release_quarantined_task(self, task: asyncio.Task[Any]) -> None:
        """Remove a quarantined task after it finally exits."""
        # 隔离任务常以真实异常（而非取消）收尾：显式消费一次，避免任务
        # 回收时打无归属的 "Task exception was never retrieved"。
        consume_task_result(task)
        reason = self._quarantined_tasks.pop(task, "")
        logger.warning(
            "[%s] quarantined task exited reason=%s remaining=%d",
            PLUGIN_ID,
            reason,
            len(self._quarantined_tasks),
        )

    def _quarantine_task(self, task: asyncio.Task[Any], reason: str) -> None:
        """Track a task that ignored cancellation instead of pretending shutdown succeeded."""
        if task.done() or task in self._quarantined_tasks:
            return
        self._quarantined_tasks[task] = reason
        task.add_done_callback(self._release_quarantined_task)
        self._mark_degraded(reason)
        logger.warning(
            "[%s] task quarantined count=%d reason=%s",
            PLUGIN_ID,
            len(self._quarantined_tasks),
            reason,
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """事件监听器：收集白名单会话消息，驱动主动回复的判断与调度。"""
        await handle_incoming_message(self, event)

    @staticmethod
    def _is_command_entry(event: AstrMessageEvent, text: str) -> bool:
        """Require an explicit command entry before consuming the event.

        Without this gate any group member could send the bare word
        ``selfreply`` and make the bot emit the whole help text and then call
        ``stop_event()``, swallowing the message for every other plugin.
        """
        if str(text or "").lstrip().startswith("/"):
            return True
        # is_explicit_direct_call 的第一判据就是宿主的 is_at_or_wake_command，
        # 不再重复调一次（宿主 callable 被同一个事件跑两遍）。
        return is_explicit_direct_call(event, text)

    # 只读视图：数据归属 SessionGate，以下 property 供既有调用点与测试
    # 以原字段名访问。读侧返回只读视图，外部误写会在运行时直接抛错。
    @property
    def _session_generation(self) -> MappingProxyType[str, int]:
        return self._gate.generation_view

    @property
    def _running_sessions(self) -> frozenset[str]:
        return self._gate.running_sessions_view

    @property
    def _session_locks(self) -> MappingProxyType[str, asyncio.Lock]:
        return self._gate.locks_view

    def _prune_session(self, umo: str) -> None:
        """会话回收单点：代次/锁/运行标记/最近裁决 + 会话状态内存回收。

        磁盘由 build_sessions_payload 写盘时过滤非白名单条目，重启后不复活。
        """
        self._gate.prune(umo)
        self._last_decisions.pop(umo, None)
        self.sessions.pop(umo, None)
        self.sessions.pop(session_group_id(umo), None)

    def _cancel_event_session(self, event: AstrMessageEvent) -> None:
        umo = event_umo(event)
        if umo and session_whitelisted(umo, self.settings.whitelist):
            self._coordinator.invalidate(umo, force_cancel=True)

    async def _add_whitelist_session(self, umo: str) -> bool:
        async with self._config_lock:
            self._reject_if_not_running("修改白名单")
            return await self._whitelist.add(umo)

    async def _remove_whitelist_session(self, umo: str) -> bool:
        async with self._config_lock:
            self._reject_if_not_running("修改白名单")
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
        return await dispatch_command_action(self, event, action, arg)

    async def _send_command_text(self, event: AstrMessageEvent, text: str) -> None:
        try:
            await event.send(MessageChain().message(text))
        except Exception as exc:
            logger.debug("[%s] inline command send failed: %s", PLUGIN_ID, exc)
            try:
                event.set_result(event.plain_result(text))
            except Exception:
                # 两条路都不通说明事件已被宿主终结，丢回显优于抛异常打断管道。
                pass
        try:
            event.stop_event()
        except Exception:
            # 事件可能已被宿主或上游插件终结，重复 stop 无意义。
            pass

    # 注意：permission_type 必须在 command_group 内层。真实宿主（4.26.8/4.27.0
    # 已验证）的 register_permission_type 会对被装饰对象调用 get_handler_full_name（访问
    # __name__），而 command_group 返回的 RegisteringCommandable 没有 __name__；
    # 顺序反了插件加载即报 AttributeError。
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
        yield event.plain_result(await self._command_text(event, "help"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("status", alias={"stat"})
    async def selfreply_status(self, event: AstrMessageEvent) -> CommandReply:
        """状态：查看运行状态、判断模型和白名单信息。"""
        self._set_command_handled(event)
        # 委托内联指令的分派出口：组装逻辑单点维护，两条出口不再镜像。
        yield event.plain_result(await self._command_text(event, "status"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("list", alias={"ls", "whitelist"})
    async def selfreply_list(self, event: AstrMessageEvent) -> CommandReply:
        """列表：查看主动回复白名单。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "list"))

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
        """开启：启用主动回复运行，重启后保持。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "on"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("off", alias={"disable", "pause", "stop"})
    async def selfreply_off(self, event: AstrMessageEvent) -> CommandReply:
        """关闭：暂停主动回复运行，重启后保持。"""
        self._set_command_handled(event)
        yield event.plain_result(await self._command_text(event, "off"))

    @permission_type(PermissionType.ADMIN)
    @selfreply.command("debug", alias={"diag", "diagnose"})
    async def selfreply_debug(self, event: AstrMessageEvent) -> CommandReply:
        """调试：查看当前会话、发送者和触发识别信息。"""
        self._set_command_handled(event)
        # 同 status：委托内联分派出口，组装逻辑单点维护。
        yield event.plain_result(await self._command_text(event, "debug"))

    def _set_command_handled(self, event: AstrMessageEvent) -> None:
        try:
            event.set_extra(COMMAND_HANDLED_KEY, True)
        except Exception:
            # 老宿主可能未实现 set_extra。标记丢失只让同一事件在后续 on_message
            # 少一层去重保护，兜底是事件自身的 stop_event/is_stopped。
            pass

    def _cancel_background_tasks(self) -> None:
        current = asyncio.current_task()
        for task in list(self._background_tasks):
            if (
                task is current
                or task.done()
                or task in self._critical_tasks
                or task in self._quarantined_tasks
            ):
                continue
            task.cancel()

    def _cancel_delay_tasks(self) -> None:
        sessions = (
            set(self._delay_tasks) | set(self._running_sessions) | set(self._running_check_tasks)
        )
        for umo in sessions:
            self._coordinator.invalidate(umo, force_cancel=True)
        self._delay_tasks.clear()
        self._cancel_background_tasks()

    async def _wait_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in list(self._background_tasks)
            if (task is not current and not task.done() and task not in self._quarantined_tasks)
        ]
        if not tasks:
            return

        _, pending = await asyncio.wait(tasks, timeout=TERMINATE_TASK_TIMEOUT_SEC)
        for task in pending:
            task.cancel()
            # 超时即取消并隔离：terminate 有界（契约 §5），取消后的清理只由
            # 任务自身的 done 回调收尾。
            self._quarantine_task(task, "shutdown deadline exceeded")
        self._background_tasks.difference_update(task for task in tasks if task.done())

    async def _save_final_state_with_deadline(self) -> None:
        async def persist() -> None:
            await self._save_storage()

        task = self._track_critical_task(persist())
        done, _ = await asyncio.wait({task}, timeout=max(0.0, TERMINATE_TASK_TIMEOUT_SEC))
        if not done:
            # 硬窗口耗尽：宿主不等隔离任务就构造新实例，本实例的慢写若落地会
            # 用陈旧快照覆盖新实例刚写出的 state.json。置位放弃标志，让仍在跑
            # 的写盘在 os.replace 之前自我放弃。
            self._abandon_disk_writes = True
            self._quarantine_task(task, "final state save deadline exceeded")
            return
        try:
            task.result()
        except Exception as exc:
            logger.warning("[%s] final state save failed: %s", PLUGIN_ID, exc)

    async def terminate(self) -> None:
        self._lifecycle_state = (
            PluginLifecycle.DEGRADED
            if self._lifecycle_state is PluginLifecycle.DEGRADED
            else PluginLifecycle.STOPPING
        )
        self._stopping = True
        # 最终落盘必须与任务收敛同处 _config_lock 内：Dashboard 的 POST /config
        # 在同一把锁下改 settings/whitelist，锁外快照会读到半更新的白名单。
        # 锁序恒为 _config_lock → _save_lock，无反向持有。
        async with self._config_lock:
            self._cancel_delay_tasks()
            await self._scheduler.stop_patrol()
            self._cancel_background_tasks()
            await self._wait_background_tasks()
            self._coordinator.reset_all()
            # 记录点已逐次落盘，此处兜底覆盖「最后一次记录之后又有内存变更」。
            await self._save_final_state_with_deadline()
        logger.info("[%s] terminated", PLUGIN_ID)
