"""旧状态键迁移先于白名单过滤，保留计数和历史；版本解析接受当前版本的字符串形式。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .host_stubs import capture_logs, load_modules, messages_at_least

PACKAGE_NAME = "selfreply_migrations_package"

LEGACY_GROUP_ID = "123456"
CANONICAL_UMO = f"qq:GroupMessage:{LEGACY_GROUP_ID}"
RECENT_TEXTS = ["在吗", "今晚几点开黑"]


def _load_modules():
    return load_modules(PACKAGE_NAME, "models", "utils", "storage")


def _write_legacy_state(path: Path, *, daily_count: int = 7) -> None:
    """升级前的 state.json：状态键是裸群号，其余字段形状与现役版本一致。"""
    _, _, storage = _load_modules()
    path.write_text(
        json.dumps(
            {
                "version": storage.STATE_VERSION,
                "sessions": {
                    LEGACY_GROUP_ID: {
                        "last_active_at": 1000.0,
                        "daily_key": "2024-01-01",
                        "daily_count": daily_count,
                        "recent": [
                            {"role": "user", "text": text, "at": 1000.0 + index}
                            for index, text in enumerate(RECENT_TEXTS)
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_legacy_bare_group_key_is_filtered_when_group_left_whitelist(tmp_path: Path) -> None:
    """群已不在白名单：迁移出的规范键必须照常被过滤，不复活已移除的会话。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    _write_legacy_state(path)
    other_umo = "qq:GroupMessage:999"

    sessions = storage.load_sessions(path, {other_umo}, 5)

    assert set(sessions) == {other_umo}, f"已移除会话复活：{sorted(sessions)}"
    assert sessions[other_umo].daily_count == 0
    assert not list(sessions[other_umo].recent)


def test_legacy_migration_is_idempotent_across_load_and_save(tmp_path: Path) -> None:
    """幂等：重复加载与紧随其后的落盘都不产生重复条目，也不冲刷刚迁过来的计数。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    _write_legacy_state(path)

    first = storage.load_sessions(path, {CANONICAL_UMO}, 5)
    payload = storage.build_sessions_payload(first, {CANONICAL_UMO}, 5)
    assert storage.write_json_atomic(path, payload)

    second = storage.load_sessions(path, {CANONICAL_UMO}, 5)
    assert set(second) == {CANONICAL_UMO}, "重复加载产生了重复条目"
    assert second[CANONICAL_UMO].daily_count == 7, "落盘后再次加载额度丢失"
    assert [item.text for item in second[CANONICAL_UMO].recent] == RECENT_TEXTS


def _write_state(path: Path, version: object) -> None:
    """写一份形状与现役版本一致、只有顶层 version 可变的 state.json。"""
    path.write_text(
        json.dumps(
            {
                "version": version,
                "sessions": {
                    CANONICAL_UMO: {
                        "last_active_at": 1000.0,
                        "daily_key": "2024-01-01",
                        "daily_count": 7,
                        "recent": [
                            {"role": "user", "text": text, "at": 1000.0 + index}
                            for index, text in enumerate(RECENT_TEXTS)
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_string_typed_current_version_loads_without_backup(tmp_path: Path, caplog: object) -> None:
    """字符串形式的当前版本按同版本加载，避免误备份有效状态。"""
    _, _, storage = _load_modules()
    for index, raw_version in enumerate(
        (storage.STATE_VERSION, str(storage.STATE_VERSION), f" {storage.STATE_VERSION} ")
    ):
        directory = tmp_path / f"string-version-{index}"
        directory.mkdir()
        path = directory / "state.json"
        _write_state(path, raw_version)

        with capture_logs(caplog, storage.logger, logging.DEBUG):
            sessions = storage.load_sessions(path, {CANONICAL_UMO}, 5)

        assert not list(directory.glob("state.json.corrupt-*")), (
            f"version={raw_version!r} 与 STATE_VERSION 同版本却被备份，用户会误以为状态全丢"
        )
        assert path.exists(), f"version={raw_version!r} 的 state.json 被移走"
        assert "version mismatch" not in "\n".join(messages_at_least(caplog, logging.ERROR))
        assert sessions[CANONICAL_UMO].daily_count == 7, f"version={raw_version!r} 加载后配额丢失"
        assert [item.text for item in sessions[CANONICAL_UMO].recent] == RECENT_TEXTS


def test_genuine_version_mismatch_still_backs_up_and_log_names_types(
    tmp_path: Path, caplog: object
) -> None:
    """不匹配版本保留备份；日志同时标明值和类型以区分同形版本号。"""
    _, _, storage = _load_modules()
    expected_token = f"expected={storage.STATE_VERSION!r}(int)"
    for index, raw_version in enumerate(("5", 3, "not-a-version")):
        directory = tmp_path / f"mismatch-{index}"
        directory.mkdir()
        path = directory / "state.json"
        _write_state(path, raw_version)

        with capture_logs(caplog, storage.logger, logging.DEBUG):
            storage.load_sessions(path, {CANONICAL_UMO}, 5)

        backups = sorted(directory.glob("state.json.corrupt-*"))
        assert len(backups) == 1, f"version={raw_version!r} 不符却未备份，下一次落盘会静默覆盖"
        assert not path.exists(), f"version={raw_version!r} 的原文件未被移走"
        joined = "\n".join(messages_at_least(caplog, logging.ERROR))
        assert f"file={raw_version!r}({type(raw_version).__name__})" in joined, (
            f"日志未标出文件版本的类型：{joined}"
        )
        assert expected_token in joined, f"日志未标出期望版本的类型：{joined}"


def test_unmigratable_legacy_key_warns_and_keeps_the_record(tmp_path: Path, caplog: object) -> None:
    """缺少唯一平台时保留旧记录并告警，避免把历史分给错误平台。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    _write_legacy_state(path, daily_count=9)

    with capture_logs(caplog, storage.logger) as logs:
        sessions = storage.load_sessions(path, {LEGACY_GROUP_ID}, 20)

    assert set(sessions) == {LEGACY_GROUP_ID}, f"裸键应原样保留在盘上对应位置：{set(sessions)}"
    assert sessions[LEGACY_GROUP_ID].daily_count == 9, "数据不该被丢弃"
    warnings = messages_at_least(logs, logging.WARNING)
    assert any(
        LEGACY_GROUP_ID in message and "cannot be migrated" in message for message in warnings
    ), f"缺少裸键迁不走的 WARNING：{warnings}"


def test_ambiguous_legacy_record_is_backed_up_before_whitelist_filtering(tmp_path: Path) -> None:
    """歧义裸键将被白名单过滤时，先留副本再允许后续状态写盘。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    raw = {
        "version": storage.STATE_VERSION,
        "sessions": {
            LEGACY_GROUP_ID: {"daily_count": 9, "recent": [{"text": "旧历史"}]},
            "telegram:GroupMessage:123456": {"daily_count": 4},
        },
    }
    path.write_text(json.dumps(raw), encoding="utf-8")

    sessions = storage.load_sessions(path, {CANONICAL_UMO}, 20)

    assert sessions[CANONICAL_UMO].daily_count == 0
    backups = list(tmp_path.glob("state.json.unmigrated-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == raw


def test_migratable_legacy_key_stays_silent(tmp_path: Path, caplog: object) -> None:
    """能迁移时不得打这条 WARNING，否则每次启动都刷一条无用告警。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    _write_legacy_state(path, daily_count=7)

    with capture_logs(caplog, storage.logger) as logs:
        sessions = storage.load_sessions(path, {CANONICAL_UMO}, 20)

    assert set(sessions) == {CANONICAL_UMO}, f"应迁移到完整 UMO：{set(sessions)}"
    warnings = messages_at_least(logs, logging.WARNING)
    assert not any("cannot be migrated" in message for message in warnings), warnings
