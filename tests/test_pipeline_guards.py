"""管线持锁复核与投递降级：被变异扫描确认为无守卫的路径。

这些路径删掉后全量套件仍全绿，而各自的后果都是用户可见的：判断期间代次变化
照样烧一次 LLM 且状态显示过期结论、排队等锁的协程拿着锁前的过期结论开跑、
没有事件可用时引用一个不存在的消息 ID、decision 违约时把「不该回」的消息静默发
出去、工具直发后同一条内容被发两遍、空生成产出一条成功的假消息。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from .host_stubs import FakeEvent, capture_logs, load_package

PACKAGE_NAME = "selfreply_pipeline_guards_test_package"


def _install() -> None:
    from .host_stubs import install_astrbot_stubs

    install_astrbot_stubs()


def _closing_tracker(coro):
    """``_track_critical_task`` 的最小替身：关掉协程并返回可 await 的空结果。

    早退路径的 finally 仍会调它；不关协程会留下一条从未运行的
    ``_finalize_ledger`` 协程对象并触发 RuntimeWarning。
    """

    async def _noop() -> None:
        return None

    coro.close()
    return _noop()


def _wire_pipeline(package, *, umo, decision, generation=None, delivery=None):
    """装配一个只接必要依赖的 ``SessionPipeline``。

    本文件所有用例都只走「持锁门卫 → 判断 → 代次复核」这一段，不碰账本封账，
    因此 ``_track_critical_task`` 统一用关协程的替身。各用例之间只有
    decision/generation/delivery 三处差异，抽成工厂避免改一次接线契约要同步
    改五处。
    """
    pipeline_mod = load_package(package, "session_pipeline")
    models = load_package(package, "models")
    gate_mod = load_package(package, "session_gate")
    state = models.SessionState()

    pipeline = object.__new__(pipeline_mod.SessionPipeline)
    pipeline._gate = gate_mod.SessionGate()
    pipeline._state_for = lambda _umo: state
    pipeline._decision = decision
    pipeline._generation = generation
    pipeline._delivery = delivery
    pipeline._last_decisions = {}
    pipeline._is_stopping = lambda: False
    pipeline._is_enabled = lambda: True
    pipeline._settings = models.Settings.from_config({"whitelist_sessions": [umo]})
    pipeline._last_events = {umo: object()}
    pipeline._track_critical_task = _closing_tracker
    return pipeline, models


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

    变异锚定：把该 ``==`` 改成 ``!=``，本用例红。
    """
    _install()
    umo = "qq:GroupMessage:12345"
    seen: dict[str, object] = {}

    class DupGeneration:
        async def generate(
            self, _umo, _state, *, expected_generation, ledger, force, silence_active_at, **_kwargs
        ):
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


async def test_degenerate_decision_dict_is_rejected_not_silently_sent(tmp_path) -> None:
    """decision 违约返回 dict（而不是把 should_reply=False 转成字符串）必须炸。

    缺陷形态：``_decide_session_reply`` 末尾的
    ``raise RuntimeError("decide() must convert should_reply=False into a string")``。
    判断模型明确说「不接话」时，契约要求 ``decide()`` 转成字符串返回；拿到 dict
    意味着被注入的实现退化——不拦这一道，会话会继续走到生成与发送，把一条模型
    判定「不该回」的消息发出去，且全程无日志。此前这道 raise **没有任何用例
    守护**：改坏它全量套件仍全绿，等于一条没人验证的声明。

    变异锚定：把该 ``raise`` 改成 ``return result``，本用例红。
    """
    _install()
    umo = "qq:GroupMessage:12345"

    class ExplodingGeneration:
        async def generate(self, *_args, **_kwargs):
            raise AssertionError("should_reply=False 的会话走到了生成：会被静默发送")

    pipeline, _models = _wire_pipeline(
        f"{PACKAGE_NAME}_degenerate_decide",
        umo=umo,
        decision=_reply_decision(should_reply=False, reason="模型说不接话"),
        generation=ExplodingGeneration(),
    )

    baseline = pipeline._gate.advance(umo)
    with pytest.raises(RuntimeError, match="should_reply=False"):
        await pipeline.check_session_locked(
            umo, trigger="message_delay", force=False, expected_generation=baseline
        )


async def test_stale_generation_after_decision_records_stale_reason(
    tmp_path, caplog: object
) -> None:
    """判断期间代次变化必须中止，且裁决登记成丢弃理由而不是模型的结论。

    缺陷形态：``_decide_session_reply`` 里判断之后的
    ``if not self._gate.is_current(...)``。判断是一次秒级 LLM 调用，期间用户发
    新消息会推进代次；删掉这道复核，过期会话照旧跑完整生成（白烧一次 LLM 与
    token），而 ``/selfreply status`` 的「最近裁决」显示模型说"该回"、实际什么都
    没发生——正是 112-113 行注释要防的反向误导。

    变异锚定：把该条件改成恒假，本用例红。
    """
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
    """排队等锁期间状态变化，必须由持锁复核挡住。

    缺陷形态：``check_session_locked`` 开头那道与锁前同一形状的门卫。
    ``check_session`` 先做预检再 ``async with lock``：排队等锁的协程在锁外被
    挂起，这期间插件可能被停用、白名单被改、代次被推进、事件被回收。等它拿到锁
    时锁前预检的结论已经过期——只有这一道复核能挡住。删掉后为注定作废的会话白
    跑一次判断+生成，排障时 ``/selfreply status`` 显示的是过期结论。

    变异锚定：把该门卫的 ``if guard is not None: return guard`` 改成恒假，
    本用例红。
    """
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


def test_quote_target_id_is_empty_without_an_event(tmp_path) -> None:
    """没有事件可用时引用目标必须降级成空串，不得编一个 ID。

    缺陷形态：``_quote_target_id`` 的 ``if last_event is None: return ""``。
    改成返回 "0" 之类，主动回复会带上一个指向不存在消息的引用 ID，平台侧要么
    报错要么引用到错误的消息。

    「事件被回收」不落在这里：冻结引用握在手里时照旧用该事件投递，见
    ``test_regressions.test_eviction_during_generation_still_delivers_and_writes_history``。

    变异锚定：把 ``return ""`` 改成 ``return "0"``，本用例红。
    """
    _install()
    umo = "qq:GroupMessage:12345"
    delivery_mod = load_package(f"{PACKAGE_NAME}_quote_target", "delivery")
    runner = object.__new__(delivery_mod.DeliveryRunner)

    assert runner._quote_target_id(None) == "", "没有事件可用却仍返回了引用 ID"

    # 对照组：有事件时取的是该事件的消息 ID
    event = FakeEvent(umo=umo)
    event.message_obj = SimpleNamespace(message_id="m-42")
    assert runner._quote_target_id(event) == "m-42"


async def test_empty_generation_short_circuits_before_delivery(tmp_path) -> None:
    """LLM 返回空文本且无工具直发时必须短路，不得走进投递路径。

    缺陷形态：``check_session_locked`` 的
    ``if not effective_reply and not direct_send_count: return "管线未生成内容。"``。
    删掉后空生成继续走到 ``deliver_reply("")``，实际发送被 ``if reply:`` 挡住，
    但返回的是"已发送"路径的文案与 ``chars=0`` 的 DEBUG 日志——排障时看到的是一条
    成功的假消息。

    变异锚定：把该条件改成恒假，本用例红。
    """
    _install()
    umo = "qq:GroupMessage:12345"

    class EmptyGeneration:
        async def generate(
            self, _umo, _state, *, expected_generation, ledger, force, silence_active_at, **_kwargs
        ):
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
    result = await pipeline.check_session_locked(
        umo, trigger="message_delay", force=False, expected_generation=baseline
    )
    assert result == "管线未生成内容。", f"空生成没有短路：{result}"


async def test_reply_targets_the_message_the_generation_read(tmp_path) -> None:
    """引用与 @ 的目标必须是生成实际读到的那条消息。

    缺陷形态：投递现读 ``_last_events``。判断 + 生成是两次模型调用（识图与判断
    模型默认就要十几秒），期间新消息会替换该表——回复内容依据的是最新消息，
    引用却指向判断前那条（或者反过来）。冻结点必须落在生成取数那一刻：早于
    判断阶段会用旧消息，晚于生成则又漂到生成期间的新消息。

    变异锚定：把冻结挪回检查入口、或去掉向投递下传的 ``target_event``，本用例红。
    """
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
    """没有代次记录的会话也必须绑到一个具体基线，不能不设防。

    缺陷形态：``if baseline: expected_generation = baseline``。``current()`` 对
    从未 advance 过的会话返回 0，真值判断把它当"未取到"而保持 ``None``——而
    ``is_current(umo, None)`` 恒真，于是判断期间到达的新消息（advance 到 1）
    不再让本轮失效，过期结论照发。这正是首次巡检（会话刚进白名单、还没收到
    消息推进代次）的默认形态。

    变异锚定：把基线恢复成真值判断，本用例红。
    """
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

    变异锚定：删掉该分支的 logger.info，本用例红。
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
