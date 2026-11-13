"""路径解析、会话状态、持久化与后台任务注册。"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

from .models import (
    ADMIN_REFRESH_WINDOW_SEC,
    PLUGIN_ID,
    MessageRecord,
    SessionState,
    now_ts,
)
from .storage import (
    apersist_settings_config,
    build_sessions_payload,
    sessions_payload_matches,
    write_json_atomic,
)
from .utils import (
    await_rollback,
    event_sender_id,
    event_sender_name,
    whitelist_storage_key,
)

if TYPE_CHECKING:
    from .main import SelfInitiatedReplyPlugin


def resolve_paths(
    config_obj: Any,
    *,
    get_config_path: Any = None,
    get_plugin_data_path: Any = None,
) -> tuple[Path, Path, Path]:
    """Resolve config / state.json / data-root paths from host root, with legacy fallback.

    第三个返回值是宿主 ``<data>`` 根，识图本地读取 allowlist 与 ``cmd_config.json``
    热读都以它为基准。它由 ``plugin_data_path`` 的构造式**正向**推出
    （``<data>/plugin_data/<pid>`` 的上两级）；不得从 ``state.json`` 反向数
    parents 层数，那会把"恰好嵌两层"变成隐式前提，嵌套一改就静默算错。
    """
    configured_path = getattr(config_obj, "config_path", None)
    if configured_path:
        config_path = Path(str(configured_path)).expanduser()
    elif callable(get_config_path):
        config_path = Path(str(get_config_path())).expanduser() / f"{PLUGIN_ID}_config.json"
    else:
        config_path = Path.home() / ".astrbot" / "data" / "config" / f"{PLUGIN_ID}_config.json"

    if callable(get_plugin_data_path):
        plugin_data_path = Path(str(get_plugin_data_path())).expanduser() / PLUGIN_ID
    else:
        plugin_data_path = config_path.parent.parent / "plugin_data" / PLUGIN_ID
    return config_path, plugin_data_path / "state.json", plugin_data_path.parent.parent


def refresh_admin_ids(plugin: SelfInitiatedReplyPlugin) -> set[str]:
    """时间窗 + mtime 双缓存热读管理员列表。"""
    now = now_ts()
    if now - plugin._admin_probe_ts < ADMIN_REFRESH_WINDOW_SEC:
        return plugin._admin_ids
    plugin._admin_probe_ts = now
    path = plugin._data_root / "cmd_config.json"
    try:
        if path.exists():
            mtime = path.stat().st_mtime
            if mtime == plugin._admin_file_mtime:
                return plugin._admin_ids
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            admins = data.get("admins_id", []) if isinstance(data, dict) else []
            if not isinstance(admins, (list, tuple, set)):
                # 形状不对（标量/字典）时按空集处理：直接迭代标量会逐字符拆出
                # 一串假管理员 ID（"12345" → 1/2/3/4/5），等于给任意单位数
                # 账号发管理员权限。
                admins = []
            plugin._admin_ids = {str(item).strip() for item in admins if str(item).strip()}
            plugin._admin_file_mtime = mtime
        # 文件不在：沿用上次集合（探测失败不变更缓存，见 ADMIN_REFRESH_WINDOW_SEC
        # 的注释）；文件被删与瞬时读取失败在本层不可区分，清空会把所有管理员
        # 暂时锁外面。
    except Exception as exc:
        logger.debug("[%s] load admins failed path=%s error=%s", PLUGIN_ID, path, exc)
    return plugin._admin_ids


def _register_task(
    plugin: SelfInitiatedReplyPlugin, coro: Coroutine[Any, Any, Any], *, critical: bool
) -> asyncio.Task[Any]:
    """统一任务注册：建 task、入注册表、挂丢弃回调；critical 额外入关键表。"""
    task: asyncio.Task[Any] = asyncio.create_task(coro)
    plugin._background_tasks.add(task)
    if critical:
        plugin._critical_tasks.add(task)
        task.add_done_callback(plugin._critical_tasks.discard)
    task.add_done_callback(plugin._background_tasks.discard)
    return task


def track_critical_task(
    plugin: SelfInitiatedReplyPlugin, coro: Coroutine[Any, Any, Any]
) -> asyncio.Task[Any]:
    """Register a persistence task even while shutdown is already stopping."""
    return _register_task(plugin, coro, critical=True)


def state_for(plugin: SelfInitiatedReplyPlugin, umo: str) -> SessionState:
    """取（必要时创建）会话状态。不做 legacy 迁移（一次性迁移在
    ``load_sessions`` 里完成）；状态键在此派生（``whitelist_storage_key``），
    调用方一律传 UMO，键的口径不散落到调用点。
    """
    key = whitelist_storage_key(umo)
    state = plugin.sessions.get(key)
    if state is None:
        state = SessionState(recent=deque(maxlen=plugin.settings.recent_message_limit))
    else:
        limit = plugin.settings.recent_message_limit
        if state.recent.maxlen != limit:
            state.recent = deque(state.recent, maxlen=limit)
    plugin.sessions[key] = state
    state.refresh_day()
    return state


def read_session_state(plugin: SelfInitiatedReplyPlugin, umo: str) -> SessionState:
    """只读取会话状态：不创建、不滞留、不做 legacy 迁移、不刷新日期。

    供 status/debug 等只读指令组装参数；用 ``state_for`` 会把非白名单会话的
    空状态隐式创建并滞留在内存。
    """
    return plugin.sessions.get(whitelist_storage_key(umo)) or SessionState()


def append_recent_user_message(
    plugin: SelfInitiatedReplyPlugin,
    event: Any,
    *,
    umo: str,
    clean_text: str,
    active_at: float | None = None,
) -> float:
    """Write last_active + recent user record. Does not advance generation."""
    stamped = now_ts() if active_at is None else active_at
    state = plugin._state_for(umo)
    state.last_active_at = stamped
    sender_id = event_sender_id(event)
    state.recent.append(
        MessageRecord(
            role="user",
            name=event_sender_name(event),
            sender_id=sender_id,
            text=clean_text,
            at=stamped,
        )
    )
    return stamped


def _build_payload(plugin: SelfInitiatedReplyPlugin) -> dict[str, Any]:
    """构造状态快照 payload；两条落盘路径共用同一份构造逻辑。"""
    return build_sessions_payload(
        plugin.sessions,
        plugin.settings.whitelist,
        plugin.settings.recent_message_limit,
    )


def save_storage_sync(plugin: SelfInitiatedReplyPlugin) -> None:
    """启动期状态落盘：磁盘已与待写快照一致时跳过（无谓 fsync 与 mtime 扰动）。

    仅 ``__init__`` 调用：刚从同一文件加载出的内容零变化时重写是纯浪费。
    ``abandoned`` 闸门与 ``save_storage`` 同源。
    """
    try:
        payload = _build_payload(plugin)
    except Exception as exc:
        logger.error("[%s] failed to prepare state snapshot: %s", PLUGIN_ID, exc, exc_info=True)
        return
    if sessions_payload_matches(plugin._storage_path, payload):
        return
    if not write_json_atomic(
        plugin._storage_path,
        payload,
        abandoned=lambda: plugin._abandon_disk_writes,
    ):
        if not plugin._abandon_disk_writes:
            logger.warning(
                "[%s] initial state save failed path=%s", PLUGIN_ID, plugin._storage_path
            )


async def save_storage(plugin: SelfInitiatedReplyPlugin) -> None:
    """落盘会话状态；内容与磁盘一致时跳过（无谓 fsync 与 mtime 扰动）。

    放弃判据按**任务身份**而不是全局标志：``terminate()`` 里被豁免的 critical
    task 自己放弃写入（它慢到越过关停窗口，落地会用陈旧快照覆盖新实例刚写出的
    state.json），而随后执行的最终落盘是另一个任务、必须照常写——它在
    ``terminate()`` 返回之前完成，不暴露在那个跨实例竞态里。用全局标志会把
    最终落盘一起挡掉，静默丢掉「最后一次记录之后又有内存变更」。

    ``_terminated`` 是本实例已经关停完毕的终态闸门：此后的写入者都只可能是
    比关停窗口更晚收尾的任务（如 check 的账本终态），它们手里的快照必然陈旧。
    """
    async with plugin._save_lock:
        current = asyncio.current_task()

        def abandoned() -> bool:
            if plugin._abandon_disk_writes or plugin._terminated:
                return True
            return current is not None and current in plugin._spared_critical_tasks

        if abandoned():
            # 放弃是主动决策，不抛异常、不记错误。
            logger.debug("[%s] state save skipped: instance abandoned", PLUGIN_ID)
            return
        payload = _build_payload(plugin)
        # 跳写判据的读+解析也进线程：state.json 可达数十 MB（白名单规模 ×
        # recent_message_limit × 单条长度），同步跑在事件循环上会阻塞该进程内
        # 所有会话与 Web 面板——写盘一侧的同一纪律见 tests/test_cleanup_nonblocking。
        if await asyncio.to_thread(sessions_payload_matches, plugin._storage_path, payload):
            return
        write_task = asyncio.create_task(
            asyncio.to_thread(
                write_json_atomic,
                plugin._storage_path,
                payload,
                abandoned=abandoned,
            )
        )
        try:
            success = await asyncio.shield(write_task)
        except asyncio.CancelledError:
            # 等 shield 保护的那次写完只为决定要不要留失败日志；取消必须
            # 原样重放，改写成 OSError 会把「被取消」上报成「写盘失败」，
            # 调用方会落到 except Exception 分支，取消路径的回滚语义丢失。
            #
            # 等待必须继续经 shield：裸 ``await write_task`` 时第二次 cancel()
            # （terminate 对 _cancel_background_tasks 调两次；_wait_background
            # _tasks 对超过 SHUTDOWN_TASK_DRAIN_TIMEOUT_SEC 的任务再调一次）会落到
            # 写盘任务上，CancelledError 从本块穿出 → **提前放开 _save_lock**，
            # 而写盘线程还在跑。它随后 os.replace 的陈旧快照会覆盖这期间更新的
            # 一次落盘（terminate 的最终落盘、乃至新实例刚写出的 state.json），
            # 且非 critical 任务不受 _spared_critical_tasks/_abandon_disk_writes
            # 保护——正是这两道闸门唯一没覆盖的写方。
            while True:
                try:
                    success = await asyncio.shield(write_task)
                    break
                except asyncio.CancelledError:
                    if write_task.done():
                        # 写盘任务本身被取消（如事件循环收尾统一取消）：没有
                        # "等它落地"可言。继续循环会在已完成的 Future 上空转，
                        # 且不再让出事件循环——整个进程卡死。
                        raise
                    continue
            if not success and not abandoned():
                logger.warning(
                    "[%s] state save failed while cancelled: %s", PLUGIN_ID, plugin._storage_path
                )
            raise
        if not success and not abandoned():
            raise OSError(f"状态文件写入失败：{plugin._storage_path}")


async def async_sync_whitelist(plugin: SelfInitiatedReplyPlugin) -> bool:
    """落盘当前配置：文件写盘进线程，宿主同步留事件循环（见 storage.apersist）。"""
    if not await apersist_settings_config(plugin._config_path, plugin.config, plugin.settings):
        raise OSError(f"配置文件写入失败：{plugin._config_path}")
    return True


async def persist_enabled(plugin: SelfInitiatedReplyPlugin, enabled: bool) -> None:
    """双写 enabled + runtime_enabled；失败经 plugin._persist_config 回滚。"""
    old_enabled = plugin.settings.enabled
    old_runtime = plugin.runtime_enabled
    plugin.settings.enabled = enabled
    plugin.runtime_enabled = enabled

    async def rollback() -> None:
        plugin.settings.enabled = old_enabled
        plugin.runtime_enabled = old_runtime
        try:
            await plugin._persist_config()
        except Exception as rollback_exc:
            logger.error(
                "[%s] enabled=%s rollback persistence failed: %s",
                PLUGIN_ID,
                enabled,
                rollback_exc,
            )

    try:
        await plugin._persist_config()
    except asyncio.CancelledError:
        # 取消同样要回滚：CancelledError 不落 except Exception，而 enabled 已在
        # 内存翻面。少了这一步，§6.1 承诺的「重启后保持」反向变成「重启后回落」。
        # 回滚必须跑完才算数：单次 shield 在取消瞬间就抛，rollback 成游离任务在
        # 锁外继续，能拿到锁并完整落盘的一次后续写入随后被它用旧值撤销。
        await await_rollback(asyncio.ensure_future(rollback()))
        raise
    except Exception:
        # 回滚同样必须跑完：rollback 先在内存翻回旧值再重写磁盘，取消若落在第二次
        # 写盘上就只剩内存那半，磁盘留着新值——重启后 /off 复活。与上方取消分支、
        # whitelist 的回滚同一纪律。
        await await_rollback(asyncio.ensure_future(rollback()))
        raise


async def apply_enabled_topology(plugin: SelfInitiatedReplyPlugin, *, enabled: bool) -> None:
    """enabled 切换后的任务拓扑单源：开则起巡检与图片清理，关则停。

    ``enabled`` 是调用方已判定"本次生效值真正变化"后的结果（webapi 传新的
    持久值，指令路径传落盘后的当前值）：本函数不再自行比较新旧，重复提交相同
    值的短路判定留在调用方，两处路径因此共享同一段拓扑动作。
    """
    if enabled:
        plugin._scheduler.ensure_patrol()
        plugin._scheduler.ensure_image_cleanup()
    else:
        plugin._cancel_delay_tasks()
        await plugin._scheduler.stop_patrol()


def track_background_task(
    plugin: SelfInitiatedReplyPlugin, coro: Coroutine[Any, Any, Any]
) -> asyncio.Task[Any] | None:
    if not plugin._can_start_tasks():
        try:
            coro.close()
        except Exception:
            pass
        return None
    return _register_task(plugin, coro, critical=False)
