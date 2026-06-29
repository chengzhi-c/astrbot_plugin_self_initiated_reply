from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import MessageRecord, PLUGIN_ID, SessionState, Settings
from .utils import session_whitelisted, whitelist_storage_key


def _config_to_dict(config_obj: Any) -> dict[str, Any]:
    if config_obj is None:
        return {}
    if isinstance(config_obj, dict):
        return dict(config_obj)
    if hasattr(config_obj, "items"):
        try:
            return {str(key): value for key, value in config_obj.items()}
        except Exception:
            pass
    try:
        return dict(config_obj)
    except Exception:
        return {}


def _update_config_obj(config_obj: Any, data: dict[str, Any]) -> None:
    if config_obj is None:
        return
    try:
        if hasattr(config_obj, "clear"):
            config_obj.clear()
            config_obj.update(data)
        else:
            for key, value in data.items():
                config_obj[key] = value
    except Exception:
        pass


def _persist_config_obj(config_obj: Any, data: dict[str, Any]) -> None:
    if config_obj is None or not hasattr(config_obj, "save_config"):
        return
    try:
        config_obj.save_config(data)
    except TypeError:
        try:
            config_obj.save_config()
        except Exception as exc:
            logger.warning("[%s] failed to save AstrBot config: %s", PLUGIN_ID, exc)
    except Exception as exc:
        logger.warning("[%s] failed to save AstrBot config: %s", PLUGIN_ID, exc)


def load_config_data(path: Path, config_obj: Any) -> dict[str, Any]:
    """Load plugin config with the on-disk JSON taking precedence.

    The custom page writes the JSON file directly, while AstrBot also passes a
    dict-like config object into the plugin. Keeping the JSON as the final
    source here prevents a stale in-memory object from overwriting a page save
    during plugin restart.
    """
    data = _config_to_dict(config_obj)
    if path.exists():
        try:
            disk_data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(disk_data, dict):
                data.update(disk_data)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("[%s] failed to load config file: %s", PLUGIN_ID, exc)
        except Exception as exc:
            logger.error("[%s] unexpected error loading config: %s", PLUGIN_ID, exc, exc_info=True)
    return data


def load_sessions(path: Path, whitelist: set[str], recent_limit: int) -> dict[str, SessionState]:
    sessions: dict[str, SessionState] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            raw_sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.warning("[%s] failed to load state: %s", PLUGIN_ID, exc)
            raw_sessions = {}
        except Exception as exc:
            logger.error("[%s] unexpected error loading state: %s", PLUGIN_ID, exc, exc_info=True)
            raw_sessions = {}
        for umo, raw in raw_sessions.items():
            umo = str(umo or "").strip()
            if not umo or not session_whitelisted(umo, whitelist) or not isinstance(raw, dict):
                continue
            umo = whitelist_storage_key(umo, whitelist)
            state = SessionState(recent=deque(maxlen=recent_limit))
            state.last_active_at = float(raw.get("last_active_at") or 0.0)
            state.last_active_sender_id = str(raw.get("last_active_sender_id") or "")
            state.last_proactive_at = float(raw.get("last_proactive_at") or 0.0)
            state.last_proactive_observed_at = float(raw.get("last_proactive_observed_at") or 0.0)
            state.last_proactive_text = str(raw.get("last_proactive_text") or "")
            state.daily_key = str(raw.get("daily_key") or state.daily_key)
            state.daily_count = int(raw.get("daily_count") or 0)
            for item in raw.get("recent", []) or []:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                state.recent.append(
                    MessageRecord(
                        role=str(item.get("role") or "user"),
                        name=str(item.get("name") or "用户"),
                        text=text,
                        sender_id=str(item.get("sender_id") or ""),
                        at=float(item.get("at") or 0.0),
                    )
                )
            sessions[umo] = state
    for umo in whitelist:
        sessions.setdefault(umo, SessionState(recent=deque(maxlen=recent_limit)))
    return sessions


def save_sessions(path: Path, sessions: dict[str, SessionState], whitelist: set[str], recent_limit: int) -> None:
    payload = {"version": 3, "sessions": {}}
    for umo, state in sessions.items():
        if not session_whitelisted(umo, whitelist):
            continue
        key = whitelist_storage_key(umo, whitelist)
        payload["sessions"][key] = {
            "last_active_at": state.last_active_at,
            "last_active_sender_id": state.last_active_sender_id,
            "last_proactive_at": state.last_proactive_at,
            "last_proactive_observed_at": state.last_proactive_observed_at,
            "last_proactive_text": state.last_proactive_text,
            "daily_key": state.daily_key,
            "daily_count": state.daily_count,
            "recent": [
                {
                    "role": item.role,
                    "name": item.name,
                    "text": item.text,
                    "sender_id": item.sender_id,
                    "at": item.at,
                }
                for item in list(state.recent)[-recent_limit:]
            ],
        }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except (OSError, UnicodeEncodeError) as exc:
        logger.warning("[%s] failed to save state: %s", PLUGIN_ID, exc)
    except Exception as exc:
        logger.error("[%s] unexpected error saving state: %s", PLUGIN_ID, exc, exc_info=True)


def migrate_config_file(path: Path, config_obj: Any, settings: Settings) -> None:
    data = settings.to_config_dict()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[%s] failed to migrate config: %s", PLUGIN_ID, exc)
    _update_config_obj(config_obj, data)
    _persist_config_obj(config_obj, data)


def sync_config_whitelist(path: Path, config_obj: Any, settings: Settings) -> None:
    data = settings.to_config_dict()
    data["whitelist_sessions"] = sorted(settings.whitelist)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("[%s] failed to sync config: %s", PLUGIN_ID, exc)
    _update_config_obj(config_obj, data)
    _persist_config_obj(config_obj, data)
