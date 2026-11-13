"""DeliveryRunner 的 @ 对方（mention_mode）行为矩阵。

与 quote（引用）正交：@ 的是「本次回复依据的那条消息的发送者」，插在链首 At 位置。
覆盖：三模式语义、random 模式的概率边界与随机源注入、无 sender_id / 自己发自己 /
完全无事件可用的降级、与 quote 同开时的插入顺序、context 兜底路径不 @、
代次已变时不插、以及 off/always 模式不消耗随机数。

@ 组件能否被断言取决于 ``host_stubs`` 的 ``At`` 桩（空壳 type 会让 ``At(qq=...)``
抛 TypeError，全部断言退化成假绿）``test_mention_host_at_stub_accepts_qq`` 就是
这条前提自身的守卫。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from .host_stubs import FakeContextSend, FakeEvent
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
    return [item for item in chain if _is_at(item)]


def _is_at(item: object) -> bool:
    """按宿主 ``At`` 判定（延迟导入：桩在 install 之后才存在）。"""
    from astrbot.api.message_components import At

    return isinstance(item, At)


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


async def test_mentions_follow_the_pinned_target_event(tmp_path: Path) -> None:
    """@ 必须锚在本次依据的那条消息的发送者上，而不是投递时刻的最新消息。

    缺陷形态：``send_reply`` 现读 ``_last_events``。判断与生成期间新消息会把
    该表换人——群里 A 提问、B 插话后，回复 @ 到了 B（「引用错对象」）。

    变异锚定：``send_reply`` 改回现读 ``_last_events``，本用例红。
    """
    _mod, models, runner, last_events = _make_runner(
        tmp_path, config={"mention_mode": "always"}, random_value=lambda: 0.99
    )
    original = _ChainRecordingEvent(umo="s1", sender_id="u-orig")
    newcomer = _ChainRecordingEvent(umo="s1", sender_id="u-new")
    last_events["s1"] = newcomer  # 判断/生成期间到达的新消息抢走了 _last_events

    outcome = await runner.send_reply(
        "s1", "hello", expected_generation=None, target_event=original
    )

    assert outcome.status is models.SendStatus.DELIVERED
    # 通道与 @ 都用冻结目标：“最新消息”事件是宿主仍在处理的槽。
    assert not newcomer.sent_chains, "发送走了生成期间新到达的消息事件"
    at_items = _at_items(original.sent_chains[0])
    assert len(at_items) == 1, original.sent_chains[0]
    assert getattr(at_items[0], "qq", None) == "u-orig", "@ 指向了插话者"


async def test_mention_always_inserts_at_for_message_sender(tmp_path: Path) -> None:
    """always：@ 本次依据消息的发送者，插在链首，并带上其昵称。

    昵称必须与 sender_id 不同且断言成前者：桩的 ``get_sender_name()`` 默认回显
    sender_id，两者写同一个值的时候 ``name=mention_id`` 这种错法也能绿。
    """
    _mod, models, runner, last_events = _make_runner(
        tmp_path, config={"mention_mode": "always"}, random_value=lambda: 0.99
    )
    event = _ChainRecordingEvent(umo="s1", sender_id="u42")
    event.get_sender_name = lambda: "小明"
    last_events["s1"] = event
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.DELIVERED
    chain = event.sent_chains[0]
    assert _is_at(chain[0]), f"链首应是 At，实际 {type(chain[0]).__name__}"
    assert str(chain[0].qq) == "u42"
    assert chain[0].name == "小明", "@ 的显示名必须是昵称，不是 QQ 号"
    assert chain[0].toDict() == {"type": "at", "data": {"qq": "u42", "name": "小明"}}, (
        "昵称必须进 OneBot 段：宿主 At.toDict() 只发 qq，读 name 的协议端会退化成空 @"
    )


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


async def test_mention_degrades_when_no_event_is_available(tmp_path: Path) -> None:
    """完全没有事件可用（无冻结目标、表项也为空）：走 context 兜底，不 @。"""
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
    assert _is_at(chain[0]), f"插入顺序错误：{kinds}"
    assert str(chain[0].qq) == "u7"
    assert chain[1].id == "msg-9"


async def test_mention_component_failure_degrades_to_plain_send(tmp_path: Path) -> None:
    """@ 组件构造失败（宿主/平台不支持）静默降级：投递结果与记账不受影响。

    要换的是 **delivery 模块自己构造的那个类** ``_NamedAt``：改
    ``components.At`` 或 ``delivery.At`` 都动不了它（类已在导入时绑定），
    那两种改法会让真实组件照常构造成功，于是本用例因链里出现 @ 而红。

    ``calls`` 断言构造器真的被调用过：只查“链里没有 At”的话，mention 路径
    根本不跑（如 ``_resolve_mention_id`` 退化成空串）也会绿。
    """
    _mod, models, runner, last_events = _make_runner(tmp_path, config={"mention_mode": "always"})
    calls: list[int] = []

    class _BoomAt:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append(1)
            raise RuntimeError("platform has no at component")

    event = _ChainRecordingEvent(umo="s1", sender_id="u1")
    last_events["s1"] = event
    original = _mod._NamedAt
    _mod._NamedAt = _BoomAt
    try:
        outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    finally:
        _mod._NamedAt = original
    assert outcome.status is models.SendStatus.DELIVERED
    assert event.sent_chains, "@ 失败不应阻止发送"
    assert len(calls) == 1, "降级分支未被触发（mention 路径根本没跑到构造点）"
    assert not _at_items(event.sent_chains[0]), "构造失败的 @ 不得进链"


async def test_quote_component_failure_degrades_to_plain_send(tmp_path: Path) -> None:
    """引用组件构造失败同样静默降级（与 @ 完全同一套纪律）。

    配置必须是 ``quote_mode="random"`` + 概率拉满：``quote_mode`` 的取值只有
    off/model/random，写 ``"always"`` 会被 ``choice()`` 静默映射成 ``off``，
    于是 ``quote_id`` 为空、``if quote_id:`` 分支根本不进，替换 ``_mod.Reply``
    也永远不会被调用——那样这条用例恒绿，什么都没验。
    """
    _mod, models, runner, last_events = _make_runner(
        tmp_path, config={"quote_mode": "random", "quote_probability": 100}
    )
    calls: list[int] = []

    class _BoomReply:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append(1)
            raise RuntimeError("platform has no reply component")

    event = _ChainRecordingEvent(umo="s1", sender_id="u1")
    # 引用 ID 取自 message_obj.message_id：给裸字符串会被当成"取不到 ID"而降级，
    # 那样本条用例根本不进 if quote_id 分支（恒绿）。
    event.message_obj = SimpleNamespace(message_id="msg-42")
    last_events["s1"] = event
    original = _mod.Reply
    _mod.Reply = _BoomReply
    try:
        outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    finally:
        _mod.Reply = original
    assert outcome.status is models.SendStatus.DELIVERED
    assert event.sent_chains, "引用失败不应阻止发送"
    assert len(calls) == 1, "降级分支未被触发（quote 路径根本没跑到构造点）"
    assert not _at_items(event.sent_chains[0]), "quote 降级不应误插 @"
    # 失败的 Reply 不得以半成品形态留在链里：插上去就是平台收到一个坏组件，
    # 而降级契约要求"装饰性组件不得影响投递结果与记账"（契约 §14.1）。
    assert not [
        item for item in event.sent_chains[0] if type(item).__name__ in {"Reply", "_FakeReply"}
    ], "quote 构造失败后链里仍留有 Reply 组件"


async def test_mention_host_at_stub_accepts_qq_and_name(tmp_path: Path) -> None:
    """守卫：``At`` 桩必须接受生产实际使用的 ``qq=`` 与 ``name=`` 关键字。

    空壳 ``type("At", (), {})`` 会让构造抛 TypeError，被
    ``_attach_chain_prefix`` 的 except 吞掉，所有"已插入 @"断言会静默通过。
    ``name=`` 同样要守：这个名字一旦不被接受，线上就是永远不出现 @ 而测试全绿。
    """
    from . import host_stubs

    host_stubs.install_astrbot_stubs()
    import astrbot.api.message_components as components

    at = components.At(qq="u1", name="小明")
    assert str(at.qq) == "u1"
    assert at.name == "小明"
    assert str(at) == "", "At 不得进入 get_plain_text 正文"
