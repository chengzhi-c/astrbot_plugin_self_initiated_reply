"""消息入口的开关、事件资格和资源边界。"""

from __future__ import annotations

from ._support import make_event
from .host_stubs import with_plugin

UMO = "fake:group:123"


async def _feed(plugin, event) -> None:
    from importlib import import_module

    package = plugin.__class__.__module__.rsplit(".", 1)[0]
    module = import_module(f"{package}.message_ingress")
    await module.handle_incoming_message(plugin, event)


def test_message_trigger_switch_off_schedules_no_check(tmp_path) -> None:
    """关掉「消息后触发」后不得再排延迟检查。

    缺陷形态：``_schedule_message_check`` 开头的早退是该开关的唯一执行点
    （``session_check_guard`` 不查它）。删掉早退后开关变成空转——用户以为
    关了消息触发，机器人仍对每条白名单消息主动接话。
    """

    async def scenario(plugin, main):
        assert plugin.settings.enabled_message_trigger is False
        await _feed(plugin, make_event(message_str="今天天气不错"))
        # 消息本身仍要记录（观察窗口与历史），只是不排检查
        assert UMO in plugin._last_events, "消息没被记录"
        assert UMO not in plugin._delay_tasks, "消息触发开关已关闭，却仍然排了延迟检查"

    with_plugin(tmp_path, scenario, enabled_message_trigger=False)


def test_message_path_runs_event_cleanup(tmp_path) -> None:
    """消息路径必须跑事件回收，否则内存只涨不落。

    缺陷形态：``handle_incoming_message`` 末尾的 ``cleanup_events_if_needed()``
    是默认配置下唯一的回收点（另一处只在巡检循环里，由默认关闭的
    ``enabled_patrol_trigger`` 把守）。删掉它，每个进过插件的会话各留一份事件
    与裁决条目，``GET /status`` 的回显随会话数单调变大。
    """

    async def scenario(plugin, main):
        calls = {"n": 0}
        original = plugin._scheduler.cleanup_events_if_needed

        def counted() -> None:
            calls["n"] += 1
            original()

        plugin._scheduler.cleanup_events_if_needed = counted
        try:
            await _feed(plugin, make_event(message_str="你好"))
        finally:
            plugin._scheduler.cleanup_events_if_needed = original
        assert calls["n"] >= 1, "消息路径没有触发事件回收，内存只涨不落"

    with_plugin(tmp_path, scenario)


def test_ignored_message_invalidates_stale_reply(tmp_path) -> None:
    """被忽略的消息（忽略名单发送者）必须作废未发出的旧回复。

    缺陷形态：``_accepted_content`` 里对 ignored/empty 的代次推进是它们
    唯一的代次推进路径——这两类消息都在 ``_record_message`` 之前返回。删掉它，
    群里被拉黑的人发言或有人刷贴纸时，上一轮已经排好的主动回复不作废，机器人
    拿着过期上下文接话，甚至回复到已经翻篇的话题上（契约 §6.3）。
    """

    async def scenario(plugin, main):
        assert plugin.settings.abandon_stale_on_new_message is True
        await _feed(plugin, make_event(message_str="先说一句"))
        before = plugin._gate.current(UMO)

        ignored = make_event(message_str="被我忽略的人说话", sender_id="blocked")
        await _feed(plugin, ignored)
        after = plugin._gate.current(UMO)

        assert after > before, (
            f"被忽略的消息没有推进代次（{before} -> {after}）：旧的主动回复不会作废"
        )
        # 只推进代次，不走 ``invalidate`` 的级联清理：最近事件与保护窗口内的
        # 图片索引必须留着（契约 §8），否则一次 @Bot 或一条表情包就让巡检
        # 因「没有可用的最近消息事件」静默跳过该会话。
        assert plugin._last_events.get(UMO) is not None, "被忽略的消息抹掉了最近事件"
        # 被忽略的消息不进观察窗口
        assert plugin._state_for(UMO).recent[-1].text != "被我忽略的人说话"

    with_plugin(
        tmp_path, scenario, ignored_sender_ids={"blocked"}, abandon_stale_on_new_message=True
    )


def test_empty_message_is_rejected(tmp_path) -> None:
    """清完后既无文本也无图的消息必须被拒，不得记成图片消息。

    缺陷形态：纯提及消息剥离提及后为空且无图片时
    ``empty=True``。删掉那道早退，函数落到 ``return clean_text or "[图片]"``，
    一条没有内容的消息被当成图片消息记进历史并触发延迟检查——机器人可能对空气
    主动回复，历史里还会出现假的 ``[图片]``。
    """

    async def scenario(plugin, main):
        # 纯提及消息：原文非空（不会被 should_ignore_event 的"空文本"一支捞走），
        # 但 strip_leading_mentions 剥离 @xxx 后为空——只有 empty 那一支
        # 能拦住它。
        await _feed(plugin, make_event(message_str="@someone"))
        assert UMO not in plugin._last_events, "空内容消息被记进了事件缓存"
        assert UMO not in plugin._delay_tasks, "空内容消息排了延迟检查"
        assert not plugin._state_for(UMO).recent, "空内容消息进了会话历史"

    with_plugin(tmp_path, scenario)


def test_stopped_event_is_dropped(tmp_path) -> None:
    """已被别的插件 ``stop()`` 的事件不得再进本插件管线。

    缺陷形态：``_eligible_session`` 的 ``event.is_stopped()`` 子句。AstrBot
    生态里指令框架、过滤器会 ``event.stop()``，本插件装在同一个事件流上；
    删掉这道闸，别人已经接走的消息仍被记状态、排延迟检查，可能和另一个插件
    重复回应。
    """

    async def scenario(plugin, main):
        event = make_event(message_str="别人已经接走的消息")
        event.stop_event()
        assert event.is_stopped() is True
        await _feed(plugin, event)
        assert UMO not in plugin._last_events, "已 stop 的事件被记进了事件缓存"
        assert UMO not in plugin._delay_tasks, "已 stop 的事件排了延迟检查"
        assert not plugin._whitelist_runtime_umos, "已 stop 的事件仍登记了运行时 UMO"

    with_plugin(tmp_path, scenario)


def test_non_whitelisted_session_is_dropped(tmp_path) -> None:
    """非白名单会话必须在入口就被丢，不得占状态与磁盘。

    缺陷形态：``_eligible_session`` 的白名单闸。删掉后机器人所在但未加白名单的
    群/私聊，每条消息都写进 ``_whitelist_runtime_umos``、事件缓存与落盘状态。
    发送最终仍被 ``session_check_guard`` 挡住（所以不外发），但内存与磁盘被白白
    占用，``GET /status`` 的会话计数也会虚高。
    """

    outsider = "fake:group:999"

    async def scenario(plugin, main):
        await _feed(plugin, make_event(umo=outsider, message_str="未加白的群"))
        assert outsider not in plugin._last_events, "非白名单会话被记进了事件缓存"
        assert outsider not in plugin._delay_tasks, "非白名单会话排了延迟检查"
        assert outsider not in plugin._whitelist_runtime_umos, "非白名单会话登记进了运行时 UMO 表"
        assert not plugin._state_for(outsider).recent, "非白名单会话进了会话历史"

    with_plugin(tmp_path, scenario)


def test_direct_call_message_does_not_schedule_check(tmp_path) -> None:
    """直接点名（@Bot）的消息只更新观察窗口，不排主动回复检查。

    ``_accepted_content`` 对 ignored 且为显式直接调用的消息会刷新
    ``last_active_at``（以及可选的 ``last_proactive_observed_at``），随后
    ``return None``——点名由 AstrBot 自己回复，本插件不该再排一次延迟检查，
    否则同一句话被回两遍。
    """

    async def scenario(plugin, main):
        event = make_event(message_str="@Bot 在吗")
        event.is_at_or_wake_command = True
        await _feed(plugin, event)
        assert UMO not in plugin._delay_tasks, "点名消息排了延迟检查，会被回两遍"
        assert plugin._state_for(UMO).last_active_at > 0, "点名消息没刷新观察窗口"

    with_plugin(tmp_path, scenario)


def test_accepted_message_records_and_schedules(tmp_path) -> None:
    """对照组：正常消息必须既记录又排检查。

    没有这条，上面六条删掉守卫后也可能因为整条路径压根不通而假绿。
    """

    async def scenario(plugin, main):
        await _feed(plugin, make_event(message_str="正常消息"))
        assert UMO in plugin._last_events, "正常消息没被记录"
        assert UMO in plugin._delay_tasks, "正常消息没排延迟检查"
        assert plugin._state_for(UMO).recent, "正常消息没进会话历史"

    with_plugin(tmp_path, scenario)


def test_mentions_of_other_people_reach_judge_context(tmp_path) -> None:
    async def scenario(plugin, main):
        text = "@小明(456) 你周末来吗？"
        await _feed(plugin, make_event(message_str=text))
        prompt = await plugin._decision.build_decision_prompt(
            UMO, plugin._state_for(UMO), "message_delay"
        )
        assert text in prompt

    with_plugin(tmp_path, scenario)
