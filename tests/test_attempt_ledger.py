"""Attempt-ledger regression tests for outbound evidence retention."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

import pytest

from .host_stubs import capture_logs, install_astrbot_stubs, load_package

PACKAGE_NAME = "selfreply_attempt_ledger_test_package"


def _models():
    install_astrbot_stubs()
    return load_package(PACKAGE_NAME, "models")


def test_each_ledger_has_explicit_unique_id() -> None:
    """每次 logical attempt 都有可记录且不依赖全局计数器的身份。"""
    models = _models()
    first = models.AttemptLedger()
    second = models.AttemptLedger()

    assert isinstance(first.ledger_id, str)
    assert UUID(hex=first.ledger_id).version == 4
    assert first.ledger_id != second.ledger_id


@pytest.mark.parametrize("late_status", ["DELIVERED", "FAILED_BEFORE_SUBMIT"])
def test_seal_marks_inflight_attempt_unknown_and_ignores_late_delivery(
    late_status: str,
) -> None:
    """Seal freezes evidence: any late adapter outcome leaves the pessimistic UNKNOWN."""
    models = _models()
    ledger = models.AttemptLedger()

    attempt = ledger.reserve("tool_direct", "tool result")
    ledger.mark_in_flight(attempt)
    sealed = ledger.seal()

    assert sealed[0].state is models.AttemptState.UNKNOWN
    assert ledger.phase == "sealed"
    assert ledger.resolve(attempt, getattr(models.SendStatus, late_status)) is False
    assert attempt.state is models.AttemptState.UNKNOWN
    assert ledger.has_submission is True
    assert ledger.has_unknown is True
    # 冻结后不得改写任何派生的额度/去重证据
    assert ledger.direct_send_count == 1
    assert ledger.direct_texts == ("tool result",)
    with pytest.raises(RuntimeError):
        ledger.reserve("final_reply", "reply")


def test_seal_marks_unstarted_attempt_abandoned_without_submission() -> None:
    """A pre-send rejection is diagnostic evidence, never quota evidence."""
    models = _models()
    ledger = models.AttemptLedger()

    attempt = ledger.reserve("final_reply", "reply")
    ledger.seal()

    assert attempt.state is models.AttemptState.ABANDONED
    assert ledger.has_submission is False
    assert ledger.has_unknown is False


def test_recording_phase_has_one_task_and_failure_is_terminal() -> None:
    """A failed persistence run cannot create another quota-recording task."""
    models = _models()
    ledger = models.AttemptLedger()
    ledger.seal()
    task = object()

    assert ledger.start_recording(task) is True
    assert ledger.record_task is task
    assert ledger.start_recording(object()) is False

    ledger.mark_record_failed("disk unavailable")

    assert ledger.phase == "record_failed"
    assert ledger.record_failure == "disk unavailable"
    assert ledger.start_recording(object()) is False


async def test_gate_blocked_check_records_decision_and_logs(tmp_path: Path, caplog: object) -> None:
    """网关拦截更新最近裁决，避免 status 显示旧结论；高频闸门日志使用 DEBUG。"""
    install_astrbot_stubs()
    package = f"{PACKAGE_NAME}_gate_log"
    pipeline_mod = load_package(package, "session_pipeline")
    models = load_package(package, "models")
    gate_mod = load_package(package, "session_gate")

    umo = "qq:GroupMessage:12345"
    state = models.SessionState()
    state.last_proactive_at = 1.0
    state.daily_count = 1

    class FakeDecision:
        def local_gate(self, _state, *, force):
            assert force is False
            return "冷却中：还剩 14m59s。"

    pipeline = object.__new__(pipeline_mod.SessionPipeline)
    pipeline._gate = gate_mod.SessionGate()
    pipeline._state_for = lambda _umo: state
    pipeline._decision = FakeDecision()
    pipeline._last_decisions = {}
    pipeline._is_stopping = lambda: False
    pipeline._is_enabled = lambda: True
    pipeline._settings = models.Settings.from_config({"whitelist_sessions": [umo]})
    pipeline._last_events = {umo: object()}

    reason = "冷却中：还剩 14m59s。"
    with capture_logs(caplog, pipeline_mod.logger, logging.DEBUG):
        result = await pipeline.check_session_locked(umo, trigger="message_delay", force=False)

    assert result == reason
    recorded = pipeline._last_decisions.get(umo)
    assert recorded is not None, "网关拦截未登记裁决：/selfreply status 会显示过期的旧裁决"
    assert recorded["should_reply"] is False
    assert recorded["reason"] == reason

    lines = [r for r in caplog.records if "gate session=" in r.getMessage()]
    assert lines, "网关拦截没有任何日志"
    assert all(r.levelno == logging.DEBUG for r in lines), (
        f"网关拦截日志落在 {lines[0].levelname}：高频跳过原因属于 DEBUG，"
        "而冷却/配额拦截正是最常见的路径"
    )
    assert reason in lines[0].getMessage()


async def test_pipeline_record_retry_does_not_apply_state_twice() -> None:
    """The pipeline retry loop repeats persistence, never quota/history mutation."""
    install_astrbot_stubs()
    package_name = f"{PACKAGE_NAME}_pipeline"
    pipeline_mod = load_package(package_name, "session_pipeline")
    models = load_package(package_name, "models")

    class FakeDelivery:
        def __init__(self) -> None:
            self.apply_calls = 0
            self.persist_calls = 0

        def apply_proactive_state(self, _umo, state, reply, _direct_count, **_kwargs) -> None:
            self.apply_calls += 1
            state.record_proactive_attempt(confirmed=True, text=reply, at=1.0)

        async def persist_proactive_state(self) -> bool:
            self.persist_calls += 1
            return self.persist_calls == 2

    pipeline = object.__new__(pipeline_mod.SessionPipeline)
    delivery = FakeDelivery()
    pipeline._delivery = delivery
    # _record_ledger 按白名单重解析记账目标（契约 §1）；空白名单即「不迁移」。
    pipeline._settings = models.Settings.from_config({})
    pipeline._state_for = lambda _umo: models.SessionState()
    state = models.SessionState()
    ledger = models.AttemptLedger()
    attempt = ledger.reserve("final_reply")
    ledger.mark_in_flight(attempt)
    ledger.resolve(attempt, models.SendStatus.DELIVERED)
    ledger.seal()
    ledger.start_recording(object())

    await pipeline._record_ledger(
        "s1",
        state,
        ledger,
        "reply",
        expected_generation=None,
        observed_active_at=1.0,
    )

    # 结论单源在账本状态（_record_ledger 不再返回 bool）
    assert ledger.phase == "recorded"
    assert delivery.apply_calls == 1
    assert delivery.persist_calls == 2
    assert state.daily_count == 1
    assert len(state.recent) == 1


def _pipeline_with_delivery(package_name: str, delivery: object):
    install_astrbot_stubs()
    pipeline_mod = load_package(package_name, "session_pipeline")
    models = load_package(package_name, "models")
    pipeline = object.__new__(pipeline_mod.SessionPipeline)
    pipeline._delivery = delivery
    # _record_ledger 会按白名单重解析记账目标（契约 §1），替身必须给出这两个依赖：
    # 空白名单即「不迁移」，与这些用例的意图一致。
    pipeline._settings = models.Settings.from_config({})
    pipeline._state_for = lambda _umo: models.SessionState()
    return pipeline, models


def _delivered_sealed_ledger(models):
    ledger = models.AttemptLedger()
    attempt = ledger.reserve("final_reply")
    ledger.mark_in_flight(attempt)
    ledger.resolve(attempt, models.SendStatus.DELIVERED)
    ledger.seal()
    return ledger


class _CountingDelivery:
    def __init__(self, persist_ok_after: int = 0) -> None:
        self.apply_calls = 0
        self.persist_calls = 0
        self._persist_ok_after = persist_ok_after

    def apply_proactive_state(self, _umo, state, reply, _direct_count, **_kwargs) -> None:
        self.apply_calls += 1
        state.record_proactive_attempt(confirmed=True, text=reply, at=1.0)

    async def persist_proactive_state(self) -> bool:
        self.persist_calls += 1
        return self._persist_ok_after != 0 and self.persist_calls >= self._persist_ok_after


async def test_pipeline_persist_exhaustion_marks_record_failed_next_run_independent() -> None:
    """persist 耗尽 → record_failed；同账本不能再挂 record task，下一轮投递不受污染。"""
    delivery = _CountingDelivery(persist_ok_after=0)
    pipeline, models = _pipeline_with_delivery(f"{PACKAGE_NAME}_persist_fail", delivery)
    state = models.SessionState()
    failed = _delivered_sealed_ledger(models)
    failed.start_recording(object())

    await pipeline._record_ledger(
        "s1",
        state,
        failed,
        "reply",
        expected_generation=None,
        observed_active_at=1.0,
    )

    assert failed.phase == "record_failed"
    assert failed.record_failure == "state persistence retries exhausted"
    assert failed.start_recording(object()) is False
    assert delivery.apply_calls == 1
    assert delivery.persist_calls == 2
    assert state.daily_count == 1

    delivery._persist_ok_after = 1
    delivery.persist_calls = 0
    nxt = _delivered_sealed_ledger(models)
    nxt.start_recording(object())
    await pipeline._record_ledger(
        "s1",
        state,
        nxt,
        "next",
        expected_generation=None,
        observed_active_at=1.0,
    )

    assert nxt.phase == "recorded"
    assert delivery.apply_calls == 2
    assert state.daily_count == 2
    assert failed.phase == "record_failed"


class _ContractDelivery:
    """将真实 confirmed 交给 SessionState，区分已确认投递和未知投递的历史写入。"""

    def __init__(self) -> None:
        self.apply_calls = 0
        self.persist_calls = 0
        self.apply_kwargs: list[dict[str, object]] = []

    def apply_proactive_state(self, _umo, state, reply, direct_count, **kwargs) -> None:
        self.apply_calls += 1
        self.apply_kwargs.append({"reply": reply, "direct_count": direct_count, **kwargs})
        state.record_proactive_attempt(
            confirmed=bool(kwargs["confirmed"]),
            text=reply.strip() or f"[工具主动发送 x{direct_count}]",
            at=1.0,
        )

    async def persist_proactive_state(self) -> bool:
        self.persist_calls += 1
        return True


async def test_pipeline_unknown_delivery_consumes_quota_without_history_entry() -> None:
    """UNKNOWN 投递（契约 §2）：consumes 配额、不写历史、不记 last_proactive_text。"""
    delivery = _ContractDelivery()
    pipeline, models = _pipeline_with_delivery(f"{PACKAGE_NAME}_unknown_quota", delivery)
    state = models.SessionState()
    ledger = models.AttemptLedger()
    attempt = ledger.reserve("final_reply")
    ledger.mark_in_flight(attempt)
    ledger.seal()  # in-flight 在 seal 时转 UNKNOWN（可能已提交）
    assert ledger.has_unknown is True
    ledger.start_recording(object())

    await pipeline._record_ledger(
        "s1",
        state,
        ledger,
        "这条不该进历史",
        expected_generation=None,
        observed_active_at=42.0,
    )

    assert ledger.phase == "recorded"
    assert delivery.apply_calls == 1
    assert delivery.apply_kwargs == [
        {
            "reply": "",
            "direct_count": 0,
            "expected_generation": None,
            "observed_active_at": 42.0,
            "confirmed": False,
        }
    ]
    assert state.daily_count == 1
    assert state.last_proactive_at == 1.0
    assert list(state.recent) == []
    assert state.last_proactive_text == ""


@pytest.mark.parametrize(
    ("final_state", "expected_reply"),
    [
        ("delivered", "工具直发；最终回复"),
        ("unknown", "工具直发"),
    ],
)
async def test_pipeline_records_confirmed_direct_text_with_final_result(
    final_state: str,
    expected_reply: str,
) -> None:
    delivery = _ContractDelivery()
    pipeline, models = _pipeline_with_delivery(
        f"{PACKAGE_NAME}_direct_{final_state}",
        delivery,
    )
    state = models.SessionState()
    ledger = models.AttemptLedger()
    direct = ledger.reserve("tool_direct", "工具直发")
    ledger.mark_in_flight(direct)
    ledger.resolve(direct, models.SendStatus.DELIVERED)
    final = ledger.reserve("final_reply")
    ledger.mark_in_flight(final)
    if final_state == "delivered":
        ledger.resolve(final, models.SendStatus.DELIVERED)
    ledger.seal()
    ledger.start_recording(object())

    await pipeline._record_ledger(
        "s1",
        state,
        ledger,
        "最终回复",
        expected_generation=None,
        observed_active_at=42.0,
    )

    assert delivery.apply_kwargs[0]["reply"] == expected_reply
    assert delivery.apply_kwargs[0]["confirmed"] is True
    assert state.last_proactive_text == expected_reply


class _BlockingDelivery:
    """``persist_proactive_state`` 卡在 release 上，直到用例显式放行。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.apply_calls = 0
        self.persist_calls = 0

    def apply_proactive_state(self, _umo, state, reply, _direct_count, **_kwargs) -> None:
        self.apply_calls += 1
        state.record_proactive_attempt(confirmed=True, text=reply, at=1.0)

    async def persist_proactive_state(self) -> bool:
        self.persist_calls += 1
        self.started.set()
        await self.release.wait()
        return True


@pytest.fixture
def delivery() -> _BlockingDelivery:
    """落盘卡在 release 上的投递替身，取消/重入用例共用。"""
    return _BlockingDelivery()


async def test_pipeline_finalize_cancel_still_converges_record_task(
    delivery: _BlockingDelivery,
) -> None:
    """finalize 被取消时仍 shield 等待 record task，配额只记一次。"""
    pipeline, models = _pipeline_with_delivery(f"{PACKAGE_NAME}_finalize_cancel", delivery)
    pipeline._track_critical_task = asyncio.create_task
    state = models.SessionState()
    ledger = _delivered_sealed_ledger(models)

    finalizer = asyncio.create_task(
        pipeline._finalize_ledger(
            "s1",
            state,
            ledger,
            "reply",
            expected_generation=None,
            observed_active_at=1.0,
        )
    )
    await delivery.started.wait()
    finalizer.cancel()
    await asyncio.sleep(0)  # 将取消交给 finalizer。
    assert not finalizer.done(), "finalize 被取消时不得提前返回（仍需等待落盘）"
    assert delivery.persist_calls == 1
    delivery.release.set()
    with pytest.raises(asyncio.CancelledError):
        await finalizer

    assert ledger.phase == "recorded"
    assert delivery.apply_calls == 1
    assert delivery.persist_calls == 1
    assert state.daily_count == 1


async def test_pipeline_finalize_reentry_reuses_single_record_task(
    delivery: _BlockingDelivery,
) -> None:
    """收尾重入复用 record_task，避免重复记账与并发落盘。"""
    pipeline, models = _pipeline_with_delivery(f"{PACKAGE_NAME}_finalize_reentry", delivery)
    pipeline._track_critical_task = asyncio.create_task
    state = models.SessionState()
    ledger = _delivered_sealed_ledger(models)

    first = asyncio.create_task(
        pipeline._finalize_ledger(
            "s1",
            state,
            ledger,
            "reply",
            expected_generation=None,
            observed_active_at=1.0,
        )
    )
    await delivery.started.wait()
    # 第一条 record task 仍卡在落盘：此时二次进入，phase 还是 RECORDING
    assert ledger.phase == "recording"

    second = asyncio.create_task(
        pipeline._finalize_ledger(
            "s1",
            state,
            ledger,
            "reply",
            expected_generation=None,
            observed_active_at=1.0,
        )
    )
    await asyncio.sleep(0)  # 让第二次进入走到等待点
    assert not second.done(), "第二次进入应复用第一条 task 并等待它"

    delivery.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)

    assert delivery.apply_calls == 1, "重入另起了记账任务，配额被重复记录"
    assert delivery.persist_calls == 1, "重入另起了落盘任务"
    assert ledger.phase == "recorded"
    assert state.daily_count == 1


async def test_pipeline_record_exception_marks_record_failed() -> None:
    """apply 抛错 → record_failed，配额未记，下次账本仍可独立记账。"""

    class BoomDelivery:
        apply_calls = 0

        def apply_proactive_state(self, *_args, **_kwargs) -> None:
            self.apply_calls += 1
            raise RuntimeError("apply boom")

        async def persist_proactive_state(self) -> bool:
            return True

    delivery = BoomDelivery()
    pipeline, models = _pipeline_with_delivery(f"{PACKAGE_NAME}_record_exc", delivery)
    state = models.SessionState()
    ledger = _delivered_sealed_ledger(models)
    ledger.start_recording(object())

    await pipeline._record_ledger(
        "s1",
        state,
        ledger,
        "reply",
        expected_generation=None,
        observed_active_at=1.0,
    )

    assert ledger.phase == "record_failed"
    assert ledger.record_failure == "apply boom"
    assert delivery.apply_calls == 1
    assert state.daily_count == 0


class _BrokenStrDelivery:
    """``apply_proactive_state`` 抛一个 ``__str__`` 自身会抛异常的异常。"""

    apply_calls = 0

    def apply_proactive_state(self, *_args, **_kwargs) -> None:
        self.apply_calls += 1

        class BrokenStr(Exception):
            def __str__(self) -> str:  # pragma: no cover - 由被测代码触发
                raise RuntimeError("__str__ is broken")

        raise BrokenStr("apply boom")

    async def persist_proactive_state(self) -> bool:
        return True


async def test_pipeline_record_survives_exception_whose_str_raises(
    tmp_path: Path, caplog: object
) -> None:
    """异常的 __str__ 也可能抛错，错误格式化须保留账本结果和落盘重试。"""
    install_astrbot_stubs()
    package_name = f"{PACKAGE_NAME}_record_broken_str"
    pipeline_mod = load_package(package_name, "session_pipeline")
    models = load_package(package_name, "models")

    delivery = _BrokenStrDelivery()
    pipeline = object.__new__(pipeline_mod.SessionPipeline)
    # _record_ledger 按白名单重解析记账目标（契约 §1）；空白名单即「不迁移」。
    pipeline._settings = models.Settings.from_config({})
    pipeline._state_for = lambda _umo: models.SessionState()
    pipeline._delivery = delivery
    pipeline._track_critical_task = asyncio.create_task
    state = models.SessionState()
    ledger = _delivered_sealed_ledger(models)

    finalized = False
    with capture_logs(caplog, pipeline_mod.logger, logging.DEBUG):
        # 走 _finalize_ledger 而不是 _record_ledger：逃逸的异常要经过的正是这条链
        await pipeline._finalize_ledger(
            "s1",
            state,
            ledger,
            "reply",
            expected_generation=None,
            observed_active_at=1.0,
        )
        finalized = True

    assert finalized, "记账异常从 finalizer 逃出：主链已算好的返回值会被整段丢掉"
    assert ledger.phase == models.LedgerPhase.RECORD_FAILED
    assert ledger.record_failure == "BrokenStr"
    assert delivery.apply_calls == 1
    assert state.daily_count == 0

    lines = [
        record
        for record in caplog.records
        if "proactive ledger finalizer failed" in record.getMessage()
    ]
    assert lines, f"记账失败没有任何日志：{[r.getMessage() for r in caplog.records]}"
    assert lines[0].levelno == logging.ERROR
    # 脱敏收口的退化值是类型名：日志与 mark_record_failed 吃的是同一串文本
    assert "error=BrokenStr" in lines[0].getMessage()


async def test_pipeline_record_cancel_marks_record_failed_then_reraises() -> None:
    """recording 期间 CancelledError → record_failed 后继续上抛。"""

    class CancelDelivery:
        apply_calls = 0

        def apply_proactive_state(self, _umo, state, reply, _direct_count, **_kwargs) -> None:
            self.apply_calls += 1
            state.record_proactive_attempt(confirmed=True, text=reply, at=1.0)

        async def persist_proactive_state(self) -> bool:
            raise asyncio.CancelledError

    delivery = CancelDelivery()
    pipeline, models = _pipeline_with_delivery(f"{PACKAGE_NAME}_record_cancel", delivery)
    state = models.SessionState()
    ledger = _delivered_sealed_ledger(models)
    ledger.start_recording(object())

    with pytest.raises(asyncio.CancelledError):
        await pipeline._record_ledger(
            "s1",
            state,
            ledger,
            "reply",
            expected_generation=None,
            observed_active_at=1.0,
        )

    assert ledger.phase == "record_failed"
    assert ledger.record_failure == "state persistence task cancelled"
    assert delivery.apply_calls == 1
    assert state.daily_count == 1
