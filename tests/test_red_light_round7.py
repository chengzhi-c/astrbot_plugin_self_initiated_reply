"""红灯测试（第七轮）：v0.8.4 前站修复验证

每个测试断言"期望的正确行为"，修复前应当失败（红灯），修复后转绿：
- R13 越权取消：非管理员写指令（add）不得取消在途主动回复
- R14 取消语义：管理员写指令（add）才取消在途回复；只读指令（status）不打断
- R15 webapi 新键：13 个规范键 + decision_history_min_messages 真实生效，非静默吞
- R16 webapi fail loud：schema 之外的未知键被拒，错误信息列出未知键
- R17 bridge 缓存：get_recorder_bridge 复用首个实例，不随 context 重建
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

from .host_stubs import with_plugin
from .test_main_runtime import UMO, _make_event


def _load_main():
    import tests.test_vision as vision

    root = Path(vision.ROOT)
    package = types.ModuleType(vision.PACKAGE_NAME)
    package.__path__ = [str(root)]
    sys.modules[vision.PACKAGE_NAME] = package
    return importlib.import_module(f"{vision.PACKAGE_NAME}.main")


# ============================================================================
# R13：非管理员写指令不取消在途回复（MP1-3 越权取消）
# ============================================================================


def test_r13_non_admin_write_command_does_not_cancel(tmp_path: Path) -> None:
    """非管理员发写指令：权限拒绝先行，在途延迟检查不得被取消。

    修复前 on_message 命令分支无条件 _cancel_event_session（白名单会话）
    → 任务被取消（红灯）。
    """

    async def scenario(plugin, main):
        event = _make_event(message_str="/selfreply add")
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin.settings.whitelist = {UMO}
        plugin._schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        task = plugin._delay_tasks.get(UMO)
        assert task is not None and not task.done()

        # FakeEvent.is_admin() 恒 False：非管理员
        await plugin.on_message(event)
        assert not task.cancelled(), "非管理员写指令不应取消在途回复"
        assert plugin._delay_tasks.get(UMO) is task

    with_plugin(tmp_path, scenario)


# ============================================================================
# R14：管理员写指令取消、只读指令不取消（MP1-3 语义）
# ============================================================================


def test_r14_admin_write_cancels_but_read_does_not(tmp_path: Path) -> None:
    """管理员视角：只读（status）不打断进行中的检查，写（add）才取消。

    修复前只读指令同样被无条件取消 → status 后任务被取消（红灯）。
    """

    async def scenario(plugin, main):
        plugin.settings.whitelist = {UMO}
        plugin._schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        task = plugin._delay_tasks.get(UMO)
        assert task is not None and not task.done()

        # 只读指令不打断
        read_event = _make_event(message_str="/selfreply status")
        read_event.role = "admin"
        plugin._last_events[UMO] = read_event
        plugin._last_event_at[UMO] = 1.0
        await plugin.on_message(read_event)
        # 用任务表引用断言：cancelling 状态时 cancelled() 尚为 False，
        # 引用断言才能区分"未取消"与"正在取消"。
        assert plugin._delay_tasks.get(UMO) is task, "只读指令不应取消在途回复"

        # 写指令取消在途回复
        write_event = _make_event(message_str="/selfreply add")
        write_event.role = "admin"
        plugin._last_events[UMO] = write_event
        plugin._last_event_at[UMO] = 1.0
        await plugin.on_message(write_event)
        assert plugin._delay_tasks.get(UMO) is not task, "管理员写指令应取消在途回复"

    with_plugin(tmp_path, scenario)


# ============================================================================
# R15：webapi 新键真实解析（MP1-4 吞字段）
# ============================================================================


def test_r15_new_config_keys_take_effect(tmp_path: Path) -> None:
    """POST 13 个规范键 + decision_history_min_messages 必须真实写入 settings。

    修复前这些键无处理分支 → ok:true 但 settings 不变（虚假绿灯，红灯）。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {
            "recent_message_limit": 30,
            "reply_length_mode": "short",
            "allow_multiline_reply": False,
            "max_reply_chars": 300,
            "log_reply_content": True,
            "bot_aliases": ["小c", "阿c"],
            "ignored_sender_ids": ["u9"],
            "check_interval_sec": 600,
            "max_daily_replies_per_session": 7,
            "quiet_hours": ["22:00-23:00"],
            "enabled_message_trigger": False,
            "enabled_patrol_trigger": True,
            "generation_timeout_sec": 90,
            "decision_history_min_messages": 8,
        }
        result = await plugin._api_post_config()
        assert result.get("ok") is True, result
        s = plugin.settings
        assert s.recent_message_limit == 30
        assert s.reply_length_mode == "short"
        assert s.allow_multiline_reply is False
        assert s.max_reply_chars == 300
        assert s.log_reply_content is True
        assert s.bot_aliases == ["小c", "阿c"]
        assert s.ignored_sender_ids == {"u9"}
        assert s.check_interval_sec == 600
        assert s.max_daily_replies_per_session == 7
        assert s.quiet_hours == ["22:00-23:00"]
        assert s.enabled_message_trigger is False
        assert s.enabled_patrol_trigger is True
        assert s.generation_timeout_sec == 90
        assert s.decision_history_min_messages == 8

    with_plugin(tmp_path, scenario)


# ============================================================================
# R16：webapi 未知键 fail loud（MP1-4）
# ============================================================================


def test_r16_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    """schema 之外的键必须被拒并列出未知键，而不是静默返回 ok:true。

    修复前未知键被忽略 → ok:true（虚假成功，红灯）。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"bogus_setting": 1}
        result = await plugin._api_post_config()
        assert result.get("ok") is False
        assert "bogus_setting" in str(result.get("error", ""))

    with_plugin(tmp_path, scenario)


# ============================================================================
# R17：bridge 缓存复用（MP1-8）
# ============================================================================


def test_r17_recorder_bridge_is_cached(tmp_path: Path) -> None:
    """get_recorder_bridge 第二次调用必须复用首个实例。

    修复前条件 `_default_bridge is None or context is not None` 恒真
    （唯一调用点恒传 context）→ 每次新建实例（红灯）。
    """

    async def scenario(plugin, main):
        from .host_stubs import MAIN_PACKAGE_NAME

        rb = sys.modules[f"{MAIN_PACKAGE_NAME}.image.recorder_bridge"]
        rb._default_bridge = None
        first = rb.get_recorder_bridge(plugin.context)
        # 唯一调用点恒传 context：修复前每次调用都重建 → 非同一实例（红灯）
        second = rb.get_recorder_bridge(plugin.context)
        assert second is first, "bridge 必须复用缓存实例"
        rb._default_bridge = None

    with_plugin(tmp_path, scenario)
