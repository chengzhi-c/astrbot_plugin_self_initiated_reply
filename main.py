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
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from functools import partial
from types import MappingProxyType
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, register

from .assembly import assemble_plugin_components
from .message_ingress import handle_incoming_message
from .runtime_adapter import AstrBotRuntimeAdapter
from .session_gate import SessionGate

# 指令处理器的产出类型：每个 @selfreply.command 处理器都是 async generator，
# 逐条 yield event.plain_result(...)。宿主侧契约是
# AsyncGenerator[MessageEventResult | str | None]，本插件只 yield 前者。
#
# **必须是运行时可解析的名字，不能放回 TYPE_CHECKING 块**（0.9.5 线上修复）。
# 精确机制已在真机 4.27.2 上读源码确证（不是推断）：宿主
# `core/star/filter/command.py::CommandFilter.init_handler_md` 注册每个指令处理器时调
#   4.23.3: inspect.signature(handler)
#   4.27.2: inspect.signature(handler, eval_str=True)   ← 一个参数之差
# `eval_str=True` 会把 `from __future__ import annotations` 产出的字符串注解真的
# eval 一遍，于是 TYPE_CHECKING-only 的名字在那一步 NameError，整个插件拒绝加载：
#   加载插件「业镜 · 主动回复」... 原因：name 'CommandReply' is not defined
# 宿主里没有 get_type_hints；早前注释写成「等价于 get_type_hints」是猜的，已订正。
# 守卫：scripts/compat_check.py::_handler_signature_gaps 照抄这一步，两个宿主版本上
# 都会红（4.23.3 上宿主自己不会失败，但那不是可依赖的事实——它已经变过一次）。
#
# 这里刻意不写成 AsyncGenerator[MessageEventResult, None]：那需要运行时 import 宿主
# 符号（多一条加载期硬依赖，且测试替身未导出该名字）。参数化成 Any 不损失任何检查力
# ——astrbot.* 在 mypy 眼里本就全是 Any，精确写法只有文档价值，
# 该价值由本注释承载。
CommandReply = AsyncGenerator[Any, None]

_AGENT_RUNTIME = AstrBotRuntimeAdapter.from_host()

# 宿主私有符号收敛：值全部来自适配层探测，本文件不再直接
# import 宿主私有层（astrbot.core.*）；模块级名字保留供测试替换与旧引用，
# 加载期缺失由 AstrBotRuntimeAdapter.validate() 的契约断言兜底（缺失即红，拒绝加载并
# 提示修复方向）。
call_event_hook = _AGENT_RUNTIME.capabilities.call_event_hook
get_astrbot_config_path = _AGENT_RUNTIME.capabilities.config_path_fn
get_astrbot_plugin_data_path = _AGENT_RUNTIME.capabilities.plugin_data_path_fn

from .adapters import AstrBotBridge
from .commands import (
    debug_text,
    dispatch_command_action,
    help_text,
    list_text,
    status_text,
)
from .image import ImageInfo, ImageParser
from .models import (
    ADMIN_COMMAND_ACTIONS,
    COMMAND_HANDLED_KEY,
    GRACEFUL_STOP_GRACE_SEC,
    PLUGIN_ID,
    PLUGIN_VERSION,
    SESSION_CANCEL_COMMAND_ACTIONS,
    SessionState,
    Settings,
    now_ts,
)
from .plugin_state import (
    persist_enabled as state_persist_enabled,
)
from .plugin_state import (
    refresh_admin_ids as state_refresh_admin_ids,
)
from .plugin_state import (
    resolve_paths as state_resolve_paths,
)
from .plugin_state import (
    save_storage as state_save_storage,
)
from .plugin_state import (
    save_storage_sync as state_save_storage_sync,
)
from .plugin_state import (
    state_for as state_state_for,
)
from .plugin_state import (
    sync_whitelist as state_sync_whitelist,
)
from .plugin_state import (
    track_background_task as state_track_background_task,
)
from .storage import (
    load_config_data,
    load_sessions,
    migrate_config_file,
)
from .utils import (
    event_sender_id,
    event_umo,
    is_admin_event,
    is_at_or_wake_command_event,
    is_explicit_direct_call,
    session_group_id,
    session_whitelisted,
    whitelist_storage_key,
)
from .webapi import bind_api_handlers, load_ui_theme, register_web_apis


@register(
    PLUGIN_ID,
    "chengzhi-c",
    "精简主动回复插件：白名单会话内，避开 @Bot/命令后自然接话",
    PLUGIN_VERSION,
)
class SelfInitiatedReplyPlugin(Star):
    # Web API 由 bind_api_handlers 挂 partial；裸注解仅导航
    # （test_bound_api_handlers_match_class_declarations）。
    _api_get_config: Callable[[], Awaitable[dict[str, Any]]]
    _api_post_config: Callable[[], Awaitable[dict[str, Any]]]
    _api_get_ui_theme: Callable[[], Awaitable[dict[str, Any]]]
    _api_post_ui_theme: Callable[[], Awaitable[dict[str, Any]]]

    def __init__(
        self, context: Context, config: AstrBotConfig | dict[str, Any] | None = None
    ) -> None:
        """校验宿主 → 路径/配置/状态 → ``_assemble_components`` → 启动副作用。"""
        _AGENT_RUNTIME.validate()
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self._config_path, self._storage_path = state_resolve_paths(
            self.config,
            get_config_path=get_astrbot_config_path,
            get_plugin_data_path=get_astrbot_plugin_data_path,
        )
        # state.json 位于 <data>/plugin_data/<plugin_id>/state.json
        _STATE_DEPTH_FROM_DATA = 2
        self._data_path = self._storage_path.parents[_STATE_DEPTH_FROM_DATA]

        config_data = load_config_data(self._config_path, self.config)
        self.settings = Settings.from_config(config_data)
        self.runtime_enabled = self.settings.enabled

        # 只保留历史记录桥接；表情包和 livingmemory 不再由本插件直连，
        # 改为通过 AstrBot 正常 LLM 管线自动触发，行为更接近 @Bot 回复。
        self.bridge = AstrBotBridge(context)

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
        self._last_decisions: dict[str, dict[str, Any]] = {}
        self._resolve_paths = lambda config_obj: state_resolve_paths(
            config_obj,
            get_config_path=get_astrbot_config_path,
            get_plugin_data_path=get_astrbot_plugin_data_path,
        )
        self._refresh_admin_ids = partial(state_refresh_admin_ids, self)
        self._state_for = partial(state_state_for, self)
        self._save_storage_sync = partial(state_save_storage_sync, self)
        self._save_storage = partial(state_save_storage, self)
        self._sync_whitelist = partial(state_sync_whitelist, self)
        self._persist_enabled = partial(state_persist_enabled, self)
        self._track_background_task = partial(state_track_background_task, self)
        self._refresh_admin_ids()

        self._assemble_components()

        self._save_storage_sync()
        try:
            # Reload/startup is also a maintenance boundary: remove old orphaned
            # cache files immediately instead of waiting for the first interval.
            self._scheduler.cleanup_image_sources(now=now_ts())
        except Exception as exc:
            logger.warning("[%s] startup image cache cleanup failed: %s", PLUGIN_ID, exc)
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
        bind_api_handlers(self)
        register_web_apis(self)

    def _assemble_components(self) -> None:
        """接线协作对象。须在 gate/状态容器就绪之后、ensure_task 之前调用。"""
        assemble_plugin_components(
            self,
            get_runtime=lambda: _AGENT_RUNTIME,
            get_call_hook=lambda: call_event_hook,
            get_grace_stop_sec=lambda: GRACEFUL_STOP_GRACE_SEC,
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """指令分流 → 白名单 → 记上下文 → 延迟检查。顺序是安全边界，不可重排。"""
        await handle_incoming_message(self, event)

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
            self._coordinator.invalidate(umo, force_cancel=True)

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
        """指令动作 → 回显文本。check 在 finally 回收缓存；未知 action 回落 help。"""
        return await dispatch_command_action(self, event, action, arg)

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
            self._coordinator.invalidate(umo, force_cancel=True)
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

    async def terminate(self) -> None:
        self._stopping = True
        # 最终落盘必须与任务收敛同处 _config_lock 内：Dashboard 的 POST /config
        # 在同一把锁下改 settings/whitelist（webapi._apply_config_updates），
        # 锁外快照会读到半更新的白名单，把刚加/刚删的会话错误过滤掉。
        # 锁序恒为 _config_lock → _save_lock（两条路径一致），无反向持有。
        async with self._config_lock:
            self._cancel_delay_tasks()
            await self._scheduler.stop_patrol()
            await self._wait_background_tasks()
            self._coordinator.reset_all()
            try:
                # 记录点已逐次落盘，此处兜底覆盖「最后一次记录之后又有内存
                # 变更」（白名单回收、跨天刷新）。
                await self._save_storage()
            except Exception as exc:
                logger.warning("[%s] final state save failed: %s", PLUGIN_ID, exc)
        logger.info("[%s] terminated", PLUGIN_ID)
