"""DeliveryRunner 独立单测（ticket 05 验收）：注入假门卫/发送器/钩子，脱离插件实例。

覆盖验收项：
- UNKNOWN 投递不自动重试、不触发 after-send 钩子、消耗冷却与日配额并推进观察窗口
- 发送成功后代次未变则观察窗口必推进；代次已变则跳过记录（不推进观察窗口）
- 工具直发与文本回复的混合出口（直发无文本/纯文本/两者都有）语义不变
- 发送前门卫拦截时：有直发则仍记录并提示，无直发则纯跳过不记录
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

from .host_stubs import FakeEvent
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

    async def __call__(self, umo: str, reply: str, expected_generation: object) -> object:
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
    save: FakeSave | None = None,
    gate_current: bool = True,
    local_gate: str = "",
    config: dict | None = None,
):
    from . import host_stubs

    host_stubs.install_astrbot_stubs()  # delivery 顶层导入宿主私有符号
    _, _, models = _load_modules()
    delivery_mod = _delivery_module()
    settings = models.Settings.from_config(config or {})
    last_events: dict[str, object] = {}
    gate = SimpleNamespace(is_current=lambda umo, generation: gate_current)
    if sender is None:
        if sender_status is None:
            outcome = models.SendOutcome(models.SendStatus.DELIVERED, "")
        else:
            outcome = models.SendOutcome(models.SendStatus(sender_status), "")
        sender = FakeSender(outcome)
    delivered = delivery_mod.DeliveryRunner(
        settings=settings,
        gate=gate,
        local_gate=lambda state, force: local_gate,
        last_events=last_events,
        call_hook=hook if hook is not None else FakeHook(),
        context_send=context_send if context_send is not None else FakeContextSend(),
        send_reply=sender,
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
    )
    return delivery_mod, models, delivered, last_events


def _state(models, *, observed_at: float = 50.0, active_at: float = 100.0):
    state = models.SessionState()
    state.last_active_at = active_at
    state.last_proactive_observed_at = observed_at
    return state


def _hook_names(hook: FakeHook) -> list[str]:
    return [event_type.name for _event, event_type in hook.calls]


# ============================================================================
# 验收项 1：UNKNOWN 不自动重试、不触发 after-send 钩子、消耗状态并推进观察窗口
# ============================================================================


async def test_deliver_unknown_consumes_state_without_retry(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path, sender_status="unknown")
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert "未自动重试" in result
    assert state.daily_count == 1
    assert state.last_proactive_at > 0
    assert state.last_proactive_observed_at == 100.0  # 视为已尝试，推进观察窗口
    assert state.last_proactive_text == ""  # 不写确认文本
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
    ok = await runner.record_proactive_state(
        "s1", state, "", 0, observed_active_at=100.0, confirmed=False
    )
    assert ok is True
    assert state.daily_count == 1
    assert state.last_proactive_observed_at == 100.0
    assert all(record.role != "assistant" for record in state.recent)


# ============================================================================
# 验收项 2：观察窗口推进语义（代次未变必推进；代次已变跳过记录）
# ============================================================================


async def test_deliver_delivered_advances_observation_and_history(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert result == "已主动回复。"
    assert state.last_proactive_observed_at == 100.0
    assert state.last_proactive_text == "你好"
    assert state.recent[-1].role == "assistant"
    assert state.daily_count == 1


async def test_record_stale_generation_skips_observation_advance(tmp_path: Path) -> None:
    """代次已变：冷却仍记录，但观察窗口不得推进（避免覆盖新会话语义）。"""
    _, models, runner, _ = _make_runner(tmp_path, gate_current=False)
    state = _state(models)
    ok = await runner.record_proactive_state(
        "s1", state, "你好", 0, expected_generation=999, observed_active_at=200.0
    )
    assert ok is True
    assert state.last_proactive_at > 0  # 冷却与配额仍消耗
    assert state.daily_count == 1
    assert state.last_proactive_observed_at == 50.0  # 观察窗口未推进


async def test_record_save_failure_returns_false(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path, save=FakeSave(fail=True))
    state = _state(models)
    ok = await runner.record_proactive_state("s1", state, "你好", 0)
    assert ok is False
    assert state.daily_count == 1  # 内存状态仍记录，仅持久化失败


async def test_record_proactive_state_flows_through_debounced_saver(tmp_path: Path) -> None:
    """合并写契约：record_proactive_state 只置脏，flush 才真正落盘。

    0.8.8 前 delivery 层测试注入的是直接可 await 的 save，掩盖了生产注入
    （异步闭包直连 DebouncedStateSaver.mark_dirty）"置脏 ≠ 同步写"
    的语义；本测试用真实合并器锁定契约。
    """
    writes: list[str] = []
    saver_mod = importlib.import_module(f"{PACKAGE_NAME}.state_saver")

    async def do_save() -> None:
        writes.append("save")

    saver = saver_mod.DebouncedStateSaver(do_save=do_save, debounce_sec=60.0)

    async def save_like_production() -> None:
        # 与生产注入同语义（0.9.0 C' 后为异步闭包直连）：async 包装 + 置脏
        saver.mark_dirty()

    _, models, runner, _ = _make_runner(tmp_path, save=save_like_production)
    state = _state(models)

    ok = await runner.record_proactive_state("s1", state, "你好", 0)
    assert ok is True
    assert writes == [], "合并写：置脏后不得立即落盘"
    assert saver.pending is True

    await saver.flush()
    assert writes == ["save"], "flush 后最终落盘"
    assert saver.pending is False

    # 窗口内第二条记录：不重复落盘，flush 后合并为一次
    await runner.record_proactive_state("s1", state, "第二条", 0)
    assert writes == ["save"], "窗口内重复置脏不重复落盘"
    await saver.flush()
    assert writes == ["save", "save"]
    saver.cancel()


# ============================================================================
# 验收项 3：混合出口三分支（直发无文本 / 纯文本 / 两者都有）
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
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert result == "已通过工具主动回复。"
    assert state.daily_count == 1
    assert state.last_proactive_text == "[工具主动发送 x2]"
    assert state.last_proactive_observed_at == 100.0


async def test_deliver_text_only_sends_once(tmp_path: Path) -> None:
    """纯文本：发送一次文本回复。"""
    _, models, runner, _ = _make_runner(tmp_path)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert result == "已主动回复。"
    assert state.last_proactive_text == "你好"


async def test_deliver_both_text_and_directs(tmp_path: Path) -> None:
    """两者都有：以文本回复为主（不返回"已通过工具"）。"""
    _, models, runner, _ = _make_runner(tmp_path)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "补充文本",
        3,
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert result == "已主动回复。"
    assert state.last_proactive_text == "补充文本"


async def test_deliver_failed_before_submit_no_directs_no_record(tmp_path: Path) -> None:
    """FAILED_BEFORE_SUBMIT 且无直发：不得消耗配额（未提交）。"""
    _, models, runner, _ = _make_runner(tmp_path, sender_status="failed_before_submit")
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        expected_generation=1,
        observed_active_at=100.0,
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
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert result == "会话已更新，放弃旧回复。"
    assert state.daily_count == 1  # 直发发生了，消耗配额


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
        expected_generation=999,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert "工具主动回复已完成" in result
    assert "会话已经更新" in result
    assert state.daily_count == 1


async def test_deliver_gate_block_without_directs_no_record(tmp_path: Path) -> None:
    _, models, runner, _ = _make_runner(tmp_path, gate_current=False)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "你好",
        0,
        expected_generation=999,
        observed_active_at=100.0,
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
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert "工具主动回复已完成；冷却中。" == result
    assert state.daily_count == 1


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
