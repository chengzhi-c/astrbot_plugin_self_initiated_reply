"""短间隔下主动生成、引用和 @ 必须依据同一条消息。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from .test_delivery_runner import _make_runner as delivery_runner
from .test_generation_runner import FakeRuntime, _make_runner, _state
from .test_mention import _ChainRecordingEvent
from .test_pipeline_guards import _reply_decision, _wire_pipeline


async def test_reply_snapshot_survives_host_mutation_and_new_messages(tmp_path) -> None:
    umo = "qq:GroupMessage:12345"
    original = _ChainRecordingEvent(umo=umo, sender_id="alice", message_str="Alice 的问题")
    original.message_obj.message_id = "message-a"
    host_request, host_result = object(), object()
    original.set_extra("provider_request", host_request)
    original.set_result(host_result)

    class MutatingRuntime(FakeRuntime):
        def run(self, _agent, **_kwargs):
            async def events():
                original._sender_id = "bob"
                original.message_obj.message_id = "message-b"
                original.message_str = "Bob 的问题"
                pipeline._last_events[umo] = _ChainRecordingEvent(umo=umo, sender_id="charlie")
                await asyncio.sleep(0)
                yield None

            return events()

    runtime = MutatingRuntime()
    _, models, generation, *_ = _make_runner(tmp_path, runtime=runtime)
    generation._last_events[umo] = original
    _, _, delivery, _ = delivery_runner(
        tmp_path,
        config={"mention_mode": "always", "quote_mode": "random", "quote_probability": 100},
    )
    pipeline, _ = _wire_pipeline(
        "selfreply_snapshot_test_package",
        umo=umo,
        decision=_reply_decision(),
        generation=generation,
        delivery=delivery,
    )
    pipeline._last_events[umo] = original
    pipeline._state_for(umo).recent.append(models.MessageRecord("user", "Alice", "Alice 的问题"))
    result = await pipeline.check_session_locked(umo, trigger="message_delay", force=True)

    from astrbot.api.message_components import At, Reply

    assert result == "已主动回复。"
    assert [str(item.qq) for item in original.sent_chains[0] if isinstance(item, At)] == ["alice"]
    assert [str(item.id) for item in original.sent_chains[0] if isinstance(item, Reply)] == [
        "message-a"
    ]
    assert original.get_extra("provider_request") is host_request
    assert original.get_result() is host_result
    assert "本轮回复目标（Alice）: Alice 的问题" in runtime.built_reqs[0].prompt


async def test_repeated_text_cannot_relabel_host_history_as_the_reply_target(tmp_path) -> None:
    history = [
        {"role": "user", "content": "好的"},
        {"role": "assistant", "content": "之前约定只使用标准库"},
        {"role": "user", "content": "Bob 新话题"},
        {"role": "user", "content": "好的"},
    ]
    _, models, runner, runtime, *_ = _make_runner(tmp_path)
    state = _state(models, recent=[("user", "好的", 100.0)])
    runner._last_events["s1"] = _ChainRecordingEvent(
        umo="s1", sender_id="alice", message_str="好的"
    )

    async def load_conversation(_event, _context):
        return SimpleNamespace(
            cid="conversation", persona_id="persona", history=json.dumps(history)
        )

    async def hook(_event, _type, req):
        req.system_prompt = "保持原有人格"
        req.contexts.append({"role": "user", "content": "记忆钩子补充"})
        return False

    runtime.load_session_conversation = load_conversation
    runner._call_hook = hook
    await runner.generate("s1", state, force=True)
    request = runtime.built_reqs[0]

    assert "Bob 新话题" not in request.prompt
    assert "本轮回复目标（u）: 好的" in request.prompt
    assert request.contexts == [*history, {"role": "user", "content": "记忆钩子补充"}]
    assert request.conversation.persona_id == "persona"
    assert request.system_prompt == "保持原有人格"


async def test_long_target_keeps_earlier_context_without_duplicate_body(tmp_path) -> None:
    _, models, runner, runtime, *_ = _make_runner(tmp_path)
    target_text = "这是方案细节。" * 430
    state = _state(
        models,
        recent=[("user", "限制条件：只使用标准库", 1.0), ("user", target_text, 2.0)],
    )
    runner._last_events["s1"] = _ChainRecordingEvent(
        umo="s1", sender_id="alice", message_str=target_text
    )

    await runner.generate("s1", state, force=True)

    prompt = runtime.built_reqs[0].prompt
    assert "限制条件：只使用标准库" in prompt
    assert prompt.count(target_text) == 1


@pytest.mark.parametrize("trailing_reply", [False, True])
async def test_oversized_target_keeps_sender_within_context_budget(
    tmp_path, trailing_reply
) -> None:
    _, models, runner, *_ = _make_runner(tmp_path)
    state = _state(models, recent=[("user", "细节" * 3500 + "最后的问题", 1.0)])
    if trailing_reply:
        state.recent.append(models.MessageRecord("assistant", "Bot", "已经回复过一次。", 2.0))

    context = await runner.build_context_text("s1", state)

    assert len(context) <= models.MAX_GENERATION_CONTEXT_CHARS
    assert "本轮回复目标（u）: " in context
    assert context.endswith("最后的问题")
