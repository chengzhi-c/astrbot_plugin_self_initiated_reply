"""配置与会话状态的持久化。

拥有：状态文件的原子写入（临时文件 + fsync + ``os.replace``）、版本不符时
先备份再降级、宿主配置对象的读取与回写、白名单在配置与状态间的同步。

不解释状态的语义，字段含义由 ``models`` 定义，何时落盘由调用方决定。
本模块只保证「写下去的不会写坏，读上来的形状可信」。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from collections import deque
from collections.abc import Callable
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
    history_display_name,
    now_ts,
)
from .utils import (
    is_full_umo,
    redact_exc_text,
    session_group_id,
    session_whitelisted,
    whitelist_storage_key,
)


def _update_config_obj(config_obj: Any, data: dict[str, Any]) -> bool:
    try:
        config_obj.clear()
        config_obj.update(data)
        return True
    except Exception as exc:
        logger.warning(
            "[%s] failed to update AstrBot config object: %s",
            PLUGIN_ID,
            redact_exc_text(exc),
        )
        return False


def _persist_config_obj(config_obj: Any, data: dict[str, Any]) -> bool:
    save_config = getattr(config_obj, "save_config", None)
    if save_config is None:
        return True
    try:
        save_config(data)
        return True
    except Exception as exc:
        logger.warning("[%s] failed to save AstrBot config: %s", PLUGIN_ID, redact_exc_text(exc))
    return False


def _json_file_matches(path: Path, expected: dict[str, Any]) -> bool:
    """磁盘 JSON 已解析内容与期望字典是否逐字等价（跳写判据的唯一实现）。

    文件缺失/损坏/非对象一律返回 False，确保首启创建与旧形状迁移照常写。
    """
    try:
        disk = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(disk, dict) and disk == expected


def config_file_matches(path: Path, settings: Settings) -> bool:
    """启动跳写判据：磁盘配置已解析内容与 ``Settings`` 序列化结果是否逐字等价。

    只有等价才可跳过启动期的规范化落盘。比较用 ``to_config_dict()`` 的正式键形态，
    集合已按排序输出，与落盘字节同源。
    """
    return _json_file_matches(path, settings.to_config_dict())


def sessions_payload_matches(path: Path, payload: dict[str, Any]) -> bool:
    """状态文件跳写判据（与 ``config_file_matches`` 共用同一实现）：等价才跳。"""
    return _json_file_matches(path, payload)


# Windows 上实时扫描/索引器会短暂持有刚写出的目标文件，``os.replace`` 随之抛
# ``PermissionError``。重试几次即可穿过那个窗口；只重试 PermissionError，
# 磁盘写满等确定性错误重试无意义。
_REPLACE_RETRY_ATTEMPTS = 5
_REPLACE_RETRY_DELAY_SEC = 0.02


def replace_with_retry(src: Path, dst: Path) -> None:
    """``os.replace`` 的瞬时占用重试包装；重试次数与间隔见上方常量。"""
    for attempt in range(_REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SEC)


def _read_text_with_retry(path: Path) -> str:
    """``Path.read_text`` 的瞬时占用重试包装。

    Windows 上杀毒软件、并发读写的另一个进程、以及刚被 ``os.replace`` 换掉的
    句柄都会让读瞬时失败（``PermissionError``）。这类失败**不代表
    文件内容坏了**：不重试就落进"损坏→备份→空状态"分支，等于把一份完好的状态
    直接清空。
    """
    for attempt in range(_REPLACE_RETRY_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8-sig")
        except PermissionError:
            # 只重试「被占用」这一类瞬时失败。网络盘的 EIO/超时属于真故障，
            # 重试 5 次只会把启动阻塞放大 5 倍。
            if attempt == _REPLACE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SEC)
    raise AssertionError("unreachable")


def _backup_unmigrated_legacy_state(path: Path, keys: set[str]) -> None:
    """Keep the source file before filtering ambiguous legacy session keys."""
    backup = path.with_name(f"{path.name}.unmigrated-{time.time_ns()}")
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        _block_writes(path)
        logger.error(
            "[%s] could not preserve unmigrated legacy sessions %s; state saving is disabled: %s",
            PLUGIN_ID,
            sorted(keys),
            redact_exc_text(exc),
        )
        return
    logger.warning(
        "[%s] ambiguous legacy session keys %s were preserved in %s before filtering",
        PLUGIN_ID,
        sorted(keys),
        backup.name,
    )


# 本次运行中读取失败过的路径。必须是内存态而非文件锁：读只在启动发生一次，
# 文件锁得靠「下次读成功」才撤，一旦某次瞬时读失败（占用/杀软扫描），锁会跨重启
# 一直留着，插件从此再也不落盘。内存态下次启动自然清零，重试即恢复。
_write_blocked: set[str] = set()


def _block_writes(path: Path) -> None:
    """读取失败后禁止本次运行写回该路径。

    读失败时内存里是空状态，此刻写回去就是拿空白覆盖用户的配额、冷却与历史。
    只留副本不够——副本自己也可能因为同一把文件锁而失败。
    """
    _write_blocked.add(str(path))
    logger.error(
        "[%s] state file could not be read; saving is disabled until the next restart",
        PLUGIN_ID,
    )


def _unblock_writes(path: Path) -> None:
    _write_blocked.discard(str(path))


def fsync_dir(path: Path) -> None:
    """replace 之后补一次目录 fsync（尽力而为）。

    只 fsync 文件不够：掉电时数据块已落盘而 rename 丢失，
    “原子发布”只对并发读成立、对持久化不成立。Windows 打不开目录
    （CRT 需 FILE_FLAG_BACKUP_SEMANTICS），那里靠 NTFS 日志覆盖 rename；
    打不开与 fsync 失败都按尽力而为处理——文件已经发布成功，让它退化成
    少一层持久性保证，而不是把整次写入报成失败。
    """
    try:
        dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def write_json_atomic(
    path: Path,
    data: dict[str, Any],
    *,
    abandoned: Callable[[], bool] | None = None,
) -> bool:
    """原子写 JSON；``abandoned`` 在替换前复查，为真则放弃发布。

    ``abandoned`` 服务插件 reload 的跨实例竞态：旧实例的最终落盘超时被隔离后，
    其慢写落地会用陈旧快照覆盖新实例刚写出的 ``state.json``；闸门在
    ``os.replace`` 之前复查，放弃的是自己的临时文件。
    """
    tmp_path: Path | None = None
    try:
        if str(path) in _write_blocked:
            # 本次启动读这个文件失败过：内存里是空状态，写回去就是覆盖用户的配额
            # 与历史。放弃本次写入，重启后重新读取即可恢复。
            logger.warning(
                "[%s] skipped writing %s: an earlier load failed this run", PLUGIN_ID, path.name
            )
            return False
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
        if abandoned is not None and abandoned():
            logger.debug("[%s] abandoned write dropped path=%s", PLUGIN_ID, path)
            return False
        replace_with_retry(tmp_path, path)
        fsync_dir(path)
        return True
    except (OSError, UnicodeEncodeError, TypeError, ValueError) as exc:
        logger.warning("[%s] failed to write %s: %s", PLUGIN_ID, path, redact_exc_text(exc))
    except Exception as exc:
        # 不开 exc_info：traceback 末尾是 str(exc) 原文，会绕过 redact_exc_text。
        logger.error("[%s] unexpected error writing %s: %s", PLUGIN_ID, path, redact_exc_text(exc))
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return False


def load_config_data(path: Path, config_obj: Any) -> tuple[dict[str, Any], bool]:
    """Load plugin config and report whether startup normalization may write it back."""
    data = dict(config_obj)
    try:
        disk_data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return data, True
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # 配置损坏会被默认值覆盖：白名单、延迟、判断模型全回到出厂值。留下
        # 备份，用户至少能把白名单抄回来。
        logger.warning("[%s] failed to load config file: %s", PLUGIN_ID, redact_exc_text(exc))
        _backup_file(path)
    except OSError as exc:
        logger.warning("[%s] failed to read config file: %s", PLUGIN_ID, redact_exc_text(exc))
        return data, False
    except Exception as exc:
        logger.error("[%s] unexpected error loading config: %s", PLUGIN_ID, redact_exc_text(exc))
        return data, False
    else:
        if isinstance(disk_data, dict):
            data.update(disk_data)
        else:
            logger.warning("[%s] config file is not an object; backing it up", PLUGIN_ID)
            _backup_file(path)
    return data, True


def _backup_file(path: Path) -> None:
    """Move a damaged file aside so the cause is recoverable."""
    try:
        backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
        replace_with_retry(path, backup)
        logger.error("[%s] file backed up to %s", PLUGIN_ID, backup.name)
    except FileNotFoundError:
        # 版本不符分支已把文件移走，调用方随后解析失败时无需重复告警。
        return
    except OSError:
        # 备份失败时不阻断加载，但必须说清后果：损坏文件没被移走，后续写入可能
        # 会把它覆盖掉，用户再无从查证原始数据。
        logger.error(
            "[%s] failed to back up file %s; the damaged file may be overwritten by the next write",
            PLUGIN_ID,
            path,
        )


def _read_state_document(path: Path) -> dict[Any, Any]:
    try:
        data = json.loads(_read_text_with_retry(path))
        if not isinstance(data, dict):
            # 解析成功但不是对象（顶层数组/null/裸标量）同样是被外部改坏的文件：
            # 不备份不记录的话，下一次状态落盘会静默覆盖它，用户无从查证。
            # 空对象 {} 是合法空状态，必须继续走下方解析而不是备份。
            raise json.JSONDecodeError("state document is not a JSON object", str(path), 0)
        file_version = data.get("version")
        # 归一成字符串再比：手改/迁移脚本会写出 "4"，严格 != 把同一版本判成不符，
        # 日志冒出 file=4 expected=4 的假矛盾，还平白备份走原文件。
        if file_version is not None and str(file_version).strip() != str(STATE_VERSION):
            logger.error(
                "[%s] state version mismatch: file=%r(%s) expected=%r(%s); backing up",
                PLUGIN_ID,
                file_version,
                type(file_version).__name__,
                STATE_VERSION,
                type(STATE_VERSION).__name__,
            )
            _backup_file(path)
        _unblock_writes(path)
        raw_sessions = data.get("sessions", {})
        if not isinstance(raw_sessions, dict):
            # 顶层是对象但 sessions 不是对象，同样是被外部改坏的文件：不备份不
            # 记录的话，load_sessions 把它当空表，而 save_storage 发现 payload
            # 不匹配就 os.replace 上去，原始内容被静默覆盖、用户无从查证。
            # 与顶层非对象走同一条备份+ERROR 路径（不静默丢数据）。
            # 键缺失时 get 返回 {}，那是合法空状态，不走到这里。
            raise json.JSONDecodeError("state sessions is not a JSON object", str(path), 0)
        return raw_sessions
    except FileNotFoundError:
        # 明确的文件不存在与 stat() 的其它 OSError 不同：Path.exists() 会把后者
        # 也折成 False，空状态随后落盘就会覆盖原状态。
        _unblock_writes(path)
        return {}
    except OSError as exc:
        # 读不到 ≠ 内容坏了。这条分支绝不能 move 原文件：换掉一份完好的状态
        # 等于把用户的配额、冷却与历史一次清空（Windows 上杀毒/并发读会触发）。
        logger.error("[%s] state file could not be read: %s", PLUGIN_ID, redact_exc_text(exc))
        _block_writes(path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("[%s] failed to load state (backing up): %s", PLUGIN_ID, redact_exc_text(exc))
        _backup_file(path)
        # 原文件已经移到 .corrupt-*，磁盘上不再有会被空状态覆盖的内容。
        _unblock_writes(path)
    except Exception as exc:
        # 不开 exc_info：traceback 末尾是 str(exc) 原文，会把刚脱掉的内容带回同一条
        # 日志（口径同 scheduler.py 的 delayed check 分支）。
        logger.error("[%s] unexpected error loading state: %s", PLUGIN_ID, redact_exc_text(exc))
        _backup_file(path)
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
                name=history_display_name(role, item.get("name")),
                text=text,
                at=as_timestamp(item.get("at"), now=load_now),
            )
        )
    return recent


def _load_session_record(raw: dict[Any, Any], recent_limit: int, load_now: float) -> SessionState:
    state = SessionState(recent=deque(maxlen=recent_limit))
    state.last_active_at = as_timestamp(raw.get("last_active_at"), now=load_now)
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

    顺序固定为「解析全部条目 → 迁移 legacy 裸群号键 → 白名单过滤 → 补空状态」。
    白名单内但文件中缺失的会话补空状态。``recent`` 用 ``maxlen=recent_limit``
    的 deque 承载，配置调小后自动裁剪。

    失败时全部不阻断插件加载（宁可丢历史，不可起不来）：
    1. 文件损坏/编码错误/版本号不符，先备份原文件再继续，版本不符仍尽力解析。
    2. 单个会话条目畸形，记 warning 后跳过该条。
    3. 字段级异常值由 ``as_timestamp`` / ``as_int`` 归一：时间戳钳到
       ``[0, now + MAX_CLOCK_SKEW_SEC]``，远未来值会让会话永久锁死
       （见 ``models.MAX_CLOCK_SKEW_SEC`` 的注释）。

    所有时间戳共用同一个 ``load_now`` 上界，避免同一份文件内的条目钳到
    互不一致的天花板。
    """
    load_now = now_ts()
    sessions: dict[str, SessionState] = {}
    raw_sessions = _read_state_document(path)

    for raw_umo, raw in raw_sessions.items():
        umo = str(raw_umo or "").strip()
        if not umo:
            continue
        if not isinstance(raw, dict):
            # docstring 第 2 条承诺「单个会话条目畸形，记 warning 后跳过该条」。
            # 挤进上面的 continue 会让该条被换成空壳、再由下一次落盘覆盖，
            # 而运维在日志里看不到任何一行。
            logger.warning(
                "[%s] skipped malformed session state %s: entry is %s, expected an object",
                PLUGIN_ID,
                umo,
                type(raw).__name__,
            )
            continue
        try:
            key = whitelist_storage_key(umo)
            sessions[key] = _load_session_record(raw, recent_limit, load_now)
        except Exception as exc:
            logger.warning(
                "[%s] skipped malformed session state %s: %s",
                PLUGIN_ID,
                umo,
                redact_exc_text(exc),
            )

    # 迁移必须先于白名单过滤：裸群号不是任何白名单写法，先过滤就把它丢掉，迁移
    # 再也看不到它，升级用户的当日配额与历史当场归零（version 匹配不备份，下一次
    # 落盘即永久覆盖）。
    # 迁移也必须先于补空状态：反过来时目标键已被塞进空壳「占位」，
    # 迁移判据会把空壳当成"目标已有自己的状态"而丢弃 legacy 数据。
    unmigrated_keys = _migrate_legacy_group_keys(sessions, whitelist)
    filtered_legacy_keys = {
        key for key in unmigrated_keys if not session_whitelisted(key, whitelist)
    }
    # 白名单过滤会删掉这些歧义键，先复制原文件以免下一次落盘永久覆盖状态。
    if filtered_legacy_keys:
        _backup_unmigrated_legacy_state(path, filtered_legacy_keys)

    sessions = {
        key: state for key, state in sessions.items() if session_whitelisted(key, whitelist)
    }

    for umo in whitelist:
        key = str(umo).strip()
        # 只为完整 UMO 补空状态：裸群号只是白名单的通配写法，真实状态键恒为
        # 完整 UMO（whitelist_storage_key 契约），空壳条目无人读写却每轮落盘。
        if is_full_umo(key):
            sessions.setdefault(key, SessionState(recent=deque(maxlen=recent_limit)))
    return sessions


def _migrate_legacy_group_keys(sessions: dict[str, SessionState], whitelist: set[str]) -> set[str]:
    """把历史裸群号键的状态并入唯一匹配的完整 UMO。

    迁移必须一次性在此完成：热路径迁移在多平台同群号时由首个访问者继承整份
    历史，其余平台永远拿不到。候选来源取「载入的会话键 ∪ 白名单里的完整 UMO」
    （后者接住"裸键在盘、目标键尚无记录"的形态）。判据：候选必须**恰好一个**
    才迁移，多平台同群号绝不猜平台；目标键已有记录时只淘汰裸键、不覆盖，
    否则 legacy 的当日配额会顶掉新键的计数。
    """
    candidates: set[str] = set()
    for key in (*sessions, *whitelist):
        candidate = str(key).strip()
        if is_full_umo(candidate):
            candidates.add(candidate)
    unmigrated: set[str] = set()
    for legacy_key in [key for key in sessions if not is_full_umo(key)]:
        matched = {key for key in candidates if session_group_id(key) == legacy_key}
        if len(matched) != 1:
            unmigrated.add(legacy_key)
            # 歧义时不猜平台：白名单里没有完整 UMO 时，此刻不存在可安全选择的目标。
            logger.warning(
                "[%s] legacy session key %s cannot be migrated: %d matching full UMOs; refusing "
                "to guess the platform",
                PLUGIN_ID,
                legacy_key,
                len(matched),
            )
            continue
        target = next(iter(matched))
        if target in sessions:
            # 目标已有记录：只淘汰裸键，不覆盖（否则 legacy 的当日配额会顶掉新键的计数）。
            del sessions[legacy_key]
            continue
        sessions[target] = sessions.pop(legacy_key)
    return unmigrated


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
                    "at": item.at,
                }
                for item in list(state.recent)[-recent_limit:]
            ],
        }
    return payload


def persist_settings_config(path: Path, config_obj: Any, settings: Settings) -> bool:
    """原子落盘当前 Settings 的全部配置（含白名单）并同步宿主配置对象。

    白名单已在 ``Settings.to_config_dict()`` 里按 ``container="set"`` 排序输出，
    此处不再单独处理。
    """
    data = settings.to_config_dict()
    if not write_json_atomic(path, data):
        return False
    return _update_config_obj(config_obj, data) and _persist_config_obj(config_obj, data)


async def apersist_settings_config(path: Path, config_obj: Any, settings: Settings) -> bool:
    """``persist_settings_config`` 的 async 版：只有文件写盘进线程。

    宿主 ``save_config`` 的线程安全性未知，仍在事件循环内同步执行。

    取消时必须等这一次写盘真正落地再重放，理由同 ``save_storage``：裸 await 下
    取消立刻抛错而写盘线程照跑，它随后的 ``os.replace`` 会落到回滚的写盘**之后**
    ——磁盘留下的是已被驳回的新配置，而内存已回滚成旧值，且不会自愈（重启加载
    到这份被驳回的配置，``config_revision`` 由内存算也与磁盘脱节）。
    """
    data = settings.to_config_dict()
    write_task = asyncio.ensure_future(asyncio.to_thread(write_json_atomic, path, data))
    try:
        success = await asyncio.shield(write_task)
    except asyncio.CancelledError:
        # 等待必须继续经 shield：第二次 cancel() 会落到写盘任务上，CancelledError
        # 从本块穿出 → 又回到"立刻抛错而线程照跑"的老问题。
        while True:
            try:
                success = await asyncio.shield(write_task)
                break
            except asyncio.CancelledError:
                if write_task.done():
                    # 被取消的是写盘任务本身（事件循环收尾会取消循环内全部任务），
                    # 没有"等它落地"可言。继续循环会在已完成的 Future 上空转：
                    # await 一个 done 的 Future 不再让出事件循环，进程卡死到强杀。
                    raise
                continue
        raise
    if not success:
        return False
    return _update_config_obj(config_obj, data) and _persist_config_obj(config_obj, data)
