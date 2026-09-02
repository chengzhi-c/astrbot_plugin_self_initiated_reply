"""路径、会话状态、持久化与后台任务：从 main 抽出的状态面。"""

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
    build_sessions_payload,
    sync_config_whitelist,
    write_sessions_payload,
)
from .utils import event_sender_id, event_sender_name, session_group_id

if TYPE_CHECKING:
    from .main import SelfInitiatedReplyPlugin


def resolve_paths(
    config_obj: Any,
    *,
    get_config_path: Any = None,
    get_plugin_data_path: Any = None,
) -> tuple[Path, Path]:
    """Resolve config + state.json paths from host root, with legacy fallback."""
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
    return config_path, plugin_data_path / "state.json"


def refresh_admin_ids(plugin: SelfInitiatedReplyPlugin) -> set[str]:
    """时间窗 + mtime 双缓存热读管理员列表。"""
    now = now_ts()
    if now - plugin._admin_probe_ts < ADMIN_REFRESH_WINDOW_SEC:
        return plugin._admin_ids
    plugin._admin_probe_ts = now
    path = plugin._data_path / "cmd_config.json"
    try:
        if path.exists():
            mtime = path.stat().st_mtime
            if mtime == plugin._admin_file_mtime:
                return plugin._admin_ids
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            admins = data.get("admins_id", []) if isinstance(data, dict) else []
            plugin._admin_ids = {str(item).strip() for item in admins if str(item).strip()}
            plugin._admin_file_mtime = mtime
    except Exception as exc:
        logger.debug("[%s] load admins failed path=%s error=%s", PLUGIN_ID, path, exc)
    return plugin._admin_ids


def track_critical_task(
    plugin: SelfInitiatedReplyPlugin, coro: Coroutine[Any, Any, Any]
) -> asyncio.Task[Any]:
    """Register a persistence task even while shutdown is already stopping."""
    task: asyncio.Task[Any] = asyncio.create_task(coro)
    plugin._background_tasks.add(task)
    plugin._critical_tasks.add(task)
    task.add_done_callback(plugin._background_tasks.discard)
    task.add_done_callback(plugin._critical_tasks.discard)
    return task


def state_for(plugin: SelfInitiatedReplyPlugin, umo: str) -> SessionState:
    state = plugin.sessions.get(umo)
    if state is None:
        legacy_key = session_group_id(umo)
        if legacy_key:
            state = plugin.sessions.pop(legacy_key, None)
    if state is None:
        state = SessionState(recent=deque(maxlen=plugin.settings.recent_message_limit))
        plugin.sessions[umo] = state
    else:
        plugin.sessions[umo] = state
        limit = plugin.settings.recent_message_limit
        if state.recent.maxlen != limit:
            state.recent = deque(state.recent, maxlen=limit)
    state.refresh_day()
    return state


def append_recent_user_message(
    plugin: SelfInitiatedReplyPlugin,
    event: Any,
    *,
    state_key: str,
    clean_text: str,
    active_at: float | None = None,
) -> float:
    """Write last_active + recent user record. Does not advance generation."""
    stamped = now_ts() if active_at is None else active_at
    state = plugin._state_for(state_key)
    state.last_active_at = stamped
    state.last_active_sender_id = event_sender_id(event)
    state.recent.append(
        MessageRecord(
            role="user",
            name=event_sender_name(event),
            sender_id=state.last_active_sender_id,
            text=clean_text,
            at=stamped,
        )
    )
    return stamped


def save_storage_snapshot(plugin: SelfInitiatedReplyPlugin) -> bool:
    # 直接使用本模块全局名：测试 patch ``plugin_state.write_sessions_payload`` 即可生效。
    try:
        payload = build_sessions_payload(
            plugin.sessions,
            plugin.settings.whitelist,
            plugin.settings.recent_message_limit,
        )
        return write_sessions_payload(plugin._storage_path, payload)
    except Exception as exc:
        logger.error("[%s] failed to prepare state snapshot: %s", PLUGIN_ID, exc, exc_info=True)
        return False


def save_storage_sync(plugin: SelfInitiatedReplyPlugin) -> None:
    if not save_storage_snapshot(plugin):
        logger.warning("[%s] initial state save failed path=%s", PLUGIN_ID, plugin._storage_path)


async def save_storage(plugin: SelfInitiatedReplyPlugin) -> None:
    async with plugin._save_lock:
        payload = build_sessions_payload(
            plugin.sessions,
            plugin.settings.whitelist,
            plugin.settings.recent_message_limit,
        )
        write_task = asyncio.create_task(
            asyncio.to_thread(write_sessions_payload, plugin._storage_path, payload)
        )
        try:
            success = await asyncio.shield(write_task)
        except asyncio.CancelledError:
            success = await write_task
            if not success:
                raise OSError(f"状态文件写入失败：{plugin._storage_path}") from None
            raise
        if not success:
            raise OSError(f"状态文件写入失败：{plugin._storage_path}")


def sync_whitelist(plugin: SelfInitiatedReplyPlugin) -> bool:
    if not sync_config_whitelist(plugin._config_path, plugin.config, plugin.settings):
        raise OSError(f"配置文件写入失败：{plugin._config_path}")
    return True


async def persist_enabled(plugin: SelfInitiatedReplyPlugin, enabled: bool) -> None:
    """双写 enabled + runtime_enabled；失败经 plugin._sync_whitelist 回滚。"""
    old_enabled = plugin.settings.enabled
    old_runtime = plugin.runtime_enabled
    plugin.settings.enabled = enabled
    plugin.runtime_enabled = enabled
    try:
        plugin._sync_whitelist()
    except Exception:
        plugin.settings.enabled = old_enabled
        plugin.runtime_enabled = old_runtime
        try:
            plugin._sync_whitelist()
        except Exception as rollback_exc:
            logger.error(
                "[%s] enabled=%s rollback persistence failed: %s",
                PLUGIN_ID,
                enabled,
                rollback_exc,
            )
        raise


def track_background_task(
    plugin: SelfInitiatedReplyPlugin, coro: Coroutine[Any, Any, Any]
) -> asyncio.Task[Any] | None:
    if not plugin._can_start_tasks():
        try:
            coro.close()
        except Exception:
            pass
        return None
    task: asyncio.Task[Any] = asyncio.create_task(coro)
    plugin._background_tasks.add(task)
    task.add_done_callback(plugin._background_tasks.discard)
    return task
