"""插件协作对象装配。

从 ``main._assemble_components`` 抽出，避免入口文件承载 100+ 行同质接线。
跨对象依赖一律 lambda 运行时查找，便于测试替换实例方法。
"""

from __future__ import annotations

from typing import Any

from .decision import DECISION_MAX_TOKENS, DECISION_SYSTEM_PROMPT, DecisionMaker
from .delivery import DeliveryRunner
from .generation import GenerationRunner
from .models import GRACEFUL_STOP_GRACE_SEC, now_ts
from .scheduler import SessionScheduler
from .session_coordinator import SessionCoordinator
from .session_pipeline import SessionPipeline
from .whitelist import WhitelistManager


def assemble_plugin_components(
    plugin: Any,
    *,
    get_runtime: Any,
    get_call_hook: Any,
    get_grace_stop_sec: Any | None = None,
) -> None:
    """接线 scheduler/decision/generation/coordinator/delivery/whitelist/pipeline。

    getter 参数必须每次调用重新解析（测试会替换 main 模块全局）。
    """
    grace = get_grace_stop_sec or (lambda: GRACEFUL_STOP_GRACE_SEC)
    plugin._scheduler = SessionScheduler(
        settings=plugin.settings,
        gate=plugin._gate,
        image_cache_dir=plugin._image_cache_dir,
        spawn=plugin._track_background_task,
        should_run=lambda: not plugin._stopping and plugin.runtime_enabled,
        state_for=lambda umo: plugin._state_for(umo),
        check_session=lambda umo, trigger, force, expected_generation: plugin._check_session(
            umo,
            trigger=trigger,
            force=force,
            expected_generation=expected_generation,
        ),
        clear_cached_event=lambda umo: plugin._clear_cached_event(umo),
        last_events=plugin._last_events,
        last_event_at=plugin._last_event_at,
        recent_image_events=plugin._recent_image_events,
        whitelist_runtime_umos=plugin._whitelist_runtime_umos,
        delay_tasks=plugin._delay_tasks,
        running_check_tasks=plugin._running_check_tasks,
        background_tasks=plugin._background_tasks,
    )
    plugin._scheduler.last_cleanup_at = now_ts()

    plugin._decision = DecisionMaker(
        settings=plugin.settings,
        resolve_provider=lambda umo: plugin.bridge.resolve_provider_id(
            umo, plugin.settings.judge_provider_id
        ),
        llm_generate=lambda provider_id, prompt: plugin.bridge.llm_generate(
            provider_id=provider_id,
            prompt=prompt,
            system_prompt=DECISION_SYSTEM_PROMPT,
            temperature=plugin.settings.decision_temperature,
            max_tokens=DECISION_MAX_TOKENS,
        ),
        read_history=lambda umo, limit: plugin.bridge.read_astrbot_history(umo, limit=limit),
        build_image_context=lambda umo, enabled, provider_id: plugin._build_image_context(
            umo, enabled=enabled, provider_id=provider_id
        ),
    )

    plugin._generation = GenerationRunner(
        settings=plugin.settings,
        context=plugin.context,
        runtime=lambda: get_runtime(),
        gate=plugin._gate,
        local_gate=lambda state, force: plugin._decision.local_gate(state, force=force),
        enforce_policy=lambda req, inherit_tools: plugin._enforce_final_tool_policy(
            req, inherit_tools
        ),
        call_hook=lambda event, event_type, req: get_call_hook()(event, event_type, req),
        grace_stop_sec=lambda: grace(),
        background_tasks=plugin._background_tasks,
        discard_background=plugin._background_tasks.discard,
        read_history=lambda umo, limit: plugin.bridge.read_astrbot_history(umo, limit=limit),
        build_image_context=lambda umo, enabled, provider_id: plugin._build_image_context(
            umo, enabled=enabled, provider_id=provider_id
        ),
        last_events=plugin._last_events,
    )

    plugin._coordinator = SessionCoordinator(
        events=plugin._last_events,
        event_at=plugin._last_event_at,
        images=plugin._recent_image_events,
        gate=plugin._gate,
        cancel_delay=lambda umo, force: plugin._cancel_delay_task(umo, force=force),
        notify_silence=lambda umo: plugin._scheduler.notify_activity(umo),
    )

    plugin._delivery = DeliveryRunner(
        settings=plugin.settings,
        gate=plugin._gate,
        local_gate=lambda state, force: plugin._decision.local_gate(state, force=force),
        last_events=plugin._last_events,
        call_hook=lambda event, event_type: get_call_hook()(event, event_type),
        context_send=lambda umo, message: plugin.context.send_message(umo, message),
        send_reply=lambda umo, reply, expected_generation: plugin._send_reply(
            umo, reply, expected_generation=expected_generation
        ),
        save_storage=lambda: plugin._save_storage(),
        runtime=lambda: get_runtime(),
    )

    plugin._whitelist = WhitelistManager(
        settings=plugin.settings,
        sync_whitelist=lambda: plugin._sync_whitelist(),
        save_storage=lambda: plugin._save_storage(),
        ensure_state=lambda key: plugin._state_for(key),
        invalidate=lambda umo: plugin._invalidate_session(umo),
        prune=lambda umo: plugin._prune_session(umo),
        sessions=plugin.sessions,
        tracked_umos=lambda: (
            set(plugin._last_events)
            | set(plugin._delay_tasks)
            | set(plugin._running_sessions)
            | set(plugin._session_locks)
        ),
        runtime_umos=plugin._whitelist_runtime_umos,
    )

    plugin._pipeline = SessionPipeline(
        owner=plugin,
        settings=plugin.settings,
        gate=plugin._gate,
        decision=plugin._decision,
        last_events=plugin._last_events,
    )
