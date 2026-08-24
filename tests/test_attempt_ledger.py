"""Attempt-ledger regression tests for outbound evidence retention."""

from __future__ import annotations

from uuid import UUID

from .host_stubs import install_astrbot_stubs, load_package

PACKAGE_NAME = "selfreply_attempt_ledger_test_package"


def _models():
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
