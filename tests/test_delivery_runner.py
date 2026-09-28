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

from .host_stubs import FakeEvent, capture_logs
from .test_vision import PACKAGE_NAME, _load_modules


def _delivery_module():
    return importlib.import_module(f"{PACKAGE_NAME}.delivery")


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


class FakeContextSend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def __call__(self, umo: str, message: object) -> None:
        self.calls.append((umo, message))
        return None


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
        # 替身也必须声明成因 code：真实 SUPPRESSED 一定带 code，留 None 会让
        # 被测分支走"非 STOPPING"的默认路径，掩盖成因判定本身（此前正是
        # 空 detail + None code 静默通过了「代次已变」文案断言）。
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
        local_gate=lambda state, force, silence_active_at=None: local_gate,
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


async def test_deliver_delivered_advances_observation_and_history(tmp_path: Path) -> None:
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
    assert state.daily_count == 0
    assert state.last_proactive_observed_at == 50.0
    assert state.last_proactive_text == ""


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


async def test_deliver_text_only_sends_once(tmp_path: Path) -> None:
    """纯文本：发送一次文本回复。"""
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


async def test_send_reply_stale_before_hooks_suppressed(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path, gate_current=False)
    outcome = await runner.send_reply("s1", "你好", expected_generation=999)
    assert outcome.status is models.SendStatus.SUPPRESSED


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
    """事件被清理（生成期间）→ 走 context 兜底路径，仍记 DELIVERED。"""
    _, models, runner, _ = _make_runner(tmp_path)
    context_send = FakeContextSend()
    runner._context_send = context_send
    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    assert outcome.status is models.SendStatus.DELIVERED
    assert [umo for umo, _msg in context_send.calls] == ["s1"]


async def test_context_send_pre_submit_failure_is_not_unknown(tmp_path: Path, monkeypatch) -> None:
    """context 路径提交前失败必须记 FAILED_BEFORE_SUBMIT，不得白吃冷却与日配额。

    ``MessageChain`` 构造失败发生在 ``outbound.send`` 调用之前，adapter 从未
    被触及。返回 UNKNOWN 会经 apply_proactive_state(confirmed=False) 消耗
    冷却与日配额，等于为一条从未发出的回复付费。

    本条只钉「提交前误记 UNKNOWN」这一侧；反向（已提交误降为提交前失败）
    由下两条守。三条合起来才能拦住全部无条件化改法。
    """
    delivery, models, runner, _ = _make_runner(tmp_path)

    class BoomChain:
        def __init__(self) -> None:
            raise RuntimeError("chain construction failed")

    monkeypatch.setattr(delivery, "MessageChain", BoomChain)

    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    assert outcome.status is models.SendStatus.FAILED_BEFORE_SUBMIT, (
        f"提交前失败被误判为 {outcome.status!r}，会白吃冷却与日配额"
    )


async def test_context_send_post_submit_failure_stays_unknown(tmp_path: Path, monkeypatch) -> None:
    """护栏：context 路径提交后失败必须仍记 UNKNOWN，不得降级为提交前失败。

    这条守的是上一条修复的反向风险。``_context_send`` 抛异常时 gateway 已调过
    adapter，结果不可知（可能已达），此时若日志分支再抛，外层 except 必须保持
    UNKNOWN，降级成 FAILED_BEFORE_SUBMIT 会让插件不消耗冷却而重发，制造重复
    消息。故修复必须是条件式的，不能把末尾 except 整体改成提交前失败。
    """
    delivery, models, runner, _ = _make_runner(tmp_path)

    async def boom_context_send(umo: str, message: object) -> None:
        raise RuntimeError("adapter died mid-send")

    runner._context_send = boom_context_send

    # 只让提交后那次日志抛（UNKNOWN 分支）；外层 except 自己的日志必须能正常
    # 执行，否则异常直接逃出 send_reply，测试观察不到返回值。
    #
    # 按日志模板锚定，而不是按「第一次调用」计数：计数法把断言钉在调用顺序上，
    # 将来有人在 send 之前新增一条 warning，就会打错位置，让这条测试静默变成
    # 「测的不是目标路径」的虚假绿灯。下方 fired 断言进一步保证锚点脱落时报红
    # 而不是无声通过。
    unknown_branch_marker = "context send result unknown"
    fired = {"n": 0}
    real_warning = delivery.logger.warning

    def boom_on_unknown_branch(*args: object, **kwargs: object) -> None:
        if args and isinstance(args[0], str) and unknown_branch_marker in args[0]:
            fired["n"] += 1
            raise RuntimeError("logger exploded after submit")
        real_warning(*args, **kwargs)

    monkeypatch.setattr(delivery.logger, "warning", boom_on_unknown_branch)

    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    assert fired["n"] == 1, (
        "UNKNOWN 分支的日志未被触发：本测试没有走到提交后失败那条路径，"
        f"断言无意义（日志模板 {unknown_branch_marker!r} 可能已改名）"
    )
    assert outcome.status is models.SendStatus.UNKNOWN, (
        f"提交后失败被降级为 {outcome.status!r}，会导致不消耗冷却而重发"
    )


async def test_send_escaping_from_gateway_after_adapter_call_stays_unknown(
    tmp_path: Path,
) -> None:
    """护栏：异常逃出 ``OutboundGateway.send`` 时必须仍记 UNKNOWN。

    gateway 内部虽把 adapter 异常转成 UNKNOWN，但它自身的 except 块或后续记账
    仍可能抛（历史形态：``str(exc)`` 二次抛出，已由 ``safe_exc_text`` 堵住；
    但"gateway 可能抛"这一通道本身是结构性的，任何新增的记账/日志分支都可能
    再引入）。此时 adapter 早已调用过，真实状态是「可能已提交」。

    若按「gateway 之后才算已提交」的直觉去写标志位，这里会翻转成
    FAILED_BEFORE_SUBMIT，不消耗冷却 → 后续触发重发 → 重复消息。

    故标志位必须在 ``await outbound.send`` **之前**置位：语义是「adapter 调用
    即将开始」，而非「gateway 已返回」。
    """
    _, models, runner, _ = _make_runner(tmp_path)
    delivery_mod = _delivery_module()

    class GatewayEscape(RuntimeError):
        pass

    async def boom_context_send(umo: str, message: object) -> None:
        raise RuntimeError("adapter called, then gateway escapes")

    runner._context_send = boom_context_send

    # 让 gateway 在 adapter 之后、返回之前抛：真实 gateway 先完成 send 调用，
    # 再抛，等价于「adapter 已调用过」这一前提成立时的逃出。
    real_gateway = delivery_mod.OutboundGateway

    class EscapingGateway(real_gateway):  # type: ignore[misc, valid-type]
        async def send(self, message, *, kind="reply"):
            await super().send(message, kind=kind)
            raise GatewayEscape("gateway escaped after adapter call")

    delivery_mod.OutboundGateway = EscapingGateway
    try:
        outcome = await runner.send_reply("s1", "你好", expected_generation=1)
    finally:
        delivery_mod.OutboundGateway = real_gateway

    assert outcome.status is models.SendStatus.UNKNOWN, (
        f"异常逃出 gateway 后被判为 {outcome.status!r}；adapter 已调用过，"
        "判成提交前失败会不消耗冷却而重发"
    )


async def test_event_send_escaping_from_gateway_stays_unknown(tmp_path: Path) -> None:
    """护栏：事件路径的异常逃出 gateway 时必须仍记 UNKNOWN。

    与上一条同源缺陷，只是发生在事件路径（``last_event.send``）。两条路径各自
    有独立的标志位与 ``except``，改一处不会连带另一处，故事件侧单列一条。
    """
    _, models, runner, last_events = _make_runner(tmp_path)

    async def boom_send(_message: object) -> None:
        raise RuntimeError("adapter called, then gateway escapes")

    event = FakeEvent()
    event.send = boom_send
    last_events["s1"] = event

    real_gateway = _delivery_module().OutboundGateway

    class EscapingGateway(real_gateway):  # type: ignore[misc, valid-type]
        async def send(self, message, *, kind="reply"):
            await super().send(message, kind=kind)
            raise RuntimeError("gateway escaped after adapter call")

    _delivery_module().OutboundGateway = EscapingGateway
    try:
        outcome = await runner.send_reply("s1", "你好", expected_generation=1)
    finally:
        _delivery_module().OutboundGateway = real_gateway

    assert outcome.status is models.SendStatus.UNKNOWN, (
        f"事件路径异常逃出 gateway 后被判为 {outcome.status!r}；adapter 已调用过，"
        "判成提交前失败会不消耗冷却而重发"
    )


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

    投递完成后 ``_clear_result`` 会把事件结果回收，事后取不到链，故在 ``send``
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


class _FlipGate:
    """前 true_times 次 is_current 返回 True，之后一律 False（代次翻转模拟）。"""

    def __init__(self, true_times: int) -> None:
        self.remaining = true_times

    def is_current(self, umo: str, generation: object) -> bool:
        if self.remaining > 0:
            self.remaining -= 1
            return True
        return False


class _ClearBoomEvent(FakeEvent):
    """宿主 clear_result 抛错的事件桩（回收失败不得阻断投递）。"""

    def clear_result(self) -> None:
        raise RuntimeError("clear_result broken")


class _ClearingHook:
    """装饰钩子：吃掉事件结果（直接置空，模拟钩子消费内容）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def __call__(self, event: object, event_type: object) -> None:
        self.calls.append((event, event_type))
        event._result = None


class _BoomHook:
    """装饰钩子直接抛错（发送尚未开始 → FAILED_BEFORE_SUBMIT）。"""

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


async def test_send_reply_hook_empty_result_and_clear_error(tmp_path: Path) -> None:
    """装饰钩子清空结果 → FAILED_BEFORE_SUBMIT；clear_result 宿主抛错被吞。"""
    _, models, runner, last_events = _make_runner(tmp_path, hook=_ClearingHook())
    last_events["s1"] = _ClearBoomEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.FAILED_BEFORE_SUBMIT
    assert "no result" in outcome.detail


async def test_send_reply_suppressed_after_decorating(tmp_path: Path) -> None:
    """装饰钩子后代次翻转 → SUPPRESSED（复核点 2）。"""
    _, models, runner, last_events = _make_runner(tmp_path)
    runner._gate = _FlipGate(true_times=1)
    last_events["s1"] = FakeEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=7)
    assert outcome.status is models.SendStatus.SUPPRESSED
    assert "after decorating" in outcome.detail


async def test_send_reply_suppressed_before_send(tmp_path: Path) -> None:
    """发送前一刻代次翻转 → SUPPRESSED（复核点 3）。"""
    _, models, runner, last_events = _make_runner(tmp_path)
    runner._gate = _FlipGate(true_times=2)
    last_events["s1"] = FakeEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=7)
    assert outcome.status is models.SendStatus.SUPPRESSED
    assert "before send" in outcome.detail


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


async def test_send_reply_decorating_hook_error_before_submit(tmp_path: Path) -> None:
    """装饰钩子抛错（发送未开始）→ FAILED_BEFORE_SUBMIT。"""
    _, models, runner, last_events = _make_runner(tmp_path, hook=_BoomHook())
    last_events["s1"] = FakeEvent()
    outcome = await runner.send_reply("s1", "hello", expected_generation=None)
    assert outcome.status is models.SendStatus.FAILED_BEFORE_SUBMIT


# ============================================================================
# send_reply：context 兜底路径
# ============================================================================


async def test_send_reply_context_path_stale_gate(tmp_path: Path) -> None:
    """无缓存事件走 context 兜底前代次翻转 → SUPPRESSED。"""
    _, models, runner, _ = _make_runner(tmp_path)
    # 入口复核消耗一次 True，context 兜底前的复核才撞到翻转
    runner._gate = _FlipGate(true_times=1)
    outcome = await runner.send_reply("s1", "hello", expected_generation=7)
    assert outcome.status is models.SendStatus.SUPPRESSED
    assert "before context send" in outcome.detail


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
    runner._gate = _FlipGate(true_times=1)
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
    """log_reply_content 开启：长短回复预览分支都走 DEBUG 记录。

    变异锚定：把 ``delivery.py`` 的 ``if self.settings.log_reply_content and reply:``
    改成 ``if False:``，本用例红，只断言返回值是假绿（返回值与预览分支无关），
    必须断言 DEBUG 记录里的预览文本本身。

    断言内容：长回复截断到 ``_LOG_REPLY_PREVIEW_CHARS`` 并**带省略号**（去掉省略号
    即红：截断后的长度相等，只有省略号能区分「截断」与「恰好这么长」），短回复
    原样记录不截断。
    """
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
