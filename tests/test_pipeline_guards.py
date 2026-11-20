"""会话检查的持锁复核、目标消息绑定与记账窗口。"""

from __future__ import annotations

import asyncio
import logging

import pytest

from .host_stubs import FakeEvent, capture_logs, load_package, with_plugin

PACKAGE_NAME = "selfreply_pipeline_guards_test_package"


def _install() -> None:
    from .host_stubs import install_astrbot_stubs

    install_astrbot_stubs()


def _closing_tracker(coro):
    """返回已完成 Task，避免收尾路径留下未执行协程。"""

    async def _noop() -> None:
        return None

    coro.close()
    return asyncio.create_task(_noop())


def _wire_pipeline(package, *, umo, decision, generation=None, delivery=None):
    """构造只带被测协作对象的管线；未使用的收尾协程默认关闭。"""
    pipeline_mod = load_package(package, "session_pipeline")
    models = load_package(package, "models")
    gate_mod = load_package(package, "session_gate")
    state = models.SessionState()

    return (
        pipeline_mod.SessionPipeline(
            state_for=lambda _umo: state,
            generation=generation,
            delivery=delivery,
            is_stopping=lambda: False,
            is_enabled=lambda: True,
            settings=models.Settings.from_config({"whitelist_sessions": [umo]}),
            gate=gate_mod.SessionGate(),
            decision=decision,
            last_events={umo: object()},
            last_decisions={},
            track_critical_task=_closing_tracker,
        ),
        models,
    )


def _reply_decision(should_reply: bool = True, reason: str = "该接话"):
    class _Decision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            return {"should_reply": should_reply, "reason": reason, "elapsed_sec": 0.1}

    return _Decision()


async def test_duplicate_final_text_after_tool_direct_send_is_suppressed(tmp_path) -> None:
    """工具直发后，结论文本与已发内容相同时必须抑制，不得同一条内容发两遍。

    缺陷形态：``check_session_locked`` 里
    ``if any(normalized_reply == collapse_whitespace(text) for text in
    ledger.direct_texts): effective_reply = ""``。反向（文本不同时照发）有守卫，
    「相同时要抑制」这一侧没有：Agent 既调了发送工具又产出同样的结论文本时，
    同一条内容会被发两遍（工具一次 + final_reply 一次）。
    """
    _install()
    umo = "qq:GroupMessage:12345"
    seen: dict[str, object] = {}

    class DupGeneration:
        async def generate(self, _umo, _state, *, expected_generation, ledger, force, **_kwargs):
            # 管线自己建账本并经 kwargs 传进来：工具直发必须记到**这一本**上，
            # 外挂一本 ledger 不会被抑制逻辑看到。
            attempt = ledger.reserve("tool_direct", "同一句话")
            ledger.mark_in_flight(attempt)
            ledger.seal()
            assert ledger.direct_texts == ("同一句话",)
            return "同一句话", ledger

    class RecordingDelivery:
        async def deliver_reply(self, _umo, _state, reply, direct_send_count, **_kwargs):
            seen["reply"] = reply
            seen["count"] = direct_send_count
            return "已发送"

    pipeline, _models = _wire_pipeline(
        f"{PACKAGE_NAME}_dup_suppress",
        umo=umo,
        decision=_reply_decision(),
        generation=DupGeneration(),
        delivery=RecordingDelivery(),
    )

    baseline = pipeline._gate.advance(umo)
    result = await pipeline.check_session_locked(
        umo, trigger="message_delay", force=False, expected_generation=baseline
    )

    assert result == "已发送", result
    assert seen["reply"] == "", "结论文本与工具直发相同，却又发了一遍"
    assert seen["count"] == 1


async def test_stale_generation_after_decision_records_stale_reason(
    tmp_path, caplog: object
) -> None:
    """判断期间代次变化后记录丢弃原因，避免 status 显示过期的接话结论。"""
    _install()
    umo = "qq:GroupMessage:12345"

    class AdvancingDecision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            # 判断这次 LLM 调用期间有新消息到达：推进代次
            pipeline._gate.advance(umo)
            return {"should_reply": True, "reason": "模型说该接话", "elapsed_sec": 0.1}

    pipeline, models = _wire_pipeline(
        f"{PACKAGE_NAME}_stale_after_decide", umo=umo, decision=AdvancingDecision()
    )

    baseline = pipeline._gate.advance(umo)
    pipeline_logger = logging.getLogger(type(pipeline).__module__)
    with capture_logs(caplog, pipeline_logger, logging.DEBUG):
        result = await pipeline.check_session_locked(
            umo, trigger="message_delay", force=False, expected_generation=baseline
        )

    assert result == models.STALE_TASK_MESSAGE, f"过期会话没有中止：{result}"
    recorded = pipeline._last_decisions.get(umo)
    assert recorded is not None, "过期会话未登记裁决"
    assert recorded["reason"] == models.STALE_TASK_MESSAGE, (
        f"裁决登记的是模型的结论而非丢弃理由：{recorded['reason']}"
    )
    assert recorded["should_reply"] is False


async def test_lock_wait_does_not_bypass_the_in_lock_guard(tmp_path) -> None:
    """等锁时配置或代次可能变化，取得锁后必须重新检查运行资格。"""
    _install()
    umo = "qq:GroupMessage:12345"

    class NoopDecision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            raise AssertionError("持锁复核没挡住：过期会话继续跑到了判断")

    pipeline, models = _wire_pipeline(
        f"{PACKAGE_NAME}_in_lock_guard", umo=umo, decision=NoopDecision()
    )

    # 锁前预检通过之后才推进代次：模拟"等锁期间有新消息到达"
    real_guard = pipeline.session_check_guard
    calls = {"n": 0}

    def guard(umo_, *, force, expected_generation):
        nonlocal real_guard
        result = real_guard(umo_, force=force, expected_generation=expected_generation)
        calls["n"] += 1
        if calls["n"] == 1:
            pipeline._gate.advance(umo_)
        return result

    pipeline.session_check_guard = guard

    baseline = pipeline._gate.advance(umo)
    result = await pipeline.check_session(
        umo, trigger="message_delay", force=False, expected_generation=baseline
    )
    assert result == models.STALE_TASK_MESSAGE, f"持锁复核没挡住过期会话：{result}"
    assert calls["n"] == 2, f"门卫调用次数异常：{calls['n']}"


async def test_empty_generation_short_circuits_before_delivery(tmp_path, caplog) -> None:
    """空生成且无工具直发时跳过投递，避免记录成成功发送。"""
    _install()
    umo = "qq:GroupMessage:12345"

    class EmptyGeneration:
        async def generate(self, _umo, _state, *, expected_generation, ledger, force, **_kwargs):
            return "", ledger

    class ExplodingDelivery:
        async def deliver_reply(self, *_args, **_kwargs):
            raise AssertionError("空生成走进了投递路径：会产出一条成功的假消息")

    pipeline, _models = _wire_pipeline(
        f"{PACKAGE_NAME}_empty_generation",
        umo=umo,
        decision=_reply_decision(),
        generation=EmptyGeneration(),
        delivery=ExplodingDelivery(),
    )

    baseline = pipeline._gate.advance(umo)
    with caplog.at_level(logging.WARNING):
        result = await pipeline.check_session_locked(
            umo, trigger="message_delay", force=False, expected_generation=baseline
        )
    assert result == "管线未生成内容。", f"空生成没有短路：{result}"
    # 这条 WARNING 是运营者唯一的可观测信号：面板显示已接话、群里什么都没发生时，
    # 没有它就只能看到一条静默失败的假成功。删掉日志本用例必须红。
    assert any(
        "generation produced no text" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), f"空生成没有留下可观测日志：{[r.getMessage() for r in caplog.records]}"


async def test_reply_targets_the_message_the_generation_read(tmp_path) -> None:
    """目标在生成输入读取时冻结，避免引用生成未依据过的新消息。"""
    _install()
    umo = "qq:GroupMessage:12345"
    before_check, after_decision, during_generation = object(), object(), object()
    seen: dict[str, object] = {}

    class SwappingDecision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            pipeline._last_events[umo] = after_decision  # 判断阶段到达的新消息
            return {"should_reply": True, "reason": "该接话", "elapsed_sec": 0.1}

    class AdvancingGeneration:
        async def generate(self, _umo, _state, **kwargs):
            seen["frozen"] = pipeline._last_events[umo]
            pipeline._last_events[umo] = during_generation  # 生成期间到达的新消息
            return "hi", kwargs["ledger"]

    class CapturingDelivery:
        async def deliver_reply(self, _umo, _state, reply, direct_send_count, **kwargs):
            seen["delivery_target"] = kwargs.get("target_event")
            return "已发送"

    pipeline, _models = _wire_pipeline(
        f"{PACKAGE_NAME}_pinned_target",
        umo=umo,
        decision=SwappingDecision(),
        generation=AdvancingGeneration(),
        delivery=CapturingDelivery(),
    )
    pipeline._last_events[umo] = before_check
    baseline = pipeline._gate.advance(umo)

    result = await pipeline.check_session_locked(
        umo, trigger="message_delay", force=False, expected_generation=baseline
    )

    assert result == "已发送", result
    assert seen["frozen"] is after_decision, "前提不成立：判断阶段没有换过事件"
    assert seen["delivery_target"] is after_decision, "引用目标不是生成读到的那条消息：冻结时机错位"


async def test_absent_generation_baseline_still_guards_against_advance(tmp_path) -> None:
    """代次 0 是有效基线，不能按假值视为未绑定。"""
    _install()
    umo = "qq:GroupMessage:12345"

    class AdvancingDecision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            pipeline._gate.advance(umo)
            return {"should_reply": True, "reason": "模型说该接话", "elapsed_sec": 0.1}

    pipeline, models = _wire_pipeline(
        f"{PACKAGE_NAME}_absent_baseline", umo=umo, decision=AdvancingDecision()
    )

    result = await pipeline.check_session_locked(umo, trigger="patrol", force=False)

    assert result == models.STALE_TASK_MESSAGE, f"无代次基线的会话没有被代次防护：{result}"


async def test_decision_rejection_is_logged_at_info(tmp_path, caplog: object) -> None:
    """模型明确说「不接话」必须在默认日志级别可见（契约 §9）。

    缺陷形态：``_decide_session_reply`` 里 ``isinstance(result, str)`` 的早退
    分支只登记裁决、不打日志，INFO 只挂在下面的 dict 分支上。而 ``decide()``
    把 ``should_reply=False`` 转成字符串返回，所以「机器人为什么不接话」这个
    最高频问题在默认配置下完全不可见——正是 §9 点名要保住的那一行。
    """
    _install()
    umo = "qq:GroupMessage:12345"

    class RefusingDecision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            return "判断不回复：用户正在连续测试"

    package = f"{PACKAGE_NAME}_refusal_info"
    pipeline, _models = _wire_pipeline(package, umo=umo, decision=RefusingDecision())
    pipeline_mod = load_package(package, "session_pipeline")

    with capture_logs(caplog, pipeline_mod.logger, logging.INFO):
        result = await pipeline.check_session_locked(umo, trigger="message_delay", force=False)

    assert result == "判断不回复：用户正在连续测试", f"拒绝没有原样返回：{result}"
    infos = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "decision" in record.getMessage()
    ]
    assert infos, "判断不回复在 INFO 级别不可见（契约 §9 要求默认可见）"
    assert "should_reply=False" in infos[0], f"INFO 行缺少结论字段：{infos[0]}"
    assert "用户正在连续测试" in infos[0], f"INFO 行缺少理由：{infos[0]}"


# 群友可控的恶意 reason：换行 + C0(ESC/BEL) + DEL + C1(CSI/NEL)
_DIRTY_REASON = "理由\n伪造\x1b[2J行\x07bell\x7fDEL\x9b31mC1\x85NEL"


def _real_decide_decision(package: str, *, should_reply: bool):
    """保留真实 decide 的清洗路径，只替换模型返回。"""
    decision_mod = load_package(package, "decision")
    maker = object.__new__(decision_mod.DecisionMaker)

    async def ask_model(_umo, _state, *, trigger):
        return {"should_reply": should_reply, "reason": _DIRTY_REASON, "elapsed_sec": 0.1}

    maker.ask_decision_model = ask_model

    class _Decision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, umo, state, *, trigger, force):
            return await maker.decide(umo, state, trigger=trigger, force=force)

    return _Decision()


@pytest.mark.parametrize("should_reply", [True, False])
async def test_reason_control_chars_never_reach_info_or_record(
    tmp_path, caplog: object, should_reply: bool
) -> None:
    """两类裁决均清除控制字符，防止 reason 伪造日志或污染 status。"""
    _install()
    umo = "qq:GroupMessage:12345"
    package = f"{PACKAGE_NAME}_reason_{should_reply}"
    pipeline, models = _wire_pipeline(
        package, umo=umo, decision=_real_decide_decision(package, should_reply=should_reply)
    )
    pipeline_mod = load_package(package, "session_pipeline")

    with capture_logs(caplog, pipeline_mod.logger, logging.INFO):
        result = await pipeline._decide_session_reply(
            umo,
            models.SessionState(),
            trigger="message_delay",
            force=False,
            expected_generation=None,
        )

    recorded = pipeline._last_decisions[umo]
    infos = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "decision" in record.getMessage()
    ]
    assert infos, f"裁决没有打 INFO（契约 §9）：{caplog.records}"
    for text in (recorded["reason"], *infos):
        assert "\n" not in text and "\r" not in text, f"未折单行: {text!r}"
        for char in ("\x1b", "\x07", "\x7f", "\x85", "\x9b"):
            assert char not in text, f"控制字符 {char!r} 进了 INFO/记录: {text!r}"
    assert isinstance(result, str) is (not should_reply)
    assert recorded["should_reply"] is should_reply


def test_committed_send_after_whitelist_readd_lands_on_live_state(tmp_path) -> None:
    """白名单重加会创建新状态；已提交发送必须记入当前状态以保留配额和冷却。"""
    _install()
    umo = "fake:group:123"
    reply_text = "已提交的回复"

    class BlockingEvent(FakeEvent):
        """send 停在半途，把「发送已提交」这一刻钉住。"""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()

        async def send(self, message):
            self.send_started.set()
            await self.release_send.wait()
            return await super().send(message)

    class FixedGeneration:
        async def generate(self, _umo, _state, *, ledger, **_kwargs):
            return reply_text, ledger

    async def scenario(plugin, _main):
        event = BlockingEvent(umo=umo, message_str="old")
        plugin._last_events[umo] = event
        plugin._last_event_at[umo] = 1.0
        old_state = plugin._state_for(umo)
        plugin._pipeline._generation = FixedGeneration()

        token = plugin._gate.advance(umo)
        task = asyncio.create_task(
            plugin._pipeline.check_session(
                umo,
                trigger="message_delay",
                force=True,
                expected_generation=token,
            )
        )
        # 消息已经交给平台，此刻的发送是「已提交」的
        await asyncio.wait_for(event.send_started.wait(), timeout=5)

        plugin._whitelist.replace(set())
        plugin._whitelist.replace({umo})
        live_state = plugin._state_for(umo)
        assert live_state is not old_state, "前置条件不成立：readd 未产生新的 SessionState"

        event.release_send.set()
        result = await asyncio.wait_for(task, timeout=5)
        assert result == "已主动回复。", f"在途检查结果异常：{result!r}"
        assert live_state.daily_count == 1, (
            "已提交的发送没有记进 live 会话状态：日配额与冷却未被消耗，"
            "下一次触发会把同一内容再发一次（契约 §1）"
        )
        assert live_state.recent, "live 会话状态没有留下这条 assistant 历史"
        assert (live_state.recent[-1].role, live_state.recent[-1].text) == (
            "assistant",
            reply_text,
        ), f"live 历史末条不是本次发送：{live_state.recent[-1]!r}"

    with_plugin(tmp_path, scenario)


async def test_lock_swap_while_waiting_keeps_single_check_per_session(tmp_path) -> None:
    """release 与 waiter 持锁之间可能发生 prune，旧锁的排队者必须重新取锁。"""
    _install()
    umo = "qq:GroupMessage:12345"

    pipeline, _models = _wire_pipeline(
        f"{PACKAGE_NAME}_gate_swap", umo=umo, decision=_reply_decision()
    )

    loop = asyncio.get_running_loop()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    depth = {"running": 0, "peak": 0}

    async def fake_locked(umo_, *, trigger, force, expected_generation=None):
        depth["running"] += 1
        depth["peak"] = max(depth["peak"], depth["running"])
        try:
            if trigger == "first":
                first_entered.set()
                await release_first.wait()
                # 排进「已释放、waiter 尚未真正持锁」的那一 tick：prune 只有在这
                # 一 tick 里才会看到一把未锁但有排队者的锁。
                loop.call_soon(pipeline._gate.prune, umo)
            else:
                await asyncio.sleep(0.05)
        finally:
            depth["running"] -= 1
        return f"{trigger} done"

    pipeline.check_session_locked = fake_locked

    old_lock = pipeline._gate.lock_for(umo)
    first = asyncio.create_task(pipeline.check_session(umo, trigger="first", force=False))
    await asyncio.wait_for(first_entered.wait(), timeout=5)

    second = asyncio.create_task(pipeline.check_session(umo, trigger="second", force=False))
    for _ in range(3):
        await asyncio.sleep(0)  # 让 second 真正挂到该锁的等待队列上

    release_first.set()
    for _ in range(3):
        await asyncio.sleep(0)  # 让 prune 换锁、second 拿到旧锁
    assert pipeline._gate.lock_for(umo) is not old_lock, (
        "前置条件不成立：prune 没有在「已释放、waiter 未持锁」的窗口内换掉锁表条目"
    )

    third = asyncio.create_task(pipeline.check_session(umo, trigger="third", force=False))
    results = await asyncio.wait_for(asyncio.gather(first, second, third), timeout=5)

    assert depth["peak"] == 1, (
        f"同一会话跑出了并发检查（峰值 {depth['peak']}）：prune 换锁后排队者持旧锁、"
        f"新检查持新锁，串行性失效"
    )
    assert results == ["first done", "second done", "third done"], f"检查结果异常：{results}"


async def test_double_cancel_keeps_running_mark_until_record_task_ends(tmp_path) -> None:
    """外层检查已结束时记账仍可能在途，运行租约必须覆盖记账窗口。"""
    _install()
    umo = "qq:GroupMessage:12345"
    package = f"{PACKAGE_NAME}_record_window"
    models = load_package(package, "models")

    class CountingDecision:
        def __init__(self) -> None:
            self.decide_calls = 0

        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            self.decide_calls += 1
            return {"should_reply": True, "reason": "该接话", "elapsed_sec": 0.1}

    class CountingGeneration:
        def __init__(self) -> None:
            self.generate_calls = 0

        async def generate(self, _umo, _state, *, ledger, **_kwargs):
            self.generate_calls += 1
            attempt = ledger.reserve("final_reply", "在呢")
            ledger.mark_in_flight(attempt)
            ledger.resolve(attempt, models.SendStatus.DELIVERED)
            return "在呢", ledger

    class BlockingRecordDelivery:
        def __init__(self) -> None:
            self.record_started = asyncio.Event()
            self.release_record = asyncio.Event()
            self.persist_calls = 0

        def apply_proactive_state(self, _umo, state, reply, _direct_count, **_kwargs) -> None:
            state.record_proactive_attempt(confirmed=True, text=reply, at=1.0)

        async def persist_proactive_state(self) -> bool:
            self.persist_calls += 1
            # 只卡住第一条记账：第二次检查若真的进来，必须让它自己跑完被断言抓住，
            # 不能把用例挂在测试自己的等待上。
            if self.persist_calls == 1:
                self.record_started.set()
                await self.release_record.wait()
            return True

        async def deliver_reply(self, *_args, **_kwargs):
            return "已发送"

    decision = CountingDecision()
    delivery = BlockingRecordDelivery()
    pipeline, _models = _wire_pipeline(
        package,
        umo=umo,
        decision=decision,
        generation=CountingGeneration(),
        delivery=delivery,
    )
    finalizers: list[asyncio.Task[object]] = []

    def track(coro):
        task = asyncio.create_task(coro)
        finalizers.append(task)
        return task

    pipeline._track_critical_task = track

    baseline = pipeline._gate.advance(umo)
    first = asyncio.create_task(
        pipeline.check_session(
            umo, trigger="message_delay", force=False, expected_generation=baseline
        )
    )
    await asyncio.wait_for(delivery.record_started.wait(), timeout=5)

    first.cancel()
    await asyncio.sleep(0)  # 第一次取消落进 shield 的 except，检查转入补等
    assert not first.done(), "第一次取消只是结束外层等待，检查必须仍在等记账任务"
    first.cancel()  # 关停窗口里的第二次取消落在「补等」上
    with pytest.raises(asyncio.CancelledError):
        await first

    try:
        assert pipeline._gate.is_running(umo), (
            "记账任务仍在跑，运行标记却被提前释放：同 umo 的第二次检查会与它并发"
        )
        second = await pipeline.check_session(
            umo, trigger="message_delay", force=True, expected_generation=baseline
        )
        assert second == "已有判断任务在运行。", (
            f"第二次检查在第一条 record task 结束前进入了主链：{second}"
        )
        assert decision.decide_calls == 1, "第二次检查又跑了一次判断"
        assert delivery.persist_calls == 1, "同一 umo 出现了第二条 record task 的落盘"
    finally:
        delivery.release_record.set()
        await asyncio.gather(*finalizers, return_exceptions=True)
    await asyncio.sleep(0)  # 释放回调在记账任务结束后的下一 tick 执行
    assert not pipeline._gate.is_running(umo), (
        "记账任务结束后运行标记没有释放：会话永久卡在 running"
    )


async def test_finalizer_registration_failure_still_releases_running_mark() -> None:
    """收尾任务登记失败也要释放运行标记，避免会话永久被拒绝。"""
    _install()
    umo = "qq:GroupMessage:12345"
    package = f"{PACKAGE_NAME}_tracker_failure"
    models = load_package(package, "models")

    class Decision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            return {"should_reply": True, "reason": "该接话", "elapsed_sec": 0.1}

    class Generation:
        async def generate(self, _umo, _state, *, ledger, **_kwargs):
            attempt = ledger.reserve("final_reply", "在呢")
            ledger.mark_in_flight(attempt)
            ledger.resolve(attempt, models.SendStatus.DELIVERED)
            return "在呢", ledger

    class Delivery:
        def apply_proactive_state(self, *_args, **_kwargs) -> None:
            raise AssertionError("登记失败时不应走到记账")

        async def persist_proactive_state(self) -> bool:
            raise AssertionError("登记失败时不应走到落盘")

        async def deliver_reply(self, *_args, **_kwargs):
            return "已发送"

    pipeline, _models = _wire_pipeline(
        package, umo=umo, decision=Decision(), generation=Generation(), delivery=Delivery()
    )

    def broken_tracker(coro):
        coro.close()  # 不关会留下未 await 的协程告警
        raise RuntimeError("task registry unavailable")

    pipeline._track_critical_task = broken_tracker
    baseline = pipeline._gate.advance(umo)

    with pytest.raises(RuntimeError, match="task registry unavailable"):
        await pipeline.check_session(
            umo, trigger="message_delay", force=True, expected_generation=baseline
        )

    assert not pipeline._gate.is_running(umo), (
        "finalizer 登记失败后运行标记没有释放：该会话此后所有检查都会被 busy 拒绝"
    )


async def test_old_finalizer_does_not_unmark_readded_session_owner() -> None:
    """remove→readd 后，旧 finalizer 不得清掉新检查的运行租约。

    二次取消时旧 finalizer 暂代检查任务持有运行标记。移出再加回白名单后，新检查
    替换 owner；若旧 finalizer 的回调无条件 unmark，会清掉新检查的标记并允许并发。
    """
    _install()
    umo = "qq:GroupMessage:12345"
    package = f"{PACKAGE_NAME}_restore_during_record"
    models = load_package(package, "models")

    class Decision:
        def local_gate(self, _state, *, force):
            return ""

        async def decide(self, _umo, _state, *, trigger, force):
            return {"should_reply": True, "reason": "该接话", "elapsed_sec": 0.1}

    class Generation:
        async def generate(self, _umo, _state, *, ledger, **_kwargs):
            attempt = ledger.reserve("final_reply", "在呢")
            ledger.mark_in_flight(attempt)
            ledger.resolve(attempt, models.SendStatus.DELIVERED)
            return "在呢", ledger

    class BlockingRecordDelivery:
        def __init__(self) -> None:
            self.record_started = asyncio.Event()
            self.release_record = asyncio.Event()

        def apply_proactive_state(self, _umo, state, reply, _direct_count, **_kwargs) -> None:
            state.record_proactive_attempt(confirmed=True, text=reply, at=1.0)

        async def persist_proactive_state(self) -> bool:
            self.record_started.set()
            await self.release_record.wait()
            return True

        async def deliver_reply(self, *_args, **_kwargs):
            return "已发送"

    delivery = BlockingRecordDelivery()
    pipeline, _models = _wire_pipeline(
        package, umo=umo, decision=Decision(), generation=Generation(), delivery=delivery
    )
    finalizers: list[asyncio.Task[object]] = []

    def track(coro):
        task = asyncio.create_task(coro)
        finalizers.append(task)
        return task

    pipeline._track_critical_task = track
    gate = pipeline._gate
    baseline = gate.advance(umo)

    first = asyncio.create_task(
        pipeline.check_session(
            umo, trigger="message_delay", force=False, expected_generation=baseline
        )
    )
    await asyncio.wait_for(delivery.record_started.wait(), timeout=5)
    first.cancel()
    await asyncio.sleep(0)  # 第一次取消落进 shield 的 except，检查转入补等
    first.cancel()  # 关停窗口里的第二次取消落在「补等」上
    with pytest.raises(asyncio.CancelledError):
        await first

    try:
        # 前置条件：租约的初始 owner 已 done，记账任务仍在 persist 里挂着。
        assert first.done(), "前置条件不成立：外层检查任务必须已结束"
        assert not finalizers[0].done(), "前置条件不成立：记账任务必须仍在跑"

        # remove→readd 后的新检查取得运行租约。旧 finalizer 收尾时不能清掉新租约。
        gate.prune(umo)
        gate.mark_running(umo)
        new_owner = gate._running_owners[umo]
        assert new_owner is asyncio.current_task()

        assert gate.is_running(umo), "remove→readd 后的新检查应保留运行标记"
    finally:
        delivery.release_record.set()
        await asyncio.gather(*finalizers, return_exceptions=True)
    await asyncio.sleep(0)  # 释放回调在记账任务结束后的下一 tick 执行
    assert gate.is_running(umo), "旧 finalizer 清掉了新检查的运行标记"
    assert gate._running_owners[umo] is new_owner, "旧 finalizer 覆盖了新检查的 owner"
    gate.unmark_running(umo)
