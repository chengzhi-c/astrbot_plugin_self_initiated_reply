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
    if sender is None and sender_status is not None:
        outcome = models.SendOutcome(models.SendStatus(sender_status), "")
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


async def test_record_proactive_state_persists_every_record(tmp_path: Path) -> None:
    """落盘契约：每条记录调用返回时状态已持久化，无延迟窗口。

    0.9.3 删除 DebouncedStateSaver 后，注入的回调即 ``_save_storage``
    本体（串行锁 + to_thread 原子写）。本测试锁定"记录即落盘"：
    崩溃窗口为零，不存在"已发送但状态未落盘"的中间态。
    """
    writes: list[str] = []

    async def save() -> None:
        writes.append("save")

    _, models, runner, _ = _make_runner(tmp_path, save=save)
    state = _state(models)

    ok = await runner.record_proactive_state("s1", state, "你好", 0)
    assert ok is True
    assert writes == ["save"], "记录即落盘，不得延迟"

    await runner.record_proactive_state("s1", state, "第二条", 0)
    assert writes == ["save", "save"], "每条记录各自落盘"


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
    # confirmed 语义守卫（0.9.0 低垂果实复审）：非 UNKNOWN 路径的直发记录
    # 必须写历史条目与文本；若误用 confirmed=False 则既无文本也无 assistant 条目
    assert state.last_proactive_text == "[工具主动发送 x1]"
    assert [item.role for item in state.recent] == ["assistant"]


async def test_deliver_send_failure_with_directs_records_confirmed(tmp_path: Path) -> None:
    """发送确定失败（非 UNKNOWN）且有直发：记录走 confirmed 语义并写历史。"""
    _, models, runner, _ = _make_runner(tmp_path, sender_status="failed_before_submit")
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "正文",
        2,
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="message_delay",
    )

    assert result == "主动发送失败。"
    assert state.daily_count == 1
    assert state.last_proactive_text == "[工具主动发送 x2]"
    assert [item.role for item in state.recent] == ["assistant"]


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
    被触及。返回 UNKNOWN 会经 ``record_proactive_state(confirmed=False)`` 消耗
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
    UNKNOWN —— 降级成 FAILED_BEFORE_SUBMIT 会让插件不消耗冷却而重发，制造重复
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

    ``outbound.py`` 的 ``except Exception`` 用 ``str(exc)`` 构造 ``SendOutcome``。
    若 adapter 抛出的异常对象自身 ``__str__`` 坏掉，这次 ``str()`` 会在 except
    块内二次抛出，不再被同一 try 捕获，于是异常**逃出 gateway**。

    这条通道的真实状态是「adapter 已调用过，可能已提交」，必须保守记 UNKNOWN。
    若按「gateway 之后才算已提交」的直觉去写标志位，这里会翻转成
    FAILED_BEFORE_SUBMIT —— 不消耗冷却 → 后续触发重发 → 重复消息。

    故标志位必须在 ``await outbound.send`` **之前**置位：语义是「adapter 调用
    即将开始」，而非「gateway 已返回」。
    """
    _, models, runner, _ = _make_runner(tmp_path)

    class UnstringableError(RuntimeError):
        def __str__(self) -> str:
            raise ValueError("__str__ is broken")

    async def boom_context_send(umo: str, message: object) -> None:
        raise UnstringableError

    runner._context_send = boom_context_send

    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    # 路径锚定：detail 必须来自坏 __str__ 抛出的那个 ValueError，证明异常确实是
    # 从 gateway 内部逃出的，而不是走了别的失败通道后碰巧也返回 UNKNOWN。
    assert outcome.detail == "__str__ is broken", (
        f"detail={outcome.detail!r}，未走「异常逃出 gateway」通道，断言无意义"
    )
    assert outcome.status is models.SendStatus.UNKNOWN, (
        f"异常逃出 gateway 后被判为 {outcome.status!r}；adapter 已调用过，"
        "判成提交前失败会不消耗冷却而重发"
    )


async def test_event_send_escaping_from_gateway_stays_unknown(tmp_path: Path) -> None:
    """护栏：事件路径的异常逃出 gateway 时必须仍记 UNKNOWN。

    与上一条同源缺陷，只是发生在事件路径（``last_event.send``）。两条路径各自
    有独立的标志位与 ``except``，改一处不会连带另一处——本条测试专门守事件侧，
    否则事件路径的悲观默认会成为无回归网的裸改动（实测：删掉它，全量测试仍全绿）。
    """
    _, models, runner, last_events = _make_runner(tmp_path)

    class UnstringableError(RuntimeError):
        def __str__(self) -> str:
            raise ValueError("__str__ is broken")

    async def boom_send(_message: object) -> None:
        raise UnstringableError

    event = FakeEvent()
    event.send = boom_send
    last_events["s1"] = event

    outcome = await runner.send_reply("s1", "你好", expected_generation=1)

    # 路径锚定，同 context 侧那条：detail 必须来自坏 __str__ 抛出的 ValueError。
    assert outcome.detail == "__str__ is broken", (
        f"detail={outcome.detail!r}，未走「异常逃出 gateway」通道，断言无意义"
    )
    assert outcome.status is models.SendStatus.UNKNOWN, (
        f"事件路径异常逃出 gateway 后被判为 {outcome.status!r}；adapter 已调用过，"
        "判成提交前失败会不消耗冷却而重发"
    )
