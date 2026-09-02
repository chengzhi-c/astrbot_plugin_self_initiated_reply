"""事件忽略判定单测：should_ignore_event 纯函数行为不变。

覆盖：消息忽略判定（自消息/命令/纯图无识图/忽略名单/直接点名）。
"""

from __future__ import annotations

import importlib
import sys

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


def test_handle_incoming_message_blindspots(tmp_path) -> None:
    """覆盖 message_ingress: 指令消息直接返回、忽略消息时更新活跃时间/作废旧任务。"""
    from .host_stubs import with_plugin

    async def scenario(plugin, main):
        from .test_main_runtime import _make_event

        ingress = sys.modules[f"{main.__package__}.message_ingress"]

        # 1. 已处理的指令事件直接返回
        handled_event = _make_event(message_str="anything")
        setattr(handled_event, main.COMMAND_HANDLED_KEY, True)
        await ingress.handle_incoming_message(plugin, handled_event)

        # 2. 内联指令解析并处理
        cmd_event = _make_event(message_str="/selfreply status")
        await ingress.handle_incoming_message(plugin, cmd_event)

        # 3. 开启 abandon_stale_on_new_message 时收到 @Bot 直接点名
        plugin.settings.abandon_stale_on_new_message = True
        direct_event = _make_event(message_str="@Bot 出来聊聊")
        direct_event.is_at_or_wake_command = True
        await ingress.handle_incoming_message(plugin, direct_event)
        state = plugin._state_for(main.whitelist_storage_key(main.event_umo(direct_event)))
        assert state.last_active_at > 0

        # 4. 开启 abandon_stale_on_new_message 且纯空格消息
        empty_event = _make_event(message_str="   ")
        await ingress.handle_incoming_message(plugin, empty_event)

    with_plugin(tmp_path, scenario)
