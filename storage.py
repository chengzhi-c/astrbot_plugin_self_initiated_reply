from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import MessageRecord, PLUGIN_ID, STATE_VERSION, SessionState, Settings
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


def _update_config_obj(config_obj: Any, data: dict[str, Any]) -> bool:
    if config_obj is None:
        return True
    try:
        if hasattr(config_obj, "clear"):
            config_obj.clear()
            config_obj.update(data)
        else:
            for key, value in data.items():
                config_obj[key] = value
        return True
    except Exception as exc:
        logger.warning("[%s] failed to update AstrBot config object: %s", PLUGIN_ID, exc)
        return False


def _persist_config_obj(config_obj: Any, data: dict[str, Any]) -> bool:
    if config_obj is None or not hasattr(config_obj, "save_config"):
        return True
    try:
        config_obj.save_config(data)
        return True
    except TypeError:
        try:
            config_obj.save_config()
            return True
        except Exception as exc:
            logger.warning("[%s] failed to save AstrBot config: %s", PLUGIN_ID, exc)
    except Exception as exc:
        logger.warning("[%s] failed to save AstrBot config: %s", PLUGIN_ID, exc)
    return False


def _write_json_atomic(path: Path, data: dict[str, Any]) -> bool:
    tmp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        return True
    except (OSError, UnicodeEncodeError, TypeError, ValueError) as exc:
        logger.warning("[%s] failed to write %s: %s", PLUGIN_ID, path, exc)
    except Exception as exc:
        logger.error("[%s] unexpected error writing %s: %s", PLUGIN_ID, path, exc, exc_info=True)
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return False


def load_config_data(path: Path, config_obj: Any) -> dict[str, Any]:
    """Load plugin config with the on-disk JSON taking precedence."""
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


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


def _backup_state_file(path: Path) -> None:
    """Move a damaged/incompatible state file aside so the cause is recoverable."""
    try:
        backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
        os.replace(path, backup)
        logger.error("[%s] state file backed up to %s", PLUGIN_ID, backup.name)
    except OSError:
        logger.error("[%s] failed to back up state file %s", PLUGIN_ID, path)


def load_sessions(path: Path, whitelist: set[str], recent_limit: int) -> dict[str, SessionState]:
    sessions: dict[str, SessionState] = {}
    raw_sessions: Any = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                file_version = data.get("version")
                if file_version is not None and file_version != STATE_VERSION:
                    # 版本不符：结构可能已变化，静默容错解析会产生"半加载"状态
                    # （字段默认化、计数错位），用户无感知。备份原文件并告警，
                    # 之后按当前结构尽力解析，避免直接丢弃仍兼容的数据。
                    logger.error(
                        "[%s] state version mismatch: file=%s expected=%s; backing up",
                        PLUGIN_ID,
                        file_version,
                        STATE_VERSION,
                    )
                    _backup_state_file(path)
            raw_sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.error("[%s] failed to load state (backing up): %s", PLUGIN_ID, exc)
            _backup_state_file(path)
        except Exception as exc:
            logger.error("[%s] unexpected error loading state: %s", PLUGIN_ID, exc, exc_info=True)
            _backup_state_file(path)
    if not isinstance(raw_sessions, dict):
        raw_sessions = {}

    for raw_umo, raw in raw_sessions.items():
        umo = str(raw_umo or "").strip()
        if not umo or not session_whitelisted(umo, whitelist) or not isinstance(raw, dict):
            continue
        try:
            key = whitelist_storage_key(umo, whitelist)
            state = SessionState(recent=deque(maxlen=recent_limit))
            state.last_active_at = _finite_float(raw.get("last_active_at"))
            state.last_active_sender_id = str(raw.get("last_active_sender_id") or "")
            state.last_proactive_at = _finite_float(raw.get("last_proactive_at"))
            state.last_proactive_observed_at = _finite_float(raw.get("last_proactive_observed_at"))
            state.last_proactive_text = str(raw.get("last_proactive_text") or "")
            state.daily_key = str(raw.get("daily_key") or state.daily_key)
            state.daily_count = _nonnegative_int(raw.get("daily_count"))
            raw_recent = raw.get("recent", [])
            if not isinstance(raw_recent, list):
                raw_recent = []
            for item in raw_recent:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                role = str(item.get("role") or "user").strip().lower()
                if role not in {"user", "assistant"}:
                    role = "user"
                state.recent.append(
                    MessageRecord(
                        role=role,
                        name=str(item.get("name") or "用户"),
                        text=text,
                        sender_id=str(item.get("sender_id") or ""),
                        at=_finite_float(item.get("at")),
                    )
                )
            sessions[key] = state
        except Exception as exc:
            logger.warning("[%s] skipped malformed session state %s: %s", PLUGIN_ID, umo, exc)

    for umo in whitelist:
        sessions.setdefault(str(umo).strip(), SessionState(recent=deque(maxlen=recent_limit)))
    return sessions


def build_sessions_payload(
    sessions: dict[str, SessionState], whitelist: set[str], recent_limit: int
) -> dict[str, Any]:
    """Build an immutable JSON-ready snapshot before moving I/O off-thread."""
    payload: dict[str, Any] = {"version": STATE_VERSION, "sessions": {}}
    for raw_umo, state in list(sessions.items()):
        umo = str(raw_umo or "").strip()
        if not umo or not session_whitelisted(umo, whitelist):
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
    return payload


def write_sessions_payload(path: Path, payload: dict[str, Any]) -> bool:
    return _write_json_atomic(path, payload)


def save_sessions(path: Path, sessions: dict[str, SessionState], whitelist: set[str], recent_limit: int) -> bool:
    try:
        payload = build_sessions_payload(sessions, whitelist, recent_limit)
    except Exception as exc:
        logger.error("[%s] unexpected error preparing state: %s", PLUGIN_ID, exc, exc_info=True)
        return False
    return write_sessions_payload(path, payload)


def migrate_config_file(path: Path, config_obj: Any, settings: Settings) -> bool:
    data = settings.to_config_dict()
    if not _write_json_atomic(path, data):
        return False
    return _update_config_obj(config_obj, data) and _persist_config_obj(config_obj, data)


def sync_config_whitelist(path: Path, config_obj: Any, settings: Settings) -> bool:
    data = settings.to_config_dict()
    data["whitelist_sessions"] = sorted(settings.whitelist)
    if not _write_json_atomic(path, data):
        return False
    return _update_config_obj(config_obj, data) and _persist_config_obj(config_obj, data)
