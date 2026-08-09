"""事件忽略判定单测（ticket 06 验收）：should_ignore_event 纯函数行为不变。

覆盖验收项：消息忽略判定（自消息/命令/纯图无识图/忽略名单/直接点名）。
"""

from __future__ import annotations

import importlib

from .test_vision import PACKAGE_NAME


def _events_module():
    from .test_vision import _load_modules

    _load_modules()  # 先创建测试包再导入 utils（与 whitelist 测试一致）
    return importlib.import_module(f"{PACKAGE_NAME}.utils")


class _FakeEvent:
    def __init__(self, *, sender_id: str = "u1", self_id: str = "", at_wake: bool = False):
        self._sender_id = sender_id
        self._self_id = self_id
        self._at_wake = at_wake

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_self_id(self) -> str:
        return self._self_id

    is_at_or_wake_command = False


def _direct_call_event() -> _FakeEvent:
    event = _FakeEvent()
    event.is_at_or_wake_command = True
    return event


def _should_ignore(events, event, text, *, vision: bool, ignored: set[str] | None = None):
    return events.should_ignore_event(
        event,
        text,
        vision_has_images=vision,
        ignored_sender_ids=ignored if ignored is not None else set(),
    )


async def test_ignore_self_message() -> None:
    events = _events_module()
    event = _FakeEvent(sender_id="bot", self_id="bot")
    assert _should_ignore(events, event, "普通消息", vision=False) is True


async def test_ignore_command_text() -> None:
    events = _events_module()
    event = _FakeEvent()
    assert _should_ignore(events, event, "/selfreply", vision=False) is True


async def test_ignore_bare_image_without_vision() -> None:
    events = _events_module()
    event = _FakeEvent()
    assert _should_ignore(events, event, "", vision=False) is True


async def test_keep_bare_image_with_vision() -> None:
    events = _events_module()
    event = _FakeEvent()
    assert _should_ignore(events, event, "", vision=True) is False


async def test_ignore_sender_in_ignore_list() -> None:
    events = _events_module()
    event = _FakeEvent(sender_id="banned")
    assert _should_ignore(events, event, "普通消息", vision=False, ignored={"banned"}) is True


async def test_ignore_explicit_direct_call() -> None:
    events = _events_module()
    event = _direct_call_event()
    assert _should_ignore(events, event, "普通消息", vision=False) is True


async def test_keep_normal_message() -> None:
    events = _events_module()
    event = _FakeEvent()
    assert _should_ignore(events, event, "普通消息", vision=False) is False
