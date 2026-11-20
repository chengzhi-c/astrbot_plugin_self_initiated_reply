"""DeliveryRunner 独立单测：注入假门卫/发送器/钩子，脱离插件实例。

覆盖：
- UNKNOWN 投递不自动重试、不触发 after-send 钩子；会话记账由 pipeline 收口
- apply/persist 分开：保存重试不得重复改配额或历史
- 工具直发与文本回复的混合出口（直发无文本/纯文本/两者都有）返回文案不变
- 发送前门卫拦截时：有直发仍返回完成提示，无直发则纯跳过
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ._support import core_loader
from .host_stubs import FakeContextSend, FakeEvent, FlipGate, capture_logs

PACKAGE_NAME = "selfreply_delivery_test_package"
_load_modules = core_loader(PACKAGE_NAME)


def _delivery_module():
    return importlib.import_module(f"{PACKAGE_NAME}.delivery")


class _CountingGate:
    """前 true_times 次 is_current 返回 True，之后一律 False，并记录调用次数。"""

    def __init__(self, *, true_times: int) -> None:
        self.remaining = true_times
        self.calls = 0

    def is_current(self, umo: str, generation: object) -> bool:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return True
        return False


class FakeHook:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def __call__(self, event: object, event_type: object) -> None:
        self.calls.append((event, event_type))


class FakeSender:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, object]] = []

    async def __call__(
        self,
        umo: str,
        reply: str,
        expected_generation: object = None,
        **_kwargs: object,
    ) -> object:
        self.calls.append((umo, reply, expected_generation))
        return self.outcome


class FakeSave:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("storage broken")


def _make_runner(
    tmp_path: Path,
    *,
    sender: FakeSender | None = None,
    sender_status: str | None = None,
    hook: FakeHook | None = None,
    context_send: FakeContextSend | None = None,
    save: FakeSave | Callable[[], Awaitable[None]] | None = None,
    gate_current: bool = True,
    local_gate: str = "",
    config: dict | None = None,
    random_value: Callable[[], float] | None = None,
):
    from . import host_stubs

    host_stubs.install_astrbot_stubs()  # delivery 顶层导入宿主私有符号
    _, _, models = _load_modules()
    delivery_mod = _delivery_module()
    settings = models.Settings.from_config(config or {})
    last_events: dict[str, object] = {}
    gate = SimpleNamespace(is_current=lambda umo, generation: gate_current)
    if sender is None and sender_status is not None:
        # 替身显式声明 SuppressCode，确保走到对应的停止原因分支。
        outcome = models.SendOutcome(
            models.SendStatus(sender_status),
            "",
            models.SuppressCode.GENERATION_CHANGED
            if models.SendStatus(sender_status) is models.SendStatus.SUPPRESSED
            else None,
        )
        sender = FakeSender(outcome)
    delivered = delivery_mod.DeliveryRunner(
        settings=settings,
        gate=gate,
        local_gate=lambda state, force: local_gate,
        last_events=last_events,
        call_hook=hook if hook is not None else FakeHook(),
        context_send=context_send if context_send is not None else FakeContextSend(),
        save_storage=save if save is not None else FakeSave(),
        runtime=lambda: SimpleNamespace(
            # 复用宿主桩的 MessageEventResult（message 链式 + chain 属性）
            new_event_result=host_stubs._FakeMessageEventResult,
            result_llm_type="llm",
            event_type=SimpleNamespace(
                OnDecoratingResultEvent=SimpleNamespace(name="OnDecoratingResultEvent"),
                OnAfterMessageSentEvent=SimpleNamespace(name="OnAfterMessageSentEvent"),
            ),
        ),
        **({"random_value": random_value} if random_value is not None else {}),
    )
    if sender is not None:
        delivered.send_reply = sender
    return delivery_mod, models, delivered, last_events


def _state(models, *, observed_at: float = 50.0, active_at: float = 100.0):
    state = models.SessionState()
    state.last_active_at = active_at
    state.last_proactive_observed_at = observed_at
    return state


def _hook_names(hook: FakeHook) -> list[str]:
    return [event_type.name for _event, event_type in hook.calls]


# ============================================================================
# UNKNOWN 不自动重试、不触发 after-send 钩子、消耗状态并推进观察窗口
# ============================================================================


async def test_send_reply_is_suppressed_after_lifecycle_stop(tmp_path: Path) -> None:
    _, models, delivery, _ = _make_runner(tmp_path)
    delivery._is_stopping = lambda: True

    result = await delivery.send_reply("s1", "reply", expected_generation=None)

    assert result.status is models.SendStatus.SUPPRESSED


async def test_stop_during_decorating_hook_blocks_event_send(tmp_path: Path) -> None:
    _, models, delivery, last_events = _make_runner(tmp_path)
    event = FakeEvent(umo="s1")
    last_events["s1"] = event
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()
    stopping = False

    async def hook(_event: object, event_type: object) -> None:
        nonlocal stopping
        if getattr(event_type, "name", "") == "OnDecoratingResultEvent":
            hook_started.set()
            await release_hook.wait()
        stopping = True

    delivery._call_hook = hook
    delivery._is_stopping = lambda: stopping
    send_task = asyncio.create_task(delivery.send_reply("s1", "reply", expected_generation=None))
    await hook_started.wait()
    release_hook.set()
    result = await send_task

    assert result.status is models.SendStatus.SUPPRESSED
    assert event.sent_texts == []


async def test_deliver_unknown_consumes_state_without_retry(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path, sender_status="unknown")
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert "未自动重试" in result
    assert state.daily_count == 0
    assert state.last_proactive_at == 0.0
    assert all(record.role != "assistant" for record in state.recent)


async def test_send_reply_unknown_skips_after_send_hook(tmp_path: Path) -> None:
    """UNKNOWN（send 抛异常，真实适配器失败形态）不得触发 after-send hook。"""
    _, models, runner, last_events = _make_runner(tmp_path)
    event = FakeEvent()
    last_events["s1"] = event

    async def failing_send(_message):
        raise RuntimeError("adapter disconnected")

    event.send = failing_send
    hook = FakeHook()
    runner._call_hook = hook
    outcome = await runner.send_reply("s1", "测试回复", expected_generation=1)

    assert outcome.status is models.SendStatus.UNKNOWN
    assert _hook_names(hook) == ["OnDecoratingResultEvent"]  # 无 after-send


async def test_send_reply_delivered_triggers_after_send_hook(tmp_path: Path) -> None:
    _, models, runner, last_events = _make_runner(tmp_path)
    event = FakeEvent()
    last_events["s1"] = event
    hook = FakeHook()
    runner._call_hook = hook
    outcome = await runner.send_reply("s1", "测试回复", expected_generation=1)

    assert outcome.status is models.SendStatus.DELIVERED
    assert _hook_names(hook) == ["OnDecoratingResultEvent", "OnAfterMessageSentEvent"]


async def test_record_unconfirmed_sets_state_fields(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path)
    state = _state(models)
    runner.apply_proactive_state("s1", state, "", 0, observed_active_at=100.0, confirmed=False)
    ok = await runner.persist_proactive_state()
    assert ok is True
    assert state.daily_count == 1
    assert state.last_proactive_observed_at == 100.0
    assert all(record.role != "assistant" for record in state.recent)


# ============================================================================
# 观察窗口推进语义（代次未变必推进；代次已变跳过记录）
# ============================================================================


async def test_deliver_reply_returns_delivered_text_without_recording_state(
    tmp_path: Path,
) -> None:
    """DELIVERED 出口只回文案：记账归 ``apply_proactive_state``/``persist_proactive_state``。

    ``deliver_reply`` 自己不碰状态（本节标题里的「观察窗口推进」由下面几条
    apply/persist 用例钉住），故这里不断言状态字段——``_state`` 的种子值会让
    那样的断言恒真。只留「发送成功时返回的那串文案」这一条真实出口。
    """
    _, models, runner, _ = _make_runner(tmp_path)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert result == "已主动回复。"


async def test_record_stale_generation_skips_observation_advance(tmp_path: Path) -> None:
    """代次已变：冷却仍记录，但观察窗口不得推进（避免覆盖新会话语义）。"""
    _, models, runner, _ = _make_runner(tmp_path, gate_current=False)
    state = _state(models)
    runner.apply_proactive_state(
        "s1", state, "你好", 0, expected_generation=999, observed_active_at=200.0
    )
    ok = await runner.persist_proactive_state()
    assert ok is True
    assert state.last_proactive_at > 0  # 冷却与配额仍消耗
    assert state.daily_count == 1
    assert state.last_proactive_observed_at == 50.0  # 观察窗口未推进


async def test_record_unconfirmed_stale_generation_skips_observation_advance(
    tmp_path: Path,
) -> None:
    """UNKNOWN 也受代次门约束：代次已变时同样不得推进观察窗口（契约 §2）。

    与上一条配对，上一条走 ``confirmed=True`` 的 ``elif`` 分支，这条走
    ``confirmed=False`` 的内层代次判据。删掉内层判据时，一次「提交状态未知」的
    旧事件会在新会话上把观察窗口推到旧事件时间，静默掩盖新消息。
    """
    _, models, runner, _ = _make_runner(tmp_path, gate_current=False)
    state = _state(models)
    runner.apply_proactive_state(
        "s1",
        state,
        "你好",
        0,
        expected_generation=999,
        observed_active_at=200.0,
        confirmed=False,
    )
    ok = await runner.persist_proactive_state()
    assert ok is True
    assert state.daily_count == 1  # 提交已发生，配额与冷却照扣
    assert state.last_proactive_observed_at == 50.0  # 但观察窗口不推进


async def test_apply_then_persist_retry_does_not_duplicate_state(tmp_path: Path) -> None:
    """Save-only retries never increment quota or append history twice."""
    writes: list[str] = []
    failures = 1

    async def save() -> None:
        nonlocal failures
        writes.append("save")
        if failures:
            failures -= 1
            raise OSError("disk unavailable")

    _, models, runner, _ = _make_runner(tmp_path, save=save)
    state = _state(models)
    runner.apply_proactive_state("s1", state, "你好", 0, observed_active_at=100.0)

    assert state.daily_count == 1
    assert len(state.recent) == 1
    # 正向锚点：代次未变时观察窗口必须推到 observed_active_at。删掉这条，
    # 上面「不得重复」只守住不增量，守不住「根本没推进」。
    assert state.last_proactive_observed_at == 100.0
    assert await runner.persist_proactive_state() is False
    assert await runner.persist_proactive_state() is True
    assert writes == ["save", "save"]
    assert state.daily_count == 1
    assert len(state.recent) == 1


async def test_apply_then_persist_persists_every_record(tmp_path: Path) -> None:
    """落盘契约：每次 apply+persist 返回时状态已持久化，无延迟窗口。

    注入的回调即 ``_save_storage``
    本体（串行锁 + to_thread 原子写）。本测试锁定"记录即落盘"：
    崩溃窗口为零，不存在"已发送但状态未落盘"的中间态。
    """
    writes: list[str] = []

    async def save() -> None:
        writes.append("save")

    _, models, runner, _ = _make_runner(tmp_path, save=save)
    state = _state(models)

    runner.apply_proactive_state("s1", state, "你好", 0)
    ok = await runner.persist_proactive_state()
    assert ok is True
    assert writes == ["save"], "记录即落盘，不得延迟"

    runner.apply_proactive_state("s1", state, "第二条", 0)
    await runner.persist_proactive_state()
    assert writes == ["save", "save"], "每条记录各自落盘"


# ============================================================================
# 混合出口三分支（直发无文本 / 纯文本 / 两者都有）
# ============================================================================


async def test_deliver_direct_only_no_text_send(tmp_path: Path) -> None:
    """仅有工具直发：不发文本，返回专用消息，仍记录尝试。"""
    _, models, runner, _ = _make_runner(tmp_path)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "",
        2,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert result == "已通过工具主动回复。"
    assert state.daily_count == 0
    assert state.last_proactive_text == ""


async def test_deliver_both_text_and_directs(tmp_path: Path) -> None:
    """两者都有：以文本回复为主（不返回"已通过工具"）。"""
    _, models, runner, _ = _make_runner(tmp_path)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "补充文本",
        3,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert result == "已主动回复。"
    assert state.last_proactive_text == ""


async def test_deliver_failed_before_submit_no_directs_no_record(tmp_path: Path) -> None:
    """FAILED_BEFORE_SUBMIT 且无直发：不得消耗配额（未提交）。"""
    _, models, runner, _ = _make_runner(tmp_path, sender_status="failed_before_submit")
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert result == "主动发送失败。"
    assert state.daily_count == 0
    assert state.last_proactive_at == 0.0


async def test_deliver_suppressed_with_directs_records(tmp_path: Path) -> None:
    """SUPPRESSED（代次已变）但有工具直发：仍记录直发尝试。"""
    _, models, runner, _ = _make_runner(tmp_path, sender_status="suppressed")
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        2,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert result == "会话已更新，放弃旧回复。"
    assert state.daily_count == 0


async def test_deliver_suppressed_while_stopping_reports_stop(tmp_path: Path) -> None:
    """停止成因的 SUPPRESSED 回显停止文案，不误报「会话已更新」。

    send_reply 的 SUPPRESSED 有两类成因：代次已变（generation changed）与
    插件停止（plugin is stopping）。统一回显 STALE_REPLY_MESSAGE 会把停止
    期间的抑制误报成会话更新，误导排障方向。
    """
    _, models, runner, _ = _make_runner(tmp_path)
    runner.send_reply = FakeSender(
        models.SendOutcome(
            models.SendStatus.SUPPRESSED,
            "plugin is stopping",
            models.SuppressCode.STOPPING,
        )
    )
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=None,
        force=False,
        trigger="patrol",
    )

    assert result == "插件正在停止，放弃回复。"


async def test_deliver_reply_reports_stopping_when_lifecycle_stopped(tmp_path: Path) -> None:
    """投递入口的停机关口报停止文案，且不落任何 attempt。

    与 ``send_reply`` 的 ``SuppressCode.STOPPING`` 分支同一成因、必须同一文案：
    两处文案一旦混同，「停止中」就会被误导向改配置排障。真实停机关口先于代次
    闸门：停机中的在途投递不能计配额。
    """
    _, models, runner, _ = _make_runner(tmp_path)
    runner._is_stopping = lambda: True
    ledger = models.AttemptLedger()
    state = _state(models)

    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        ledger=ledger,
        expected_generation=None,
        force=False,
        trigger="patrol",
    )

    assert result == models.STOPPING_REPLY_TEXT == "插件正在停止，放弃回复。"
    assert ledger.attempts == ()
    assert state.daily_count == 0
    assert state.last_proactive_at == 0.0


async def test_deliver_stopping_suppression_is_read_from_code_not_detail(tmp_path: Path) -> None:
    """判定取 ``code``：detail 措辞变化不得改变回显文案。

    与上一条配对，上一条走生产构造路径（detail 与 code 一致），这一条把
    detail 换成不含 "stopping" 字样的措辞、code 仍为 STOPPING：靠文案判定的
    实现会在这里退回 STALE_REPLY_MESSAGE，把停止期间的抑制误报成会话更新。
    """
    _, models, runner, _ = _make_runner(tmp_path)
    runner.send_reply = FakeSender(
        models.SendOutcome(
            models.SendStatus.SUPPRESSED,
            "插件正在停止，放弃回复。",  # 措辞里没有 "stopping"
            models.SuppressCode.STOPPING,
        )
    )
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=None,
        force=False,
        trigger="patrol",
    )

    assert result == "插件正在停止，放弃回复。"


async def test_deliver_non_stopping_suppression_stays_stale(tmp_path: Path) -> None:
    """非停止成因（代次已变）的 SUPPRESSED 回 STALE_REPLY_MESSAGE。"""
    _, models, runner, _ = _make_runner(tmp_path)
    runner.send_reply = FakeSender(
        models.SendOutcome(
            models.SendStatus.SUPPRESSED,
            "generation changed before send",
            models.SuppressCode.GENERATION_CHANGED,
        )
    )
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=None,
        force=False,
        trigger="patrol",
    )

    assert result == "会话已更新，放弃旧回复。"


async def test_deliver_sealed_ledger_suppression_is_not_reported_as_stale(tmp_path: Path) -> None:
    """账本已封的 SUPPRESSED 不得报「会话已更新」。

    成因是关停/隔离期迟到的发送被 gateway 以 GATE_REJECTED 拒绝
    （ledger already sealed），与代次无关。统一回 STALE_REPLY_MESSAGE 会把
    排障方向指向「会话已更新」，与本文件声明的 SUPPRESSED 文案按成因分流相违。
    """
    delivery_mod, models, runner, last_events = _make_runner(tmp_path)
    last_events["s1"] = FakeEvent()
    ledger = models.AttemptLedger()
    ledger.seal()

    result = await runner.deliver_reply(
        "s1",
        _state(models),
        "你好",
        0,
        ledger=ledger,
        expected_generation=None,
        force=False,
        trigger="patrol",
    )

    assert result == "本次投递已收尾，放弃这条迟到的回复。"
    assert result == delivery_mod.LEDGER_SEALED_REPLY_TEXT
    assert "会话已更新" not in result


async def test_deliver_stopping_suppression_outranks_generation_change(tmp_path: Path) -> None:
    """停止成因的 SUPPRESSED 必须先于代次复核判定，代次同窗口变了的组合也要报停止。

    缺陷形态：装饰 hook 是一次 await，期间插件重载会同时置 ``_stopping`` 并推进
    代次。旧顺序先查 ``is_current``，"因停止而抑制"被误报成「会话已更新」，排障
    方向被指错（DECISIONS.md 的 SUPPRESSED 文案按成因分流一节）。

    与 ``test_deliver_suppressed_while_stopping_reports_stop`` 的分工：那条只覆盖
    代次未变的一半（旧实现在那里也绿）；本条把同窗口内代次也变了的组合钉住，
    FlipGate 让发送前的代次复核通过、发送后的复核翻转。
    """
    _, models, runner, _ = _make_runner(tmp_path)
    runner.send_reply = FakeSender(
        models.SendOutcome(
            models.SendStatus.SUPPRESSED,
            "plugin is stopping",
            models.SuppressCode.STOPPING,
        )
    )

    # 对照：hook await 期间只有 _stopping 被置位，代次未变
    current = await runner.deliver_reply(
        "s1",
        _state(models),
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=7,
        force=False,
        trigger="patrol",
    )
    assert current == "插件正在停止，放弃回复。"

    # 同一窗口内代次也变了：第一次 is_current 过发送前闸门，之后一律过期
    runner._gate = FlipGate(true_times=1)
    stale = await runner.deliver_reply(
        "s1",
        _state(models),
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=7,
        force=False,
        trigger="patrol",
    )
    assert stale == "插件正在停止，放弃回复。"


async def test_send_via_event_stopping_outranks_generation_change(tmp_path: Path) -> None:
    """复核点 2/2 内部：停止判定必须先于代次复核（_send_via_event 级）。

    缺陷形态：deliver_reply 的 SUPPRESSED 文案按成因分流依赖 outcome.code，
    而 _send_via_event 原顺序先查 is_current。装饰 hook 是一次 await，插件
    重载会在同一窗口既置 _stopping 又推进代次，返回 GENERATION_CHANGED，
    运营者看到「会话已更新」而非「插件正在停止」。既有
    test_deliver_stopping_suppression_outranks_generation_change 的发送器是
    替身，只钉到 deliver_reply 级；本条驱动真实 _send_via_event。
    """
    _, models, delivery, last_events = _make_runner(tmp_path)
    event = FakeEvent(umo="s1")
    last_events["s1"] = event
    hook_started = asyncio.Event()
    release_hook = asyncio.Event()
    stopping = False
    gate = _CountingGate(true_times=1)

    async def hook(_event: object, event_type: object) -> None:
        nonlocal stopping
        if getattr(event_type, "name", "") == "OnDecoratingResultEvent":
            hook_started.set()
            await release_hook.wait()
            # 同一 await 窗口内重载做的两件事：置停止 + 推进代次
            stopping = True
            gate.remaining = 0

    delivery._call_hook = hook
    delivery._is_stopping = lambda: stopping
    delivery._gate = gate
    send_task = asyncio.create_task(delivery.send_reply("s1", "reply", expected_generation=7))
    await hook_started.wait()
    release_hook.set()
    result = await send_task

    assert result.status is models.SendStatus.SUPPRESSED
    assert result.code is models.SuppressCode.STOPPING


async def test_deliver_stopping_with_direct_sends_reports_completion(tmp_path: Path) -> None:
    """停止早退不得无视工具直发：账本会为直发记一次消耗配额的主动尝试。

    缺陷形态：deliver_reply 见 _stopping 直接回 STOPPING_REPLY_TEXT，而
    direct_send_count>0 时 session_pipeline._record_ledger 照记
    「工具主动发送 xN」并消耗冷却与日配额——回执说「放弃回复」仿佛什么都
    没发生，与落盘状态互相矛盾。
    """
    _, models, runner, _ = _make_runner(tmp_path)
    runner._is_stopping = lambda: True

    result = await runner.deliver_reply(
        "s1",
        _state(models),
        "",
        2,
        ledger=models.AttemptLedger(),
        expected_generation=None,
        force=False,
        trigger="patrol",
    )

    assert result == "工具主动回复已完成；插件正在停止，放弃回复。"


# ============================================================================
# 发送前门卫
# ============================================================================


async def test_deliver_gate_block_with_directs_records(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path, gate_current=False)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "",
        2,
        ledger=models.AttemptLedger(),
        expected_generation=999,
        force=False,
        trigger="patrol",
    )

    assert "工具主动回复已完成" in result
    assert "会话已经更新" in result
    assert state.daily_count == 0


async def test_deliver_gate_block_without_directs_no_record(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path, gate_current=False)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=999,
        force=False,
        trigger="patrol",
    )

    assert result == "会话已经更新，放弃旧任务。"
    assert state.daily_count == 0
    assert state.last_proactive_at == 0.0


async def test_deliver_local_gate_block_with_directs_records(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path, local_gate="冷却中。")
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "",
        1,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert "工具主动回复已完成；冷却中。" == result
    assert state.daily_count == 0
    assert state.last_proactive_text == ""
    assert [item.role for item in state.recent] == []


async def test_deliver_confirmed_failure_with_directs_skips_history(tmp_path: Path) -> None:
    """发送确定失败（非 UNKNOWN）时，即使有工具直发也不消耗配额、不写历史。

    FAILED_BEFORE_SUBMIT 表示平台侧确定未提交（行为契约 §2）：工具直发是另一条
    链路的既成副作用，不改变"本次最终发送未送达"这一事实，故配额与 assistant
    历史都不动。
    """
    _, models, runner, _ = _make_runner(tmp_path, sender_status="failed_before_submit")
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "正文",
        2,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="message_delay",
    )

    assert result == "主动发送失败。"
    assert state.daily_count == 0
    assert state.last_proactive_text == ""
    assert [item.role for item in state.recent] == []


# ============================================================================
# send_reply 内部状态机（钩子装饰 → 复核 → 事件发送 / context 兜底）
# ============================================================================


async def test_send_reply_stale_before_hooks_skips_hooks(tmp_path: Path) -> None:
    """钩子前代次复核：代次失效时连装饰钩子都不得触发（避免无谓副作用）。"""
    _, models, runner, last_events = _make_runner(tmp_path, gate_current=False)
    event = FakeEvent()
    last_events["s1"] = event
    hook = FakeHook()
    runner._call_hook = hook
    outcome = await runner.send_reply("s1", "你好", expected_generation=999)
    assert outcome.status is models.SendStatus.SUPPRESSED
    assert _hook_names(hook) == []


async def test_send_reply_context_fallback_when_no_event(tmp_path: Path) -> None:
    """完全没有事件可用（无冻结目标、表项也为空）→ context 兜底，仍记 DELIVERED。"""
    delivery, models, runner, _ = _make_runner(tmp_path)
    context_send = FakeContextSend()
    runner._context_send = context_send
    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    assert outcome.status is models.SendStatus.DELIVERED
    assert [umo for umo, _msg in context_send.calls] == ["s1"]
    # §14.1：context 兜底路径不引用——该路径的前提就是事件已不在手边，没有消息
    # ID 可用，插进去只会引到一个空/错的消息。判据按 delivery 自己绑定的 Reply
    # 取（与 test_mention 的 _at_items 同款具名判据），不写"链里只有正文"那种
    # 可被改写的恒真形式。
    sent = context_send.calls[0][1]
    chain = list(getattr(sent, "chain", []) or [])
    assert chain, "context 兜底没有链内容，本条断言会空转"
    assert not any(isinstance(item, delivery.Reply) for item in chain), "context 路径不得引用"


# ============================================================================
# 引用（quote_mode / quote_probability）
# ============================================================================


def _event_with_id(message_id: str = "m1"):
    """带 message_obj.message_id 的事件（宿主真实形态：ID 常挂在 message_obj 上）。"""
    event = FakeEvent()
    event.message_obj = SimpleNamespace(message_id=message_id)
    return event


def _capture_sent_chains(event) -> list[list]:
    """抓发送瞬间的消息链。

    投递完成后 ``clear_result`` 会把事件结果回收，事后取不到链，故在 ``send``
    入口处快照，这也正是宿主适配器实际拿到的对象。
    """
    chains: list[list] = []
    original = event.send

    async def send(message):
        chains.append(list(getattr(message, "chain", []) or []))
        return await original(message)

    event.send = send
    return chains


async def test_quote_off_never_inserts_reply_component(tmp_path: Path) -> None:
    """quote_mode=off：即使概率 100 也不引用（默认值必须是"不改变现有行为"）。"""
    delivery_mod, models, runner, last_events = _make_runner(
        tmp_path, config={"quote_mode": "off", "quote_probability": 100}
    )
    event = _event_with_id()
    last_events["s1"] = event
    chains = _capture_sent_chains(event)

    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    assert outcome.status is models.SendStatus.DELIVERED
    assert len(chains) == 1
    assert len(chains[0]) == 1, chains[0]  # 只有正文
    assert not isinstance(chains[0][0], delivery_mod.Reply)


async def test_quote_random_mode_quotes_at_probability_extremes(tmp_path: Path) -> None:
    """random 模式：100 必引用、0 必不引用，且引用组件在被引消息 ID 上。"""
    _, models, runner, last_events = _make_runner(
        tmp_path, config={"quote_mode": "random", "quote_probability": 100}
    )
    event = _event_with_id("msg-42")
    last_events["s1"] = event
    chains = _capture_sent_chains(event)

    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    assert outcome.status is models.SendStatus.DELIVERED
    assert len(chains[0]) == 2
    quote, text = chains[0]
    assert getattr(quote, "id", None) == "msg-42"
    assert text == "你好"

    _, _, never, never_events = _make_runner(
        tmp_path, config={"quote_mode": "random", "quote_probability": 0}
    )
    never_event = _event_with_id()
    never_events["s1"] = never_event
    never_chains = _capture_sent_chains(never_event)
    await never.send_reply("s1", "你好", expected_generation=1)
    assert len(never_chains[0]) == 1


async def test_quote_follows_the_pinned_target_event(tmp_path: Path) -> None:
    """引用必须锚在本次依据的那条消息上，而不是投递时刻的最新消息。

    缺陷形态：``send_reply`` 现读 ``_last_events``。判断与生成是两次模型调用，
    期间到达的新消息会把该表换成别人的消息——引用指向一条本次从未依据过的
    消息，运营者看到的是「引用错对象」。
    """
    _, models, runner, last_events = _make_runner(
        tmp_path, config={"quote_mode": "random", "quote_probability": 100}
    )
    original = _event_with_id("msg-original")
    newcomer = _event_with_id("msg-newcomer")
    last_events["s1"] = newcomer  # 判断/生成期间到达的新消息抢走了 _last_events
    original_chains = _capture_sent_chains(original)
    newcomer_chains = _capture_sent_chains(newcomer)

    outcome = await runner.send_reply("s1", "你好", expected_generation=1, target_event=original)

    assert outcome.status is models.SendStatus.DELIVERED
    # 装饰与通道都用冻结目标：新消息事件是宿主仍在处理的槽，不能借用。
    assert not newcomer_chains, "发送走了生成期间新到达的消息事件"
    quote, text = original_chains[0]
    assert getattr(quote, "id", None) == "msg-original", "引用指向了生成期间到达的消息"
    assert text == "你好"


async def test_quote_model_mode_obeys_the_judge(tmp_path: Path) -> None:
    """model 模式：模型说引用就引用、说不引用就不引用，概率无权覆盖模型。"""
    for decision, expected_quote in ((True, True), (False, False)):
        _, _, runner, last_events = _make_runner(
            tmp_path, config={"quote_mode": "model", "quote_probability": 100}
        )
        event = _event_with_id()
        last_events["s1"] = event
        chains = _capture_sent_chains(event)

        await runner.send_reply("s1", "你好", expected_generation=1, quote=decision)

        assert len(chains[0]) == (2 if expected_quote else 1), (decision, chains[0])


async def test_quote_model_mode_falls_back_to_probability(tmp_path: Path) -> None:
    """model 模式下模型未表态（None：手动检查）→ 按概率兜底。"""
    _, _, always, always_events = _make_runner(
        tmp_path,
        config={"quote_mode": "model", "quote_probability": 100},
        random_value=lambda: 0.99,
    )
    always_event = _event_with_id()
    always_events["s1"] = always_event
    always_chains = _capture_sent_chains(always_event)
    await always.send_reply("s1", "你好", expected_generation=1, quote=None)
    assert len(always_chains[0]) == 2

    _, _, never, never_events = _make_runner(
        tmp_path,
        config={"quote_mode": "model", "quote_probability": 50},
        random_value=lambda: 0.99,
    )
    never_event = _event_with_id()
    never_events["s1"] = never_event
    never_chains = _capture_sent_chains(never_event)
    await never.send_reply("s1", "你好", expected_generation=1, quote=None)
    assert len(never_chains[0]) == 1


async def test_quote_skipped_without_message_id(tmp_path: Path) -> None:
    """取不到消息 ID 时降级为普通发送，而不是让整次回复失败。"""
    _, models, runner, last_events = _make_runner(
        tmp_path, config={"quote_mode": "random", "quote_probability": 100}
    )
    event = FakeEvent()  # 无 message_obj、无 message_id
    last_events["s1"] = event
    chains = _capture_sent_chains(event)

    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    assert outcome.status is models.SendStatus.DELIVERED
    assert len(chains[0]) == 1


# ============================================================================
# send_reply 异常与分支路径（代次复核失效点、外发未提交、钩子异常、context 兜底）
# ============================================================================


class _ClearingHook:
    """装饰钩子：吃掉事件结果（直接置空，模拟钩子消费内容）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def __call__(self, event: object, event_type: object) -> None:
        self.calls.append((event, event_type))
        event._result = None


class _BoomHook:
    """装饰钩子直接抛错（必须降级为未装饰发送，不得丢弃整条回复）。"""

    async def __call__(self, event: object, event_type: object) -> None:
        raise RuntimeError("decorating hook broken")


class _AfterSendBoomHook(FakeHook):
    """装饰正常、after-send 抛错（必须 warning 吞掉不影响投递结果）。"""

    async def __call__(self, event: object, event_type: object) -> None:
        self.calls.append((event, event_type))
        if len(self.calls) > 1:
            raise RuntimeError("after-send hook broken")


class _FalseSendEvent(FakeEvent):
    """事件发送明确返回 False（未提交）的事件桩。"""

    async def send(self, message):
        return False


# ============================================================================
# send_reply：事件路径分支
# ============================================================================


async def test_send_reply_hook_empty_result(tmp_path: Path) -> None:
    """装饰钩子清空结果 → FAILED_BEFORE_SUBMIT。"""
    _, models, runner, last_events = _make_runner(tmp_path, hook=_ClearingHook())
    last_events["s1"] = FakeEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.FAILED_BEFORE_SUBMIT
    assert "no result" in outcome.detail


async def test_decorating_hook_stop_suppresses_delivery(tmp_path: Path) -> None:
    _, models, runner, last_events = _make_runner(tmp_path)
    event = FakeEvent()
    last_events["s1"] = event
    ledger = models.AttemptLedger()

    async def stop_hook(event, event_type):
        event.stop_event()
        return True

    runner._call_hook = stop_hook
    result = await runner.deliver_reply(
        "s1",
        _state(models),
        "hello",
        0,
        ledger=ledger,
        expected_generation=None,
        force=True,
        trigger="manual",
    )
    assert ledger.attempts == ()
    assert event.get_result() is None
    assert result == "装饰钩子已中止回复。"


async def test_send_reply_suppressed_after_decorating(tmp_path: Path) -> None:
    """装饰钩子后代次翻转 → SUPPRESSED（复核点 2，唯一的 await 窗口）。"""
    _, models, runner, last_events = _make_runner(tmp_path)
    runner._gate = FlipGate(true_times=1)
    last_events["s1"] = FakeEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=7)
    assert outcome.status is models.SendStatus.SUPPRESSED
    assert "after decorating" in outcome.detail


async def test_send_reply_no_gate_recheck_between_check_and_send(tmp_path: Path) -> None:
    """代次复核到 send 之间零 await：中间不再插一道复核。

    删除依据：两个相邻复核点之间的同步段里没有任何协程能推进代次，那道复核
    恒为假，属于死分支。本条把这条结构钉住：同一 gate 实例被查询的次数
    只应是入口一次 + 装饰钩子后一次。
    """
    _, models, runner, last_events = _make_runner(tmp_path)
    gate = _CountingGate(true_times=2)
    runner._gate = gate
    last_events["s1"] = FakeEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=7)

    assert gate.calls == 2, "复核点到 send 之间不得再有代次查询"
    assert outcome.status is models.SendStatus.DELIVERED


async def test_send_reply_outbound_not_submitted(tmp_path: Path) -> None:
    """事件发送返回 False：未提交，清理结果并原样回传分类。"""
    _, models, runner, last_events = _make_runner(tmp_path)
    last_events["s1"] = _FalseSendEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.FAILED_BEFORE_SUBMIT


async def test_send_reply_after_send_hook_error_still_delivered(tmp_path: Path) -> None:
    """after-send 钩子抛错：warning 吞掉，投递结果仍为 DELIVERED。"""
    _, models, runner, last_events = _make_runner(tmp_path, hook=_AfterSendBoomHook())
    last_events["s1"] = FakeEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.DELIVERED


async def test_send_reply_after_send_hook_error_log_is_redacted(
    tmp_path: Path, caplog: object
) -> None:
    """after-send 钩子失败分支的 warning 必须脱敏，与同函数另一条 except 同口径。

    钩子链上任意第三方插件的处理器抛异常时，异常文本原样进 WARNING。provider
    类插件的异常串标准形态就是 ``Client error '401' for url '...?rkey=...'``，
    三个凭证一起落盘。这是整条管线里异常面最宽的一处（不是本插件自己的代码），
    而同函数下方 send 失败那条早已走 redact_exc_text 并注明"脱敏口径同 decision"。
    """
    signed_url = (
        "https://img.example.com/group/abc/0/media.jpg"
        "?rkey=SECRET_RKEY_VALUE&Signature=SECRET_SIGNATURE&token=SECRET_TOKEN"
    )
    secrets = ("SECRET_RKEY_VALUE", "SECRET_SIGNATURE", "SECRET_TOKEN")

    class _SigningHook(FakeHook):
        """装饰正常、after-send 抛「异常文本含签名 URL」的错。"""

        async def __call__(self, event: object, event_type: object) -> None:
            self.calls.append((event, event_type))
            if len(self.calls) > 1:
                raise RuntimeError(f"Client error '401' for url '{signed_url}'")

    delivery_mod, models, runner, last_events = _make_runner(tmp_path, hook=_SigningHook())
    last_events["s1"] = FakeEvent()
    with capture_logs(caplog, delivery_mod.logger, logging.WARNING):
        outcome = await runner.send_reply("s1", "hello", expected_generation=None)

    # 脱敏不得改变投递结论：钩子失败仍按已送达记账
    assert outcome.status is models.SendStatus.DELIVERED
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("after-send hook failed" in message for message in warnings), (
        f"未捕获 after-send 钩子失败日志：{warnings}"
    )
    for secret in secrets:
        assert all(secret not in message for message in warnings), (
            f"after-send 钩子失败日志泄漏凭证 {secret}：{warnings}"
        )


async def test_send_reply_decorating_hook_error_still_delivered(tmp_path: Path) -> None:
    """装饰钩子抛错：按出错时手上的链照发，分类仍 DELIVERED。

    缺陷形态：装饰钩子是唯一一处未加保护的第三方钩子调用，异常落到外层
    ``except`` 时尚未发送，整条已生成的回复被判
    FAILED_BEFORE_SUBMIT —— 用户收不到消息、账本无 attempt（不消耗冷却与日
    配额、不写历史），下一轮对同一上下文再生成一次。同函数对 after-send 钩子
    已有等价降级，两处必须同一口径。
    """
    _, models, runner, last_events = _make_runner(tmp_path, hook=_BoomHook())
    event = FakeEvent()
    last_events["s1"] = event
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)

    assert event.sent_texts == ["hello"], "装饰钩子抛错吞掉了整条回复"
    assert outcome.status is models.SendStatus.DELIVERED


# ============================================================================
# send_reply：context 兜底路径
# ============================================================================


async def test_send_reply_context_send_unknown(tmp_path: Path) -> None:
    """context 发送抛错：可能已提交 → UNKNOWN（不得重试）。"""

    class BoomSend(FakeContextSend):
        async def __call__(self, umo: str, message: object) -> None:
            raise RuntimeError("adapter exploded mid-send")

    _, models, runner, _ = _make_runner(tmp_path, context_send=BoomSend())
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.UNKNOWN


async def test_send_reply_context_send_rejected_false(tmp_path: Path) -> None:
    """context 发送返回 False：无可达平台 → FAILED_BEFORE_SUBMIT。"""

    class FalseSend(FakeContextSend):
        async def __call__(self, umo: str, message: object):
            return False

    _, models, runner, _ = _make_runner(tmp_path, context_send=FalseSend())
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.FAILED_BEFORE_SUBMIT


async def test_deliver_context_cancellation_records_unknown_state(tmp_path: Path) -> None:
    """提交中的任务取消时，仍需把可能已送达的尝试记为 UNKNOWN。"""

    class CancelAfterStart(FakeContextSend):
        async def __call__(self, umo: str, message: object) -> None:
            self.calls.append((umo, message))
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

    hook = FakeHook()
    _, models, runner, _ = _make_runner(
        tmp_path,
        context_send=CancelAfterStart(),
        hook=hook,
    )

    state = _state(models)

    result = await runner.deliver_reply(
        "s1",
        state,
        "hello",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert "状态未知" in result
    assert state.daily_count == 0
    assert state.last_proactive_at == 0.0
    assert all(record.role != "assistant" for record in state.recent)
    assert hook.calls == []


async def test_deliver_event_cancellation_records_unknown_state(tmp_path: Path) -> None:
    """事件发送取消时，仍需清理结果并完成 UNKNOWN 状态记账。"""

    class CancelAfterStart(FakeEvent):
        async def send(self, message: object) -> None:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

    hook = FakeHook()
    _, models, runner, last_events = _make_runner(tmp_path, hook=hook)
    last_events["s1"] = CancelAfterStart()

    state = _state(models)

    result = await runner.deliver_reply(
        "s1",
        state,
        "hello",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert "状态未知" in result
    assert state.daily_count == 0
    assert state.last_proactive_at == 0.0
    assert all(record.role != "assistant" for record in state.recent)
    assert _hook_names(hook) == ["OnDecoratingResultEvent"]


async def test_deliver_cancel_after_send_start_no_retry(tmp_path: Path) -> None:
    """发送已开始后被 cancel：记 UNKNOWN、sender 只调一次（无重试）。"""

    send_calls = 0

    class CancelAfterStart(FakeEvent):
        async def send(self, message: object) -> None:
            nonlocal send_calls
            send_calls += 1
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

    _, models, runner, last_events = _make_runner(tmp_path)
    last_events["s1"] = CancelAfterStart()

    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "hello",
        0,
        ledger=models.AttemptLedger(),
        expected_generation=1,
        force=False,
        trigger="patrol",
    )

    assert "状态未知" in result
    assert send_calls == 1
    assert state.daily_count == 0


# ============================================================================
# deliver_reply：失败混合出口与日志分支
# ============================================================================


async def test_deliver_failure_with_directs_and_gate_flip(tmp_path: Path) -> None:
    """发送失败后代次已变：返回放弃旧回复，不改成发送失败。"""
    _, models, runner, _ = _make_runner(tmp_path, sender_status="failed_before_submit")
    runner._gate = FlipGate(true_times=1)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "hello",
        1,
        ledger=models.AttemptLedger(),
        expected_generation=7,
        force=False,
        trigger="message_delay",
    )
    assert result == "会话已更新，放弃旧回复。"


async def test_deliver_log_reply_content_preview(tmp_path: Path, caplog: Any) -> None:
    """长预览截断并带省略号，短预览保持原文；两者均使用 DEBUG。"""
    delivery_mod, models, runner, _ = _make_runner(
        tmp_path, save=FakeSave(), config={"log_reply_content": True}
    )
    state = _state(models)
    long_reply = "长" * 100
    short_reply = "短回复"
    limit = delivery_mod._LOG_REPLY_PREVIEW_CHARS

    with capture_logs(caplog, delivery_mod.logger, logging.DEBUG):
        long_result = await runner.deliver_reply(
            "s1",
            state,
            long_reply,
            0,
            ledger=models.AttemptLedger(),
            expected_generation=None,
            force=False,
            trigger="message_delay",
        )
        short_result = await runner.deliver_reply(
            "s1",
            state,
            short_reply,
            0,
            ledger=models.AttemptLedger(),
            expected_generation=None,
            force=False,
            trigger="message_delay",
        )

    assert long_result == "已主动回复。"
    assert short_result == "已主动回复。"

    previews = [
        record.getMessage()
        for record in caplog.records
        if "proactive reply sent" in record.getMessage() and "text=" in record.getMessage()
    ]
    assert len(previews) == 2, f"预览分支未记录（被改坏即只剩非预览那条）：{caplog.records}"
    assert f"text={long_reply[:limit]}…" in previews[0], (
        f"长回复未按 {limit} 字截断并加省略号：{previews[0]}"
    )
    assert f"chars={len(long_reply)}" in previews[0]
    assert f"text={short_reply}" in previews[1], f"短回复未原样记录：{previews[1]}"
    assert "…" not in previews[1], "短回复不该被截断"
