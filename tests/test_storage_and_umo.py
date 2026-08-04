from __future__ import annotations

import importlib
import json
import logging
import sys
import types
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "selfreply_test_package"


def _install_astrbot_stubs() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    message_components = types.ModuleType("astrbot.api.message_components")

    class AstrMessageEvent:
        pass

    class At:
        pass

    api.logger = logging.getLogger("selfreply-test")
    event.AstrMessageEvent = AstrMessageEvent
    message_components.At = At
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.message_components": message_components,
        }
    )


def _load_modules():
    _install_astrbot_stubs()
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE_NAME] = package
    models = importlib.import_module(f"{PACKAGE_NAME}.models")
    utils = importlib.import_module(f"{PACKAGE_NAME}.utils")
    storage = importlib.import_module(f"{PACKAGE_NAME}.storage")
    return models, utils, storage


def test_tool_call_marker_is_not_sent_as_a_reply() -> None:
    _, utils, _ = _load_modules()
    assert (
        utils.clean_reply("[tool call] send_emoji_by_id", allow_multiline=True, max_chars=200) == ""
    )
    assert (
        utils.clean_reply(
            "[historical tool call] search_emoji", allow_multiline=True, max_chars=200
        )
        == ""
    )
    assert (
        utils.clean_reply("[tool call]send_emoji_by_id", allow_multiline=True, max_chars=200) == ""
    )
    assert (
        utils.clean_reply("[historical tool call]search_emoji", allow_multiline=True, max_chars=200)
        == ""
    )
    assert utils.clean_reply("自然回复", allow_multiline=True, max_chars=200) == "自然回复"


def test_final_send_path_rechecks_generation_after_decorating_hook() -> None:
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    method_start = source.index("    async def _send_reply(")
    method_end = source.find("\n    def ", method_start)
    method = source[method_start : method_end if method_end != -1 else None]
    hook_marker = "await call_event_hook(last_event, EventType.OnDecoratingResultEvent)"
    send_marker = "send_result = await outbound.send(result)"

    assert "expected_generation: int | None = None" in method.splitlines()[1]
    hook_offset = method.index(hook_marker)
    send_offset = method.index(send_marker, hook_offset)
    assert "_gate.is_current(umo, expected_generation)" in method[hook_offset:send_offset]
    assert (
        "expected_generation=expected_generation"
        in source[source.index("sent = await self._send_reply") :]
    )


def test_bare_group_whitelist_keeps_platform_state_isolated(tmp_path: Path) -> None:
    _, utils, storage = _load_modules()
    whitelist = {"12345"}
    qq = "qq:GroupMessage:12345"
    telegram = "telegram:GroupMessage:12345"

    assert utils.session_whitelisted(qq, whitelist)
    assert utils.session_whitelisted(telegram, whitelist)
    assert utils.whitelist_storage_key(qq, whitelist) == qq
    assert utils.whitelist_storage_key(telegram, whitelist) == telegram

    state = storage.SessionState(recent=deque(maxlen=5))
    state.daily_count = 2
    sessions = {qq: state, telegram: storage.SessionState(recent=deque(maxlen=5))}
    path = tmp_path / "state.json"
    assert storage.save_sessions(path, sessions, whitelist, 5)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["sessions"]) == {qq, telegram}


def test_malformed_session_record_does_not_abort_load(tmp_path: Path) -> None:
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "sessions": {
                    "qq:GroupMessage:123": {
                        "last_active_at": "bad",
                        "daily_count": -99,
                        "recent": "not-a-list",
                    },
                    "qq:GroupMessage:456": {"recent": [{"role": "unknown", "text": "kept"}]},
                    "qq:GroupMessage:789": "not-an-object",
                }
            }
        ),
        encoding="utf-8",
    )

    sessions = storage.load_sessions(path, {"123", "456", "789"}, 5)
    assert sessions["qq:GroupMessage:123"].last_active_at == 0.0
    assert sessions["qq:GroupMessage:123"].daily_count == 0
    assert sessions["qq:GroupMessage:456"].recent[0].role == "user"
    assert "qq:GroupMessage:789" not in sessions


def test_corrupt_state_file_is_backed_up_and_load_continues(tmp_path: Path) -> None:
    """JSON 损坏时备份原文件（corrupt-<ts>）并继续以空状态加载，不得抛错。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")

    sessions = storage.load_sessions(path, {"123"}, 5)

    backups = sorted(tmp_path.glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{not valid json"
    assert "123" in sessions  # 白名单会话仍以空状态创建


def test_version_mismatch_state_file_is_backed_up_and_best_effort_loaded(tmp_path: Path) -> None:
    """version 不符时备份原文件，仍尽力解析仍兼容的数据。"""
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 999,
                "sessions": {"qq:GroupMessage:123": {"last_active_at": 1.5, "daily_count": 3}},
            }
        ),
        encoding="utf-8",
    )

    sessions = storage.load_sessions(path, {"123"}, 5)

    backups = sorted(tmp_path.glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert sessions["qq:GroupMessage:123"].last_active_at == 1.5
    assert sessions["qq:GroupMessage:123"].daily_count == 3


def test_atomic_state_writer_leaves_previous_file_on_serialization_failure(tmp_path: Path) -> None:
    _, _, storage = _load_modules()
    path = tmp_path / "state.json"
    path.write_text('{"previous": true}', encoding="utf-8")
    assert not storage.write_sessions_payload(path, {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"previous": True}


def test_session_state_record_proactive_attempt_confirmed_and_unconfirmed() -> None:
    """record_proactive_attempt 单点写入语义：confirmed 写历史，unconfirmed 只消耗配额。"""
    models, _, _ = _load_modules()

    state = models.SessionState()
    state.record_proactive_attempt(confirmed=True, text="你好呀", at=10.0)
    assert state.last_proactive_at == 10.0
    assert state.daily_count == 1
    assert state.last_proactive_text == "你好呀"
    assert len(state.recent) == 1
    assert state.recent[-1].role == "assistant"
    assert state.recent[-1].text == "你好呀"

    state.record_proactive_attempt(confirmed=False, text="", at=20.0)
    assert state.last_proactive_at == 20.0
    assert state.daily_count == 2
    # UNKNOWN 投递不写历史条目
    assert len(state.recent) == 1
    assert state.last_proactive_text == "你好呀"
