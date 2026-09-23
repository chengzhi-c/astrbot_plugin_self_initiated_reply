"""DeliveryRunner 的 @ 对方（mention_mode）行为矩阵。

与 quote（引用）正交：@ 的是「本次回复依据的那条消息的发送者」，插在链首 At 位置。
覆盖：三模式语义、random 模式的概率边界与随机源注入、无 sender_id / 自己发自己 /
事件已回收三级降级、与 quote 同开时的插入顺序、context 兜底路径不 @、
代次已变时不插、以及 off/always 模式不消耗随机数。

@ 组件能否被断言取决于 ``host_stubs`` 的 ``At`` 桩（空壳 type 会让 ``At(qq=...)``
抛 TypeError，全部断言退化成假绿）——``test_mention_host_at_stub_accepts_qq`` 就是
这条前提自身的守卫。
"""

from __future__ import annotations

from pathlib import Path

from .host_stubs import FakeEvent
from .test_delivery_runner import _make_runner


class _ChainRecordingEvent(FakeEvent):
    """把发送时拿到的消息链记下来（``send`` 由 OutboundGateway 调用）。"""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.sent_chains: list[list[object]] = []

    async def send(self, message: object) -> None:
        self.sent_chains.append(list(getattr(message, "chain", []) or []))
        return None


def _at_items(chain: list[object]) -> list[object]:
    return [item for item in chain if type(item).__name__ == "_FakeAt"]


async def test_mention_off_never_mentions(tmp_path: Path) -> None:
    """默认 off：不 @，且不消耗随机数（sender 在也不插）。"""
    _mod, models, runner, last_events = _make_runner(
        tmp_path, config={"mention_mode": "off"}, random_value=lambda: 0.0
    )
    event = _ChainRecordingEvent(umo="s1", sender_id="u1")
    last_events["s1"] = event
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.DELIVERED
    assert event.sent_chains, "发送未被记录"
    assert not _at_items(event.sent_chains[0]), "off 模式不得插入 @"


async def test_mention_always_inserts_at_for_message_sender(tmp_path: Path) -> None:
    """always：@ 本次依据消息的发送者，插在链首。"""
    _mod, models, runner, last_events = _make_runner(
        tmp_path, config={"mention_mode": "always"}, random_value=lambda: 0.99
    )
    event = _ChainRecordingEvent(umo="s1", sender_id="u42")
    last_events["s1"] = event
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.DELIVERED
    chain = event.sent_chains[0]
    assert type(chain[0]).__name__ == "_FakeAt", f"链首应是 At，实际 {type(chain[0]).__name__}"
    assert str(chain[0].qq) == "u42"


async def test_mention_random_follows_probability_boundaries(tmp_path: Path) -> None:
    """random：0 从不、100 每次；概率边界由同一个随机源判定。

    ``draw`` 经默认参数绑定而非闭包捕获：循环变量在闭包里会被 B023 判为延迟绑定，
    三个迭代全部读到最后一个值，边界断言会失真。
    """
    for probability, draw, expected in ((0, 0.0, False), (100, 0.999, True)):
        _mod, _models, runner, last_events = _make_runner(
            tmp_path,
            config={"mention_mode": "random", "mention_probability": probability},
            random_value=lambda captured=draw: captured,
        )
        event = _ChainRecordingEvent(umo="s1", sender_id="u1")
        last_events["s1"] = event
        await runner.send_reply("s1", "hello", expected_generation=None)
        assert bool(_at_items(event.sent_chains[0])) is expected, (
            f"probability={probability} draw={draw} 判定不符"
        )


async def test_mention_off_and_always_do_not_consume_random_source(tmp_path: Path) -> None:
    """off/always 不得调用随机源：否则同批测试的随机数轨迹不可复现。

    变异锚定：把 ``_should_mention`` 改成无条件调 ``self._random_value()``
    （先算概率再判模式），本用例红。
    """
    calls: list[int] = []

    def counting_random() -> float:
        calls.append(1)
        return 0.5

    for mode in ("off", "always"):
        calls.clear()
        _mod, _models, runner, last_events = _make_runner(
            tmp_path, config={"mention_mode": mode}, random_value=counting_random
        )
        last_events["s1"] = _ChainRecordingEvent(umo="s1", sender_id="u1")
        await runner.send_reply("s1", "hello", expected_generation=None)
        assert not calls, f"{mode} 模式不应消耗随机源（调用 {len(calls)} 次）"


async def test_mention_degrades_when_sender_id_missing(tmp_path: Path) -> None:
    """取不到 sender_id：静默降级为普通发送，不抛、不影响投递结果。"""
    _mod, models, runner, last_events = _make_runner(tmp_path, config={"mention_mode": "always"})
    event = _ChainRecordingEvent(umo="s1", sender_id="")
    last_events["s1"] = event
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.DELIVERED
    assert not _at_items(event.sent_chains[0])


async def test_mention_never_targets_the_bot_itself(tmp_path: Path) -> None:
    """自己发自己的消息不 @：Bot 会被自己 @ 上，属明显误用。"""
    _mod, models, runner, last_events = _make_runner(tmp_path, config={"mention_mode": "always"})
    event = _ChainRecordingEvent(umo="s1", sender_id="bot1", self_id="bot1")
    last_events["s1"] = event
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.DELIVERED
    assert not _at_items(event.sent_chains[0])


async def test_mention_degrades_when_event_already_recycled(tmp_path: Path) -> None:
    """事件已回收（_last_events 为空）：走 context 兜底，不 @（无 sender 可取）。"""
    from .test_delivery_runner import FakeContextSend

    context_send = FakeContextSend()
    _mod, models, runner, _last_events = _make_runner(
        tmp_path, config={"mention_mode": "always"}, context_send=context_send
    )
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.DELIVERED
    sent = context_send.calls[0][1]
    assert not _at_items(list(getattr(sent, "chain", []) or [])), "context 路径不得 @"


async def test_mention_and_quote_together_put_at_before_reply(tmp_path: Path) -> None:
    """两个开关同开：链首顺序必须是 [At, Reply, ...正文]（先点名后引用）。"""
    _mod, _models, runner, last_events = _make_runner(
        tmp_path,
        config={
            "mention_mode": "always",
            "quote_mode": "random",
            "quote_probability": 100,
        },
        random_value=lambda: 0.0,
    )
    event = _ChainRecordingEvent(umo="s1", sender_id="u7")
    event.message_id = "msg-9"  # 事件自身字段优先，供 event_message_id 取到
    last_events["s1"] = event
    await runner.send_reply("s1", "hello", expected_generation=None, quote=True)
    chain = event.sent_chains[0]
    kinds = [type(item).__name__ for item in chain]
    assert kinds[:2] == ["_FakeAt", "_FakeReply"], f"插入顺序错误：{kinds}"
    assert str(chain[0].qq) == "u7"
    assert chain[1].id == "msg-9"


async def test_mention_skipped_when_generation_changed_before_send(tmp_path: Path) -> None:
    """代次在复核点 2 翻转：整条发送被抑制，@ 与正文都不出去。"""
    from .test_delivery_blindspots import _FlipGate

    _mod, models, runner, last_events = _make_runner(tmp_path, config={"mention_mode": "always"})
    runner._gate = _FlipGate(true_times=1)
    event = _ChainRecordingEvent(umo="s1", sender_id="u1")
    last_events["s1"] = event
    outcome = await runner.send_reply("s1", "hello", expected_generation=7)
    assert outcome.status is models.SendStatus.SUPPRESSED
    assert not event.sent_chains, "抑制后不得调用 sender"


async def test_mention_component_failure_degrades_to_plain_send(tmp_path: Path) -> None:
    """@ 组件构造失败（宿主/平台不支持）静默降级：投递结果与记账不受影响。"""
    from . import host_stubs

    host_stubs.install_astrbot_stubs()
    import astrbot.api.message_components as components

    class _BoomAt:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("platform has no at component")

    _mod, models, runner, last_events = _make_runner(tmp_path, config={"mention_mode": "always"})
    event = _ChainRecordingEvent(umo="s1", sender_id="u1")
    last_events["s1"] = event
    original = components.At
    components.At = _BoomAt
    try:
        outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    finally:
        components.At = original
    assert outcome.status is models.SendStatus.DELIVERED
    assert event.sent_chains, "@ 失败不应阻止发送"


def test_mention_host_at_stub_accepts_qq(tmp_path: Path) -> None:
    """守卫：``At`` 桩必须接受 ``qq=`` 关键字，否则上面全部用例都是假绿。

    空壳 ``type("At", (), {})`` 会让 ``At(qq=...)`` 抛 TypeError，被
    ``_attach_mention`` 的 except 吞掉——所有"已插入 @"断言会静默通过。
    """
    from . import host_stubs

    host_stubs.install_astrbot_stubs()
    import astrbot.api.message_components as components

    at = components.At(qq="u1")
    assert str(at.qq) == "u1"
    assert str(at) == "", "At 不得进入 get_plain_text 正文"
