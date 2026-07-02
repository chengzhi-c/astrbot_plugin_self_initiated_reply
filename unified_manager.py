from __future__ import annotations

from typing import Any

from .models import PLUGIN_ID


class UnifiedManagerApi:
    """Simplified dashboard API - only self-reply config."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def register(self, context: Any, route: str) -> None:
        register = context.register_web_api
        register(f"{route}/unified/overview", self.overview, ["GET"], "统一管理页概览")

    async def overview(self) -> dict[str, Any]:
        return {
            "ok": True,
            "self_reply": self._self_reply_summary(),
        }

    def _self_reply_summary(self) -> dict[str, Any]:
        settings = self.owner.settings
        return {
            "available": True,
            "enabled": bool(self.owner.runtime_enabled),
            "decision_model_enabled": settings.decision_model_enabled,
            "whitelist_count": len(settings.whitelist),
            "message_delay_sec": settings.message_delay_sec,
            "min_silence_sec": settings.min_silence_sec,
            "cooldown_sec": settings.cooldown_sec,
            # Backward-compatible aliases for older callers.
            "cooldown_seconds": settings.cooldown_sec,
            "idle_trigger_seconds": settings.message_delay_sec,
            "patrol_inactive_after_sec": settings.patrol_inactive_after_sec,
            "min_context_messages": settings.decision_history_min_messages,
            "decision_history_min_messages": settings.decision_history_min_messages,
        }
