"""Web API、配置读写与 UI 偏好（自 main.py 拆分）。"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
from collections import deque
from functools import partial
from typing import Any

from astrbot.api import logger

try:
    from astrbot.api.web import request
except ImportError:  # pragma: no cover - compatibility with older AstrBot hosts
    from quart import request

from .models import (
    DEFAULT_DECISION_PROMPT_TEMPLATE,
    MAX_CACHED_IMAGE_EVENTS,
    MAX_WHITELIST_ITEM_LEN,
    PLUGIN_ID,
    Settings,
)


def _config_value(config: Any, key: str, default: Any = "") -> Any:
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _provider_config(provider: Any) -> Any:
    if isinstance(provider, dict):
        return provider.get("provider_config") or provider.get("config") or provider
    return getattr(provider, "provider_config", None) or getattr(provider, "config", None) or {}


def _provider_id(provider: Any, fallback_id: str = "") -> str:
    config = _provider_config(provider)
    return str(
        _config_value(config, "id")
        or _config_value(config, "provider_id")
        or getattr(provider, "id", "")
        or getattr(provider, "provider_id", "")
        or fallback_id
        or ""
    ).strip()


def _provider_label(provider: Any, provider_id: str) -> str:
    config = _provider_config(provider)
    label = str(
        _config_value(config, "display_name")
        or _config_value(config, "name")
        or _config_value(config, "model")
        or _config_value(config, "model_name")
        or getattr(provider, "display_name", "")
        or getattr(provider, "name", "")
        or provider_id
    ).strip()
    return f"{label} ({provider_id})" if label and label != provider_id else label or provider_id


def _provider_option(provider: Any, fallback_id: str = "") -> dict[str, str] | None:
    provider_id = _provider_id(provider, fallback_id)
    if not provider_id:
        return None
    return {"id": provider_id, "label": _provider_label(provider, provider_id)}


def _provider_items(source: Any) -> list[Any]:
    if isinstance(source, dict):
        return list(source.items())
    return list(source or [])


def _collect_provider_options(plugin) -> list[dict[str, str]]:
    providers: list[Any] = []
    get_all = getattr(plugin.context, "get_all_providers", None)
    if callable(get_all):
        try:
            providers = _provider_items(get_all())
        except Exception as exc:
            logger.debug("[%s] get_all_providers failed: %s", PLUGIN_ID, exc)

    if not providers:
        providers = _providers_from_manager(plugin)

    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for provider in providers:
        fallback_id = ""
        if isinstance(provider, tuple) and len(provider) == 2:
            fallback_id = str(provider[0] or "")
            provider = provider[1]
        option = _provider_option(provider, fallback_id)
        if not option or option["id"] in seen:
            continue
        seen.add(option["id"])
        options.append(option)
    return sorted(options, key=lambda item: item["label"].lower())


def _providers_from_manager(plugin) -> list[Any]:
    provider_manager = getattr(plugin.context, "provider_manager", None)
    inst_map = getattr(provider_manager, "inst_map", None)
    if isinstance(inst_map, dict):
        return _provider_items(inst_map)
    return []


async def _api_get_config(plugin):
    """返回当前配置。"""
    try:
        min_context_messages = plugin.settings.decision_history_min_messages
        return {
            "ok": True,
            # enabled 是持久配置；runtime_enabled 是 /on /off 临时运行态。
            # 返回持久值可避免前端全量保存把临时暂停固化成永久关闭。
            "enabled": plugin.settings.enabled,
            "runtime_enabled": plugin.runtime_enabled,
            "decision_model_enabled": plugin.settings.decision_model_enabled,
            "judge_provider_id": plugin.settings.judge_provider_id,
            "decision_prompt_template": plugin.settings.decision_prompt_template,
            "decision_prompt_default": DEFAULT_DECISION_PROMPT_TEMPLATE,
            "decision_temperature": plugin.settings.decision_temperature,
            "decision_timeout_sec": plugin.settings.decision_timeout_sec,
            "min_context_messages": min_context_messages,
            # Backward-compatible alias for older unified-manager frontend builds.
            "proactive_threshold": min_context_messages,
            "message_delay_sec": plugin.settings.message_delay_sec,
            "min_silence_sec": plugin.settings.min_silence_sec,
            "cooldown_sec": plugin.settings.cooldown_sec,
            "patrol_inactive_after_sec": plugin.settings.patrol_inactive_after_sec,
            "proactive_inherit_tools": plugin.settings.proactive_inherit_tools,
            # Backward-compatible aliases for older custom-page builds.
            "idle_trigger_seconds": plugin.settings.message_delay_sec,
            "cooldown_seconds": plugin.settings.cooldown_sec,
            "whitelist": list(plugin.settings.whitelist),
            "pipeline_mode": True,
            "vision_judge_enabled": plugin.settings.vision_judge_enabled,
            "vision_main_enabled": plugin.settings.vision_main_enabled,
            # 聚合值，保留给旧版前端
            "vision_enabled": plugin.settings.vision_enabled,
            "vision_provider_id": plugin.settings.vision_provider_id,
            "vision_judge_provider_id": plugin.settings.vision_judge_provider_id,
            "vision_skip_stickers": plugin.settings.vision_skip_stickers,
            "vision_max_images": plugin.settings.vision_max_images,
            "vision_image_age_sec": plugin.settings.vision_image_age_sec,
            "vision_timeout_sec": plugin.settings.vision_timeout_sec,
        }
    except Exception as exc:
        logger.warning("[%s] api get config failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "error": str(exc)}


async def _api_providers(plugin):
    """返回当前可选聊天 Provider。"""
    try:
        return {"ok": True, "providers": _collect_provider_options(plugin)}
    except Exception as exc:
        logger.warning("[%s] api providers failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "providers": [], "error": str(exc)}


async def _api_cleanup_image_cache(plugin):
    """手动清理过期图片缓存；不删除有效窗口内仍受保护的源。"""
    if plugin._stopping:
        return {"ok": False, "error": "插件正在关闭"}
    try:
        removed = await plugin._run_image_cleanup()
        return {
            "ok": True,
            "removed": removed,
            "max_age_sec": int(plugin.settings.vision_image_age_sec),
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[%s] manual image cache cleanup failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "error": "图片缓存清理失败"}


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是布尔值")
    return value


def _load_ui_theme(plugin) -> str:
    """从 ui_prefs.json 加载主题偏好；损坏或缺失回退 auto。"""
    try:
        raw = json.loads(plugin._ui_prefs_path.read_text(encoding="utf-8"))
        theme = str(raw.get("theme", "auto")).strip()
    except Exception:
        return "auto"
    return theme if theme in {"auto", "light", "dark"} else "auto"


def _save_ui_theme(plugin, theme: str) -> bool:
    """原子写入主题偏好（tmp + replace）。"""
    try:
        tmp = plugin._ui_prefs_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"theme": theme}), encoding="utf-8")
        tmp.replace(plugin._ui_prefs_path)
        return True
    except Exception as exc:
        logger.warning("[%s] ui theme save failed: %s", PLUGIN_ID, exc)
        return False


async def _api_get_ui_theme(plugin):
    """获取插件页面 UI 主题偏好。"""
    return {"ok": True, "theme": plugin._ui_theme}


async def _api_post_ui_theme(plugin):
    """更新插件页面 UI 主题偏好（持久化到 ui_prefs.json）。"""
    try:
        data = await _request_json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {"ok": False, "error": "请求体必须是 JSON 对象"}
    theme = str(data.get("theme", "")).strip()
    if theme not in {"auto", "light", "dark"}:
        return {"ok": False, "error": f"无效主题: {theme!r}"}
    if theme != plugin._ui_theme:
        if not _save_ui_theme(plugin, theme):
            return {"ok": False, "error": "主题写入失败"}
        plugin._ui_theme = theme
    return {"ok": True, "theme": plugin._ui_theme}


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} 必须是整数") from exc


def _strict_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} 必须是有限数字")
    return parsed


async def _request_json() -> Any:
    json_reader = getattr(request, "json", None)
    if callable(json_reader):
        try:
            return await json_reader(default={})
        except TypeError:
            return await json_reader()
    get_json = getattr(request, "get_json", None)
    if callable(get_json):
        return await get_json(silent=True)
    raise RuntimeError("当前 AstrBot Web API 不支持 JSON 请求读取")


async def _api_post_config(plugin):
    async with plugin._config_lock:
        if plugin._stopping:
            return {"ok": False, "error": "插件正在关闭"}
        return await _api_post_config_locked(plugin)


async def _api_post_config_locked(plugin):
    """更新配置。"""
    try:
        data = await _request_json()
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")

        updates: dict[str, Any] = {}
        if "enabled" in data:
            updates["enabled"] = _strict_bool(data["enabled"], "enabled")
        if "decision_model_enabled" in data:
            updates["decision_model_enabled"] = _strict_bool(
                data["decision_model_enabled"], "decision_model_enabled"
            )
        if "judge_provider_id" in data:
            updates["judge_provider_id"] = str(data["judge_provider_id"] or "").strip()
        if "decision_prompt_template" in data:
            prompt = str(data["decision_prompt_template"] or "").strip()
            updates["decision_prompt_template"] = prompt or DEFAULT_DECISION_PROMPT_TEMPLATE
        if "decision_temperature" in data:
            updates["decision_temperature"] = _strict_float(
                data["decision_temperature"], "decision_temperature"
            )
        if "decision_timeout_sec" in data:
            updates["decision_timeout_sec"] = _strict_float(
                data["decision_timeout_sec"], "decision_timeout_sec"
            )
        cooldown_value = data.get("cooldown_sec", data.get("cooldown_seconds"))
        if cooldown_value is not None:
            updates["cooldown_sec"] = _strict_int(cooldown_value, "cooldown_sec")
        message_delay_value = data.get("message_delay_sec", data.get("idle_trigger_seconds"))
        if message_delay_value is not None:
            updates["message_delay_sec"] = _strict_int(message_delay_value, "message_delay_sec")
        if "min_silence_sec" in data:
            updates["min_silence_sec"] = _strict_int(data["min_silence_sec"], "min_silence_sec")
        if "patrol_inactive_after_sec" in data:
            updates["patrol_inactive_after_sec"] = _strict_int(
                data["patrol_inactive_after_sec"], "patrol_inactive_after_sec"
            )
        min_context_value = data.get("min_context_messages", data.get("proactive_threshold"))
        if min_context_value is not None:
            updates["decision_history_min_messages"] = _strict_int(
                min_context_value, "decision_history_min_messages"
            )
        if "proactive_inherit_tools" in data:
            updates["proactive_inherit_tools"] = _strict_bool(
                data["proactive_inherit_tools"], "proactive_inherit_tools"
            )
        if "vision_judge_enabled" in data:
            updates["vision_judge_enabled"] = _strict_bool(
                data["vision_judge_enabled"], "vision_judge_enabled"
            )
        if "vision_main_enabled" in data:
            updates["vision_main_enabled"] = _strict_bool(
                data["vision_main_enabled"], "vision_main_enabled"
            )
        if "vision_enabled" in data and not (
            "vision_judge_enabled" in data or "vision_main_enabled" in data
        ):
            # 旧前端只会发聚合开关，同步到两个新开关
            legacy_vision = _strict_bool(data["vision_enabled"], "vision_enabled")
            updates["vision_judge_enabled"] = legacy_vision
            updates["vision_main_enabled"] = legacy_vision
        if "vision_provider_id" in data:
            updates["vision_provider_id"] = str(data["vision_provider_id"] or "").strip()
        if "vision_judge_provider_id" in data:
            updates["vision_judge_provider_id"] = str(
                data["vision_judge_provider_id"] or ""
            ).strip()
        if "vision_skip_stickers" in data:
            updates["vision_skip_stickers"] = _strict_bool(
                data["vision_skip_stickers"], "vision_skip_stickers"
            )
        if "vision_max_images" in data:
            updates["vision_max_images"] = _strict_int(
                data["vision_max_images"], "vision_max_images"
            )
        if "vision_image_age_sec" in data:
            updates["vision_image_age_sec"] = _strict_int(
                data["vision_image_age_sec"], "vision_image_age_sec"
            )
        if "vision_timeout_sec" in data:
            updates["vision_timeout_sec"] = _strict_float(
                data["vision_timeout_sec"], "vision_timeout_sec"
            )
        if "whitelist" in data:
            if not isinstance(data["whitelist"], list):
                raise ValueError("whitelist 必须是数组")
            items: list[str] = []
            for item in data["whitelist"]:
                raw = str(item).strip()
                if not raw:
                    continue
                if len(raw) > MAX_WHITELIST_ITEM_LEN:
                    raise ValueError(f"白名单条目过长: {raw[:20]}…")
                if re.search(r"[\x00-\x1f\"'\\]", raw):
                    raise ValueError("白名单条目含非法字符")
                items.append(raw)
            updates["whitelist"] = items

        old_settings = copy.deepcopy(plugin.settings)
        old_runtime_enabled = plugin.runtime_enabled
        old_last_events = dict(plugin._last_events)
        old_last_event_at = dict(plugin._last_event_at)
        old_recent_image_events = {
            key: deque(values, maxlen=MAX_CACHED_IMAGE_EVENTS)
            for key, values in plugin._recent_image_events.items()
        }
        old_whitelist_runtime_umos = {
            key: set(values) for key, values in plugin._whitelist_runtime_umos.items()
        }
        old_session_generation = dict(plugin._session_generation)
        old_sessions = dict(plugin.sessions)
        old_session_locks = dict(plugin._session_locks)
        old_delay_umos = set(plugin._delay_tasks)
        try:
            candidate = plugin.settings.to_config_dict()
            for key, value in updates.items():
                if key == "whitelist":
                    candidate["whitelist_sessions"] = list(value)
                else:
                    candidate[key] = value
            new_settings = Settings.from_config(candidate)
            vision_changed = any(
                getattr(plugin.settings, key) != getattr(new_settings, key)
                for key in (
                    "vision_judge_enabled",
                    "vision_main_enabled",
                    "vision_provider_id",
                    "vision_judge_provider_id",
                    "vision_timeout_sec",
                )
            )
            plugin.settings = new_settings
            if "whitelist" in updates:
                plugin._replace_whitelist(new_settings.whitelist)
            if vision_changed:
                plugin._image_parsers.clear()
                plugin._image_parser_timeout = None
            if updates:
                plugin._sync_whitelist()
                await plugin._save_storage()

            # 持久 enabled 真正变化才清除临时覆盖并同步任务拓扑；全量表单
            # 重复提交相同值不得影响 /on /off 建立的临时运行态。
            enabled_persisted_changed = (
                "enabled" in updates and old_settings.enabled != new_settings.enabled
            )
            if enabled_persisted_changed:
                plugin.runtime_enabled = new_settings.enabled
                if plugin.runtime_enabled:
                    plugin._ensure_patrol_task()
                    plugin._ensure_image_cleanup_task()
                else:
                    plugin._cancel_delay_tasks()
                    await plugin._stop_patrol_task()
            elif plugin.runtime_enabled:
                plugin._ensure_image_cleanup_task()
            return {"ok": True}
        except Exception:
            plugin.settings = old_settings
            plugin.runtime_enabled = old_runtime_enabled
            plugin._last_events = old_last_events
            plugin._last_event_at = old_last_event_at
            plugin._recent_image_events = old_recent_image_events
            plugin._whitelist_runtime_umos = old_whitelist_runtime_umos
            plugin._session_generation = old_session_generation
            plugin.sessions = old_sessions
            plugin._session_locks = old_session_locks
            # 回滚后重新调度被白名单变更取消的延迟检查（已取消的任务对象
            # 不可复用，只能按默认 message_delay 语义重建）。
            for umo in old_delay_umos:
                if umo in plugin.sessions and not plugin._stopping:
                    try:
                        plugin._schedule_delayed_check(
                            umo, delay_sec=None, trigger="message_delay", force=False
                        )
                    except Exception as re_exc:
                        logger.debug(
                            "[%s] delayed check reschedule failed on rollback session=%s error=%s",
                            PLUGIN_ID,
                            umo,
                            re_exc,
                        )
                else:
                    logger.debug(
                        "[%s] delayed check dropped on rollback session=%s", PLUGIN_ID, umo
                    )
            # 回滚后一律丢弃 parser 缓存，避免残留按失败配置建的实例
            plugin._image_parsers.clear()
            plugin._image_parser_timeout = None
            # 回滚后恢复任务拓扑：禁用路径可能已停掉 patrol/cleanup，
            # 否则出现 runtime_enabled=True 但巡检永久停止的不一致态。
            if old_runtime_enabled and not plugin._stopping:
                plugin._ensure_patrol_task()
                plugin._ensure_image_cleanup_task()
            try:
                plugin._sync_whitelist()
                await plugin._save_storage()
            except Exception as rollback_exc:
                logger.error("[%s] config rollback persistence failed: %s", PLUGIN_ID, rollback_exc)
            raise
    except Exception as exc:
        logger.warning("[%s] api post config failed: %s", PLUGIN_ID, exc)
        return {"ok": False, "error": str(exc)}


async def _api_status(plugin):
    """返回插件集成状态。"""
    return {
        "ok": True,
        "loaded": True,
        "runtime_enabled": plugin.runtime_enabled,
        "whitelist_count": len(plugin.settings.whitelist),
        "pipeline_mode": True,
        "decision_model_enabled": plugin.settings.decision_model_enabled,
    }


def register_web_apis(plugin) -> None:
    """注册统一管理页面所需的 Web API。"""
    register = plugin.context.register_web_api
    route = f"/{PLUGIN_ID}"
    register(
        f"{route}/config",
        partial(_api_get_config, plugin),
        ["GET"],
        "获取主动回复插件配置",
    )
    register(
        f"{route}/config",
        partial(_api_post_config, plugin),
        ["POST"],
        "更新主动回复插件配置",
    )
    register(
        f"{route}/status",
        partial(_api_status, plugin),
        ["GET"],
        "获取插件集成状态",
    )
    register(
        f"{route}/providers",
        partial(_api_providers, plugin),
        ["GET"],
        "获取可选判断模型 Provider",
    )
    register(
        f"{route}/image-cache/cleanup",
        partial(_api_cleanup_image_cache, plugin),
        ["POST"],
        "清理主动回复插件过期图片缓存",
    )
    register(
        f"{route}/ui/theme",
        partial(_api_get_ui_theme, plugin),
        ["GET"],
        "获取插件页面 UI 偏好（主题）",
    )
    register(
        f"{route}/ui/theme",
        partial(_api_post_ui_theme, plugin),
        ["POST"],
        "更新插件页面 UI 偏好（主题）",
    )
    plugin.unified_manager.register(plugin.context, route)


def bind_api_handlers(plugin) -> None:
    """在实例上保留历史方法名，供测试与外部以 plugin._api_* 调用。"""
    plugin._api_get_config = partial(_api_get_config, plugin)
    plugin._api_post_config = partial(_api_post_config, plugin)
    plugin._api_get_ui_theme = partial(_api_get_ui_theme, plugin)
    plugin._api_post_ui_theme = partial(_api_post_ui_theme, plugin)


# 公开入口：main.py 初始化时加载主题偏好。
load_ui_theme = _load_ui_theme
save_ui_theme = _save_ui_theme
