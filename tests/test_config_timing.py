from __future__ import annotations

import sys
import types
import asyncio
from pathlib import Path


PLUGIN_PARENT = Path(__file__).resolve().parents[1].parent
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))


def _install_astrbot_stubs() -> None:
    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    class _MessageChain:
        def message(self, text):
            self.text = text
            return self

    class _Filter:
        class EventMessageType:
            ALL = "all"

        class PlatformAdapterType:
            ALL = "all"

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def platform_adapter_type(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def command_group(*args, **kwargs):
            def decorator(func):
                func.command = lambda *a, **k: (lambda command_func: command_func)
                return func

            return decorator

    class _PermissionType:
        ADMIN = "admin"

    def _identity_decorator(*args, **kwargs):
        return lambda func: func

    class _Star:
        def __init__(self, context):
            self.context = context

    def _register(*args, **kwargs):
        return lambda cls: cls

    class _ProviderRequest:
        pass

    class _MessageEventResult:
        def set_result_content_type(self, *args, **kwargs):
            return self

        def message(self, *args, **kwargs):
            return self

    class _ResultContentType:
        LLM_RESULT = "llm_result"

    class _MainAgentBuildConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _EventType:
        OnLLMRequestEvent = "on_llm_request"
        OnDecoratingResultEvent = "on_decorating_result"
        OnAfterMessageSentEvent = "on_after_message_sent"

    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.event.filter": types.ModuleType("astrbot.api.event.filter"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.api.message_components": types.ModuleType("astrbot.api.message_components"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.astr_agent_run_util": types.ModuleType("astrbot.core.astr_agent_run_util"),
        "astrbot.core.astr_main_agent": types.ModuleType("astrbot.core.astr_main_agent"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.message_event_result": types.ModuleType(
            "astrbot.core.message.message_event_result"
        ),
        "astrbot.core.provider": types.ModuleType("astrbot.core.provider"),
        "astrbot.core.provider.entities": types.ModuleType("astrbot.core.provider.entities"),
        "astrbot.core.star": types.ModuleType("astrbot.core.star"),
        "astrbot.core.star.star_handler": types.ModuleType("astrbot.core.star.star_handler"),
        "astrbot.core.pipeline": types.ModuleType("astrbot.core.pipeline"),
        "astrbot.core.pipeline.context": types.ModuleType("astrbot.core.pipeline.context"),
        "quart": types.ModuleType("quart"),
    }

    modules["astrbot.api"].logger = _Logger()
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = _MessageChain
    modules["astrbot.api.event"].filter = _Filter
    modules["astrbot.api.event.filter"].PermissionType = _PermissionType
    modules["astrbot.api.event.filter"].permission_type = _identity_decorator
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = _Star
    modules["astrbot.api.star"].register = _register
    modules["astrbot.api.message_components"].At = object
    modules["astrbot.core.astr_agent_run_util"].run_agent = lambda *a, **k: None
    modules["astrbot.core.astr_main_agent"].MainAgentBuildConfig = _MainAgentBuildConfig
    modules["astrbot.core.astr_main_agent"]._get_session_conv = lambda *a, **k: None
    modules["astrbot.core.astr_main_agent"].build_main_agent = lambda *a, **k: None
    modules["astrbot.core.message.message_event_result"].MessageEventResult = _MessageEventResult
    modules["astrbot.core.message.message_event_result"].ResultContentType = _ResultContentType
    modules["astrbot.core.provider.entities"].ProviderRequest = _ProviderRequest
    modules["astrbot.core.star.star_handler"].EventType = _EventType
    modules["astrbot.core.pipeline.context"].call_event_hook = lambda *a, **k: False
    modules["quart"].request = types.SimpleNamespace(get_json=lambda *a, **k: {})

    for name, module in modules.items():
        sys.modules.setdefault(name, module)


_install_astrbot_stubs()

from astrbot_plugin_self_initiated_reply.main import SelfInitiatedReplyPlugin  # noqa: E402
from astrbot_plugin_self_initiated_reply.models import Settings  # noqa: E402


def _plugin(config: dict) -> SelfInitiatedReplyPlugin:
    plugin = object.__new__(SelfInitiatedReplyPlugin)
    plugin.settings = Settings.from_config(config)
    plugin.runtime_enabled = plugin.settings.enabled
    return plugin


def test_config_api_exposes_runtime_timing_fields():
    plugin = _plugin(
        {
            "message_delay_sec": 90,
            "min_silence_sec": 180,
            "cooldown_sec": 600,
            "patrol_inactive_after_sec": 3600,
        }
    )

    config = asyncio.run(plugin._api_get_config())

    assert config["message_delay_sec"] == 90
    assert config["min_silence_sec"] == 180
    assert config["cooldown_sec"] == 600
    assert config["idle_trigger_seconds"] == 90
    assert config["patrol_inactive_after_sec"] == 3600


def test_config_api_saves_runtime_timing_fields(monkeypatch):
    plugin = _plugin(
        {
            "message_delay_sec": 20,
            "min_silence_sec": 30,
            "cooldown_sec": 20,
            "patrol_inactive_after_sec": 1800,
        }
    )
    plugin._sync_whitelist = lambda: None
    plugin._ensure_patrol_task = lambda: None
    plugin._cancel_delay_tasks = lambda: None

    async def _save_storage():
        pass

    async def _stop_patrol_task():
        pass

    async def _get_json(silent=True):
        return {
            "message_delay_sec": 120,
            "min_silence_sec": 300,
            "cooldown_sec": 900,
            "patrol_inactive_after_sec": 7200,
        }

    plugin._save_storage = _save_storage
    plugin._stop_patrol_task = _stop_patrol_task

    import astrbot_plugin_self_initiated_reply.main as main_module

    monkeypatch.setattr(main_module, "request", types.SimpleNamespace(get_json=_get_json))

    result = asyncio.run(plugin._api_post_config())

    assert result == {"ok": True}
    assert plugin.settings.message_delay_sec == 120
    assert plugin.settings.min_silence_sec == 300
    assert plugin.settings.cooldown_sec == 900
    assert plugin.settings.patrol_inactive_after_sec == 7200


def test_message_trigger_delay_waits_for_delay_and_minimum_silence():
    plugin = _plugin({"message_delay_sec": 20, "min_silence_sec": 120})

    assert plugin._message_trigger_delay("message_delay") == 120
    assert plugin._message_trigger_delay("reply_request") == 120


def test_message_trigger_delay_respects_longer_message_delay():
    plugin = _plugin({"message_delay_sec": 240, "min_silence_sec": 120})

    assert plugin._message_trigger_delay("message_delay") == 240
    assert plugin._message_trigger_delay("reply_request") == 120


def test_check_session_rechecks_silence_before_sending(monkeypatch):
    plugin = _plugin(
        {
            "whitelist_sessions": ["session-1"],
            "message_delay_sec": 20,
            "min_silence_sec": 60,
            "cooldown_sec": 0,
            "max_daily_replies_per_session": 0,
        }
    )
    plugin._stopping = False
    plugin._running_sessions = set()
    plugin.sessions = {}
    state = plugin._state_for("session-1")
    state.last_active_at = 100.0
    state.last_proactive_at = 0.0
    state.last_proactive_observed_at = 0.0
    send_calls = []

    async def _ask_decision_model(*args, **kwargs):
        return {"should_reply": True, "reason": "test", "elapsed_sec": 0.0}

    async def _generate_reply_via_pipeline(*args, **kwargs):
        state.last_active_at = 190.0
        return "reply"

    async def _send_reply(umo, reply):
        send_calls.append((umo, reply))
        return True

    async def _save_storage():
        pass

    import astrbot_plugin_self_initiated_reply.main as main_module

    monkeypatch.setattr(main_module, "now_ts", lambda: 200.0)
    plugin._in_quiet_hours = lambda: False
    plugin._ask_decision_model = _ask_decision_model
    plugin._generate_reply_via_pipeline = _generate_reply_via_pipeline
    plugin._send_reply = _send_reply
    plugin._save_storage = _save_storage

    result = asyncio.run(plugin._check_session("session-1", trigger="message_delay", force=False))

    assert send_calls == []
    assert result.startswith("静默时间不足")
