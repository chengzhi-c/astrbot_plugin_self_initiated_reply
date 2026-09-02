"""Attempt-ledger regression tests for outbound evidence retention."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from .host_stubs import install_astrbot_stubs, load_package

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


def test_seal_marks_inflight_attempt_unknown_and_ignores_late_delivery() -> None:
    """A cancellation after adapter entry remains an unconfirmed submission."""
    models = _models()
    ledger = models.AttemptLedger()

    attempt = ledger.reserve("tool_direct", "tool result")
    ledger.mark_in_flight(attempt)
    sealed = ledger.seal()

    assert sealed[0].state is models.AttemptState.UNKNOWN
    assert ledger.phase == "sealed"
    assert ledger.resolve(attempt, models.SendStatus.DELIVERED) is False
    assert attempt.state is models.AttemptState.UNKNOWN
    assert ledger.has_submission is True
    assert ledger.has_unknown is True


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
    state = models.SessionState()
    ledger = models.AttemptLedger()
    attempt = ledger.reserve("final_reply")
    ledger.mark_in_flight(attempt)
    ledger.resolve(attempt, models.SendStatus.DELIVERED)
    ledger.seal()
    ledger.start_recording(object())

    result = await pipeline._record_ledger(
        "s1",
        state,
        ledger,
        "reply",
        expected_generation=None,
        observed_active_at=1.0,
    )

    assert result is True
    assert delivery.apply_calls == 1
    assert delivery.persist_calls == 2
    assert state.daily_count == 1
    assert len(state.recent) == 1
    assert ledger.phase == "recorded"


def _pipeline_with_delivery(package_name: str, delivery: object):
    install_astrbot_stubs()
    pipeline_mod = load_package(package_name, "session_pipeline")
    models = load_package(package_name, "models")
    pipeline = object.__new__(pipeline_mod.SessionPipeline)
    pipeline._delivery = delivery
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

    result = await pipeline._record_ledger(
        "s1",
        state,
        failed,
        "reply",
        expected_generation=None,
        observed_active_at=1.0,
    )

    assert result is False
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
    second = await pipeline._record_ledger(
        "s1",
        state,
        nxt,
        "next",
        expected_generation=None,
        observed_active_at=1.0,
    )

    assert second is True
    assert nxt.phase == "recorded"
    assert delivery.apply_calls == 2
    assert state.daily_count == 2
    assert failed.phase == "record_failed"


async def test_pipeline_finalize_cancel_still_converges_record_task() -> None:
    """finalize 被取消时仍 shield 等待 record task，配额只记一次。"""
    install_astrbot_stubs()
    package_name = f"{PACKAGE_NAME}_finalize_cancel"
    pipeline_mod = load_package(package_name, "session_pipeline")
    models = load_package(package_name, "models")

    class HangingDelivery:
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

    delivery = HangingDelivery()
    pipeline = object.__new__(pipeline_mod.SessionPipeline)
    pipeline._delivery = delivery
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
    delivery.release.set()
    with pytest.raises(asyncio.CancelledError):
        await finalizer

    assert ledger.phase == "recorded"
    assert delivery.apply_calls == 1
    assert delivery.persist_calls == 1
    assert state.daily_count == 1


async def test_pipeline_rejected_critical_task_leaves_ledger_sealed() -> None:
    """record task 注册被拒：协程关闭、账本停在 sealed，配额未记。"""
    delivery = _CountingDelivery(persist_ok_after=1)
    pipeline, models = _pipeline_with_delivery(f"{PACKAGE_NAME}_reject_task", delivery)
    pipeline._track_critical_task = lambda _coro: None
    state = models.SessionState()
    ledger = _delivered_sealed_ledger(models)

    with pytest.raises(RuntimeError, match="critical task registration was rejected"):
        await pipeline._finalize_ledger(
            "s1",
            state,
            ledger,
            "reply",
            expected_generation=None,
            observed_active_at=1.0,
        )

    assert ledger.phase == "sealed"
    assert ledger.record_task is None
    assert delivery.apply_calls == 0
    assert state.daily_count == 0


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

    result = await pipeline._record_ledger(
        "s1",
        state,
        ledger,
        "reply",
        expected_generation=None,
        observed_active_at=1.0,
    )

    assert result is False
    assert ledger.phase == "record_failed"
    assert ledger.record_failure == "apply boom"
    assert delivery.apply_calls == 1
    assert state.daily_count == 0


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
