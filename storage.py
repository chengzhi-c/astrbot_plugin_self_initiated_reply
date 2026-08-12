"""配置与会话状态的持久化。

拥有：状态文件的原子写入（临时文件 + fsync + ``os.replace``）、版本不符时
先备份再降级、宿主配置对象的读取与回写、白名单在配置与状态间的同步。

不解释状态的语义——字段含义由 ``models`` 定义，何时落盘由调用方决定。
本模块只保证「写下去的不会写坏，读上来的形状可信」。
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import (
    PLUGIN_ID,
    STATE_VERSION,
    MessageRecord,
    SessionState,
    Settings,
    as_int,
    as_timestamp,
    now_ts,
)
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
            # 宿主配置对象的形状不受本插件约束：items() 可能不是 Mapping 协议
            # （惰性代理、属性代理等）。此处静默是为了继续走下方 dict() 兜底，
            # 两条路都失败才返回空字典——不能在第一条失败时就中断。
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
    save_config = config_obj.save_config
    args: tuple[Any, ...]
    try:
        signature = inspect.signature(save_config)
    except (TypeError, ValueError):
        args = (data,)
    else:
        try:
            signature.bind(data)
            args = (data,)
        except TypeError:
            try:
                signature.bind()
            except TypeError as exc:
                logger.warning("[%s] unsupported AstrBot config save signature: %s", PLUGIN_ID, exc)
                return False
            args = ()
    try:
        save_config(*args)
        return True
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


def _backup_state_file(path: Path) -> None:
    """Move a damaged/incompatible state file aside so the cause is recoverable."""
    try:
        backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
        os.replace(path, backup)
        logger.error("[%s] state file backed up to %s", PLUGIN_ID, backup.name)
    except OSError:
        logger.error("[%s] failed to back up state file %s", PLUGIN_ID, path)


def _read_state_document(path: Path) -> dict[Any, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            file_version = data.get("version")
            if file_version is not None and file_version != STATE_VERSION:
                logger.error(
                    "[%s] state version mismatch: file=%s expected=%s; backing up",
                    PLUGIN_ID,
                    file_version,
                    STATE_VERSION,
                )
                _backup_state_file(path)
            raw_sessions = data.get("sessions", {})
            return raw_sessions if isinstance(raw_sessions, dict) else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.error("[%s] failed to load state (backing up): %s", PLUGIN_ID, exc)
        _backup_state_file(path)
    except Exception as exc:
        logger.error("[%s] unexpected error loading state: %s", PLUGIN_ID, exc, exc_info=True)
        _backup_state_file(path)
    return {}


def _load_recent_records(
    raw_recent: Any, recent_limit: int, load_now: float
) -> deque[MessageRecord]:
    recent: deque[MessageRecord] = deque(maxlen=recent_limit)
    if not isinstance(raw_recent, list):
        return recent
    for item in raw_recent:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        role = str(item.get("role") or "user").strip().lower()
        if role not in {"user", "assistant"}:
            role = "user"
        recent.append(
            MessageRecord(
                role=role,
                name=str(item.get("name") or "用户"),
                text=text,
                sender_id=str(item.get("sender_id") or ""),
                at=as_timestamp(item.get("at"), now=load_now),
            )
        )
    return recent


def _load_session_record(raw: dict[Any, Any], recent_limit: int, load_now: float) -> SessionState:
    state = SessionState(recent=deque(maxlen=recent_limit))
    state.last_active_at = as_timestamp(raw.get("last_active_at"), now=load_now)
    state.last_active_sender_id = str(raw.get("last_active_sender_id") or "")
    state.last_proactive_at = as_timestamp(raw.get("last_proactive_at"), now=load_now)
    state.last_proactive_observed_at = as_timestamp(
        raw.get("last_proactive_observed_at"), now=load_now
    )
    state.last_proactive_text = str(raw.get("last_proactive_text") or "")
    state.daily_key = str(raw.get("daily_key") or state.daily_key)
    state.daily_count = as_int(raw.get("daily_count"), 0)
    state.recent = _load_recent_records(raw.get("recent", []), recent_limit, load_now)
    return state


def load_sessions(path: Path, whitelist: set[str], recent_limit: int) -> dict[str, SessionState]:
    """从 state.json 载入会话状态，只保留仍在白名单内的会话。

    白名单内但文件中缺失的会话补空状态，保证调用方无需处理 KeyError。
    ``recent`` 用 ``maxlen=recent_limit`` 的 deque 承载，配置调小后自动裁剪。

    失败时分三层，全部不阻断插件加载（宁可丢历史，不可起不来）：
    1. 文件损坏 / 编码错误 / 版本号不符 —— 先备份原文件再继续（``_backup_state_file``），
       不静默覆盖用户数据；版本不符仍尽力按当前结构解析，避免丢弃仍兼容的部分。
    2. 单个会话条目畸形 —— 记 warning 后跳过该条，其余会话正常载入。
    3. 字段级异常值（NaN/负数/远未来/未知 role）—— 由 ``as_timestamp`` /
       ``as_int`` 归一，不让脏值进入运行期计算。时间戳钳到
       ``[0, now + MAX_CLOCK_SKEW_SEC]``：状态文件可被手工编辑，远未来值会让
       ``remaining_silence_sec`` 变成数十年、该会话永久锁死（，
       危害与实测见 ``models.MAX_CLOCK_SKEW_SEC`` 的注释）。
       ``daily_count`` 经 ``as_int`` 后带上界，不再接受任意大整数。

    本次载入的所有时间戳共用同一个 ``load_now`` 上界，避免同一份文件内的条目
    因逐条取时钟而钳到互不一致的天花板。
    """
    load_now = now_ts()
    sessions: dict[str, SessionState] = {}
    raw_sessions = _read_state_document(path)

    for raw_umo, raw in raw_sessions.items():
        umo = str(raw_umo or "").strip()
        if not umo or not session_whitelisted(umo, whitelist) or not isinstance(raw, dict):
            continue
        try:
            key = whitelist_storage_key(umo)
            sessions[key] = _load_session_record(raw, recent_limit, load_now)
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
        key = whitelist_storage_key(umo)
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
