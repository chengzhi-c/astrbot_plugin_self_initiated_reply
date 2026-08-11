"""DeliveryRunner 覆盖率补盲（0.9.0 轴 D）：send_reply 异常与分支路径。

盲区背景（补盲前 delivery.py 75%）：装饰钩子空结果、代次三连复核的
各失效点、外发未提交、after-send 钩子异常、context 兜底的 UNKNOWN/False
语义。全部经注入假门卫/钩子/发送器直接驱动（补盲前 75%）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .host_stubs import FakeEvent
from .test_delivery_runner import (
    FakeContextSend,
    FakeHook,
    FakeSave,
    _hook_names,
    _make_runner,
    _state,
)


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

    async def send_via_runner(umo: str, reply: str, expected_generation: int | None):
        return await runner.send_reply(umo, reply, expected_generation=expected_generation)

    runner._send_reply = send_via_runner
    state = _state(models)

    result = await runner.deliver_reply(
        "s1",
        state,
        "hello",
        0,
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert "状态未知" in result
    assert state.daily_count == 1
    assert state.last_proactive_at > 0
    assert state.last_proactive_observed_at == 100.0
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

    async def send_via_runner(umo: str, reply: str, expected_generation: int | None):
        return await runner.send_reply(umo, reply, expected_generation=expected_generation)

    runner._send_reply = send_via_runner
    state = _state(models)

    result = await runner.deliver_reply(
        "s1",
        state,
        "hello",
        0,
        expected_generation=1,
        observed_active_at=100.0,
        force=False,
        trigger="patrol",
    )

    assert "状态未知" in result
    assert state.daily_count == 1
    assert state.last_proactive_at > 0
    assert state.last_proactive_observed_at == 100.0
    assert all(record.role != "assistant" for record in state.recent)
    assert _hook_names(hook) == ["OnDecoratingResultEvent"]


# ============================================================================
# deliver_reply：失败混合出口与日志分支
# ============================================================================


async def test_deliver_failure_with_directs_and_gate_flip(tmp_path: Path) -> None:
    """发送失败且有工具直发：先记录，再撞代次翻转 → 放弃旧回复文案。"""
    _, models, runner, _ = _make_runner(tmp_path, sender_status="failed_before_submit")
    runner._gate = _FlipGate(true_times=2)
    state = _state(models)
    result = await runner.deliver_reply(
        "s1",
        state,
        "hello",
        1,
        expected_generation=7,
        observed_active_at=100.0,
        force=False,
        trigger="message_delay",
    )
    assert result == "会话已更新，放弃旧回复。"


async def test_deliver_log_reply_content_preview(tmp_path: Path) -> None:
    """log_reply_content 开启：长短回复预览分支都走 DEBUG 记录。"""
    _, models, runner, _ = _make_runner(
        tmp_path, save=FakeSave(), config={"log_reply_content": True}
    )
    state = _state(models)
    long_result = await runner.deliver_reply(
        "s1",
        state,
        "长" * 100,
        0,
        expected_generation=None,
        observed_active_at=100.0,
        force=False,
        trigger="message_delay",
    )
    assert long_result == "已主动回复。"
    short_result = await runner.deliver_reply(
        "s1",
        state,
        "短回复",
        0,
        expected_generation=None,
        observed_active_at=100.0,
        force=False,
        trigger="message_delay",
    )
    assert short_result == "已主动回复。"
