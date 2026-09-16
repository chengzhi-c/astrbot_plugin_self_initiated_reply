"""事件忽略判定单测：should_ignore_event 纯函数行为不变。

覆盖：消息忽略判定（自消息/命令/纯图无识图/忽略名单/直接点名）。
"""

from __future__ import annotations

import importlib
import logging
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


def test_cq_at_requires_digit_boundary() -> None:
    """CQ @ 判定要求数字边界：他人 QQ 号以 self_id 结尾时不得误判为点名。

    `[CQ:at,qq=456123]` 在 self_id=123 下若被误判，该消息会被
    should_ignore_event 当作直接点名静默丢弃、不进观察窗口。
    """
    events = _events_module()
    event = _FakeEvent(self_id="123")
    assert events.is_explicit_direct_call(event, "[CQ:at,qq=123]") is True
    assert events.is_explicit_direct_call(event, "[CQ:at,qq=456123]") is False
    assert events.is_explicit_direct_call(event, "[At:123]") is True
    assert events.is_explicit_direct_call(event, "[At:456123]") is False


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
        utils = sys.modules[f"{main.__package__}.utils"]
        state = plugin._state_for(utils.whitelist_storage_key(utils.event_umo(direct_event)))
        assert state.last_active_at > 0

        # 4. 开启 abandon_stale_on_new_message 且纯空格消息
        empty_event = _make_event(message_str="   ")
        await ingress.handle_incoming_message(plugin, empty_event)

    with_plugin(tmp_path, scenario)


class _SourcedImage:
    """可抽出可用来源的图片组件（``url`` 非空）。"""

    type = "image"
    subType = 0
    url = "https://cdn.example.test/photo.png"


class _SourcelessImage:
    """只有组件、无任何来源的图片：``has_images`` 为真而 ``extract_images`` 为空。

    ``_accepted_content`` 的 "[图片]" 回落正依赖这两个判据不等价。
    """

    type = "image"
    subType = 0


def _spy_scheduler(plugin) -> list[str]:
    """记录 ``schedule_delayed_check`` 的 umo 调用，仍执行原方法。"""
    scheduled: list[str] = []
    original = plugin._scheduler.schedule_delayed_check

    def _spy(umo, **kwargs):
        scheduled.append(umo)
        return original(umo, **kwargs)

    plugin._scheduler.schedule_delayed_check = _spy
    return scheduled


def test_image_capture_failure_does_not_break_the_scheduling_chain(tmp_path, caplog) -> None:
    """图片抓取抛异常不得阻断消息调度链。

    ``handle_incoming_message`` 把 ``_capture_images`` 包在 try/except 里，失败
    只记 WARNING，之后仍要走事件回收与延迟检查。该不变量此前只由代码结构成立、
    无用例锚定——重构掉 try/except 或把调度挪进 try 之前都不会有测试变红。
    """
    from .host_stubs import capture_logs, messages_at_least, with_plugin

    async def scenario(plugin, main):
        from .test_main_runtime import _make_event

        ingress = sys.modules[f"{main.__package__}.message_ingress"]
        assert plugin.settings.vision_enabled is True

        scheduled = _spy_scheduler(plugin)

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("vision provider exploded")

        plugin._vision.capture = _boom

        event = _make_event(message_str="看看这张图")
        event.is_at_or_wake_command = False
        event.get_messages = lambda: [_SourcedImage()]

        with capture_logs(caplog, ingress.logger, logging.WARNING):
            await ingress.handle_incoming_message(plugin, event)

        assert scheduled, "图片抓取失败后仍必须安排延迟检查"
        warnings = messages_at_least(caplog, logging.WARNING)
        assert any("image capture failed" in message for message in warnings), warnings

    with_plugin(tmp_path, scenario, vision_main_enabled=True)


def test_image_without_source_degrades_to_placeholder_and_still_schedules(tmp_path, caplog) -> None:
    """有图片组件但抽不出来源时降级为 "[图片]"，且不重复抓取、不阻断调度。

    与上一用例互为补：上一条走 ``_capture_images`` 抛异常，这条走
    ``extract_images`` 返回空（``has_images`` 与 ``extract_images`` 判据不等价）。
    """
    from .host_stubs import capture_logs, messages_at_least, with_plugin

    async def scenario(plugin, main):
        from .test_main_runtime import _make_event

        ingress = sys.modules[f"{main.__package__}.message_ingress"]
        utils = sys.modules[f"{main.__package__}.utils"]
        scheduled = _spy_scheduler(plugin)

        captured: list[object] = []

        async def _capture(*args, **kwargs):
            captured.append((args, kwargs))

        plugin._vision.capture = _capture

        event = _make_event(message_str="   ")
        event.is_at_or_wake_command = False
        event.get_messages = lambda: [_SourcelessImage()]

        with capture_logs(caplog, ingress.logger, logging.DEBUG):
            await ingress.handle_incoming_message(plugin, event)

        assert not captured, "无来源图片不得进入 Vision 抓取"
        assert scheduled, "降级为 [图片] 后仍必须安排延迟检查"
        state = plugin._state_for(utils.whitelist_storage_key(utils.event_umo(event)))
        assert state.recent[-1].text == "[图片]"
        debug_logs = messages_at_least(caplog, logging.DEBUG)
        assert any("extract_images returned empty" in message for message in debug_logs), debug_logs

    with_plugin(tmp_path, scenario, vision_main_enabled=True)


def test_direct_call_defers_same_batch_proactive_reply(tmp_path) -> None:
    """被 @Bot/唤醒后，同一批消息不再触发主动回复（``skip_after_direct_call``）。

    背景（实测口径）：@Bot 的消息由 AstrBot 正常回复、不经过本插件；若只更新
    活跃时间而不记「已回应」，静默时间一到就会再主动接一句——表现为「刚被点名
    答过又自己插话」，且判断模型看不到那轮 @Bot 对话，无从自制。
    """
    from .host_stubs import with_plugin

    async def scenario(plugin, main):
        from .test_main_runtime import _make_event

        utils = sys.modules[f"{main.__package__}.utils"]
        ingress = sys.modules[f"{main.__package__}.message_ingress"]

        event = _make_event(message_str="@Bot 出来聊聊")
        event.is_at_or_wake_command = True
        await ingress.handle_incoming_message(plugin, event)

        umo = utils.event_umo(event)
        state = plugin._state_for(utils.whitelist_storage_key(umo))
        assert state.last_proactive_observed_at == state.last_active_at, (
            "直接点名消息必须把观察窗口推进到本条消息"
        )
        assert plugin._decision.local_gate(state, force=False) == ("这条消息之后已经主动回复过。")

        # 关闭开关即回到旧行为：只更新活跃时间，不推进观察窗口
        plugin.settings.skip_after_direct_call = False
        before = state.last_proactive_observed_at
        other = _make_event(message_str="@Bot 在吗")
        other.is_at_or_wake_command = True
        await ingress.handle_incoming_message(plugin, other)
        assert state.last_proactive_observed_at == before, "开关关闭后不得再推进观察窗口"

    with_plugin(tmp_path, scenario)
