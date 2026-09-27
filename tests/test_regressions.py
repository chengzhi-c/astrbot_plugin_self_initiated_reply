"""历史缺陷回归测试。

按主题组织的历史回归守卫：
- 命令入口与生命周期
- 回归清单：工具策略、配置回滚、并发互斥、ABA 等
- Agent 管线装配与宿主交互路径
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from .host_stubs import (
    PipelineTestAdapter,
    load_modules,
    until,
    webapi_module,
    with_plugin,
)
from .test_main_runtime import UMO, _make_event

ROOT = Path(__file__).resolve().parents[1]


# ============================================================================
# 命令入口与生命周期
# ============================================================================

PACKAGE_NAME_R3 = "selfreply_regressions_package"


def _load_r3_modules():
    return load_modules(
        PACKAGE_NAME_R3, "models", "utils", "commands", "image", "image.recorder_bridge"
    )


# ============================================================================
# RL-4 任意用户可触发命令并吞掉事件（中危）
# ============================================================================


def test_help_action_is_reachable_without_admin() -> None:
    """确认 help 不在管理员动作集合内（用于说明上一条的影响面）。"""
    models, _, _, _, _ = _load_r3_modules()
    assert "help" not in models.ADMIN_COMMAND_ACTIONS


def test_bare_command_word_is_parsed_as_command() -> None:
    """记录当前解析行为：裸词即命令（说明缺陷来源，非断言修复）。"""
    _, _, commands, _, _ = _load_r3_modules()
    assert commands.parse_command_text("selfreply") == ("help", "")
    assert commands.parse_command_text("selfreply add") == ("add", "")


# RL-5（Web 配置读取失败返回 None）的守卫已迁至
# test_webapi.py::test_api_get_config_error_path，那里是真调 API 断言
# 载荷形状，比在 except 尾段里搜 "return" 更直接，也不会因重排 except 而误红。


def test_leading_mention_strip_only_removes_at_form() -> None:
    """@ 前缀剥离必须精确匹配 ``[At:<id>]``，不得吞掉正文方括号块。

    宽松正则 ``\\[[^\\]]*[Aa][Tt][^\\]]*\\]`` 只要求方括号内含子串 "at"，
    于是 ``[chat]`` / ``[data]`` / ``[cat]`` 开头的消息前缀会在入历史时
    被静默删除，进判断模型的消息文本与群里实际发言不符。
    """
    _, utils, _, _, _ = _load_r3_modules()
    assert utils.clean_chat_text("[At:123] 你好") == "你好"
    assert utils.clean_chat_text("[At:123][At:456] 你好") == "你好"
    assert utils.clean_chat_text("[chat] 你好") == "[chat] 你好"
    assert utils.clean_chat_text("[data] x") == "[data] x"
    assert utils.clean_chat_text("[cat] meow") == "[cat] meow"


# ============================================================================
# RL-6 会话代次表无界增长（低危）
# ============================================================================


def test_image_cache_cleanup_has_manual_api_and_startup_sweep(tmp_path: Path) -> None:
    """插件启动即回收过期缓存（不等到首个周期），并注册手动 POST 清理入口。

    启动清理现为后台任务（rglob+stat 不跑在宿主事件循环上，见
    test_cleanup_nonblocking 的启动契约），故“即回收”断言为有界等待而非
    构造同步完成；防回归的锚点是“不等首个周期”（周期下限 60s，等待
    上限 2s，量级上不可能混淌）。
    """
    models, _, _, _, _ = _load_r3_modules()
    cache_dir = tmp_path / "data" / models.PLUGIN_ID / "image_cache"
    cache_dir.mkdir(parents=True)
    expired = cache_dir / "expired.png"
    expired.write_bytes(b"old")
    os.utime(expired, (1, 1))

    async def scenario(plugin, main):
        await until(lambda: not expired.exists())
        assert any(
            route.endswith("/image-cache/cleanup") and "POST" in methods
            for route, _handler, methods, _description in plugin.context.register_web_api_calls
        )

    with_plugin(
        tmp_path,
        scenario,
        vision_judge_enabled=False,
        vision_main_enabled=False,
        vision_image_age_sec=60,
    )


def test_plugin_logo_is_root_square_png() -> None:
    """AstrBot 从插件根目录的 logo.png 读取插件图标。"""
    logo = ROOT / "logo.png"
    data = logo.read_bytes()
    assert logo.is_file()
    assert data[:8] == bytes.fromhex("89504e470d0a1a0a")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    assert width == height
    assert width > 0


def test_config_mutations_share_one_lock_and_settings_normalizer(tmp_path: Path) -> None:
    """两次并发 POST 不得交错损坏配置；候选值必须经 Settings 归一。"""

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        webapi = webapi_module(main)
        payloads = iter(({"cooldown_sec": 111}, {"min_silence_sec": 222}))
        original = webapi._request_json

        async def fake_json():
            payload = next(payloads)
            await asyncio.sleep(0)
            return payload

        webapi._request_json = fake_json
        try:
            first, second = await asyncio.gather(
                webapi_module(main)._api_post_config(plugin),
                webapi_module(main)._api_post_config(plugin),
            )
        finally:
            webapi._request_json = original
            web.request.payload = {}

        assert first["ok"] is True
        assert second["ok"] is True
        assert plugin.settings.cooldown_sec == 111
        assert plugin.settings.min_silence_sec == 222

    with_plugin(tmp_path, scenario)


# ============================================================================
# 工具策略与配置回滚
# ============================================================================


def _load_vision_image():
    """复用 test_vision 的动态包加载模式。"""
    import tests.test_vision as vision

    root = Path(vision.ROOT)
    package = types.ModuleType(vision.PACKAGE_NAME)
    package.__path__ = [str(root)]
    sys.modules[vision.PACKAGE_NAME] = package
    return importlib.import_module(f"{vision.PACKAGE_NAME}.image")


def _install_tool_injecting_pipeline(plugin, main, *, event):
    """固定形态：3 标准工具 + hook 注入 + reset/prompts 双快照。

    泛化实现见 host_stubs.install_tool_injecting_pipeline。
    """
    from .host_stubs import install_tool_injecting_pipeline

    return install_tool_injecting_pipeline(
        plugin, main, event=event, snapshot_reset=True, snapshot_prompts=True
    )


async def _run_pipeline(plugin):
    state = plugin._state_for(UMO)
    token = plugin._gate.advance(UMO)
    return await plugin._generation.generate(UMO, state, expected_generation=token, force=True)


def test_config_change_mid_run_does_not_flip_tool_policy(tmp_path: Path) -> None:
    """入口快照：运行中把开关改为 True 不得让本次运行 fail-open。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        event.plugins_name = ["other_plugin"]

        def run_effect(runner, **_kwargs):
            async def gen():
                # 模拟用户在一次主动运行中途保存配置开启继承
                plugin.settings.proactive_inherit_tools = True
                yield None

            return gen()

        from .host_stubs import install_tool_injecting_pipeline

        ctrl = install_tool_injecting_pipeline(plugin, main, event=event, run_effect=run_effect)
        enforce_snapshots = ctrl["enforce_snapshots"]
        try:
            result = await _run_pipeline(plugin)
            assert result.text == "你好呀"
            # 快照为 False：即使运行中 settings 变为 True，enforce 仍按 False 清理
            assert enforce_snapshots == [[], []]
            assert main._AGENT_RUNTIME._tool_list(ctrl["req_holder"]["req"]) == []
        finally:
            ctrl["restore"]()

    with_plugin(tmp_path, scenario)


def test_second_enforce_happens_before_reset(tmp_path: Path) -> None:
    """reset 执行时工具集必须已经清理：hook 注入的工具不能进 runner。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        event.plugins_name = ["other_plugin"]

        ctrl = _install_tool_injecting_pipeline(plugin, main, event=event)
        try:
            result = await _run_pipeline(plugin)
            assert result.text == "你好呀"
            assert ctrl["enforce_snapshots"] == [[], []]
            # reset 执行时工具集为空：第二次清理在 reset 之前完成
            assert ctrl["reset_snapshots"] == [[]]
        finally:
            ctrl["restore"]()

    with_plugin(tmp_path, scenario)


def test_system_hint_matches_tool_policy(tmp_path: Path) -> None:
    """继承模式提示词描述真实边界；默认模式仍写死禁用工具。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0

        ctrl = _install_tool_injecting_pipeline(plugin, main, event=event)
        try:
            await _run_pipeline(plugin)
            assert len(ctrl["prompts"]) == 1
            default_prompt = ctrl["prompts"][0]
            assert "不得执行命令或 Python" in default_prompt
        finally:
            ctrl["restore"]()

    with_plugin(tmp_path, scenario)

    async def inherit_scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0

        ctrl = _install_tool_injecting_pipeline(plugin, main, event=event)
        try:
            await _run_pipeline(plugin)
            assert len(ctrl["prompts"]) == 1
            inherit_prompt = ctrl["prompts"][0]
            assert "继承宿主完整工具链" in inherit_prompt
            assert "不得执行命令或 Python" not in inherit_prompt
        finally:
            ctrl["restore"]()

    with_plugin(tmp_path / "inherit", inherit_scenario, proactive_inherit_tools=True)


def test_cache_hit_does_not_rewrite_file(tmp_path: Path) -> None:
    """内容寻址命中且未篡改时不得重写文件（digest 比较修复）。"""

    image = _load_vision_image()
    parser = image.ImageParser(object(), source_cache_dir=tmp_path / "image_cache")
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    encoded = base64.b64encode(payload).decode()
    data_url = "data:image/png;base64," + encoded
    digest = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "image_cache" / digest[:2] / f"{digest}.png"

    writes: list[bytes] = []
    original_write = Path.write_bytes

    def counting_write_bytes(self, data):
        writes.append(bytes(data))
        return original_write(self, data)

    Path.write_bytes = counting_write_bytes
    try:
        assert parser._materialize_data_url(data_url) is not None
        assert parser._materialize_data_url(data_url) is not None
        assert len(writes) == 1, f"命中缓存不得重写，实际写入 {len(writes)} 次"
        assert target.read_bytes() == payload
    finally:
        Path.write_bytes = original_write


def test_config_rollback_restores_sessions_and_locks(tmp_path: Path) -> None:
    """回滚必须恢复 sessions 与 _session_locks（与 settings 同级），
    且被白名单变更 prune 掉的会话状态必须**原对象**复活（契约 §11 B2）。

    §11 B2 的失效形态是静默的：``_whitelist.replace`` 会 pop 掉被移出会话的
    ``SessionState``，回滚若只按 ``_restore_session_history`` 的「saved 里有而
    sessions 里没有 → 新建对象」分支走，会话键虽在、**标量全清零**
    （日配额/冷却/观察窗口）且对象身份丢失（在途任务写孤儿状态）。
    ``whitelist.commit_change`` 有 ``pruned`` 回填，webapi 的回滚路径此前漏了。
    """

    async def scenario(plugin, main):
        import sys

        umo = UMO
        plugin.sessions[umo] = plugin._state_for(umo)
        plugin._gate.lock_for(umo)
        plugin.settings.whitelist = {umo}

        # 造出非零标量：这些字段正是 §11 B2 要保的东西
        state = plugin.sessions[umo]
        state.daily_count = 5
        state.last_proactive_at = 1234.0
        state.last_proactive_observed_at = 2345.0
        state.last_active_at = 3456.0
        state.last_proactive_text = "上次回复"
        original_state = plugin.sessions[umo]

        async def boom():
            raise OSError("disk full")

        plugin._save_storage = boom
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist_sessions": []}
        # API 层不抛异常：内部回滚后返回 ok:False
        result = await webapi_module(main)._api_post_config(plugin)
        assert result.get("ok") is False
        assert umo in plugin.sessions
        assert umo in plugin._session_locks

        # §11 B2：原对象复活（身份即正确性，在途任务持的是它）
        restored = plugin.sessions[umo]
        assert restored is original_state, "回滚未保住 SessionState 对象身份"
        assert restored.daily_count == 5, f"日配额被清零：{restored.daily_count}"
        assert restored.last_proactive_at == 1234.0, "冷却时间戳被清零"
        assert restored.last_proactive_observed_at == 2345.0, "观察窗口被清零"
        assert restored.last_active_at == 3456.0, "活跃时间被清零"
        assert restored.last_proactive_text == "上次回复", "上次回复文本丢失"

    with_plugin(tmp_path, scenario)


# ============================================================================
# 工具策略 / UNKNOWN / 并发
# ============================================================================


def _load_vision_main():
    import tests.test_vision as vision

    root = Path(vision.ROOT)
    package = types.ModuleType(vision.PACKAGE_NAME)
    package.__path__ = [str(root)]
    sys.modules[vision.PACKAGE_NAME] = package
    return importlib.import_module(f"{vision.PACKAGE_NAME}.main")


# ============================================================================
# 继承模式危险工具 denylist
# ============================================================================


def test_inherit_mode_denylists_host_dangerous_tools(tmp_path: Path) -> None:
    """继承模式放行普通工具，但宿主级危险工具（含 hook 注入）一律拒绝。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0

        from .host_stubs import install_tool_injecting_pipeline

        ctrl = install_tool_injecting_pipeline(
            plugin,
            main,
            event=event,
            # 普通插件工具 + 宿主级危险工具（cron，4.23.3 实测名 future_task）
            build_tools=("send_image", "future_task"),
            # 模拟 hook 在第一次 enforce 后注入危险工具（kb agentic）与普通工具
            first_enforce_tools=("astr_kb_search", "third_party_weather"),
        )
        enforce_snapshots = ctrl["enforce_snapshots"]
        try:
            result = await plugin._generation.generate(
                UMO, plugin._state_for(UMO), expected_generation=1, force=True
            )
            assert result.text == "你好呀"
            # 修复前：继承分支直接 return True → 危险工具残留（红灯）
            assert enforce_snapshots[0] == ["send_image"]
            assert enforce_snapshots[1] == ["send_image", "third_party_weather"]
        finally:
            ctrl["restore"]()

    with_plugin(tmp_path, scenario, proactive_inherit_tools=True)


# ============================================================================
# UNKNOWN 发送 + 工具直发时仍记录状态
# ============================================================================


def test_unknown_send_records_state_even_with_direct_sends(tmp_path: Path) -> None:
    """工具已直发后最终文本提交 UNKNOWN：状态必须记录，观察窗口必须推进。"""

    from types import SimpleNamespace

    from .host_stubs import _FakeMessageChain

    async def scenario(plugin, main):
        from .host_stubs import DirectSendingRunner, FakeBuildResult, PipelineTestAdapter

        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        async def build_effect(kwargs, result):
            kwargs["req"].func_tool.add_tool(SimpleNamespace(name="send_image"))
            return FakeBuildResult(
                agent_runner=DirectSendingRunner(event),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=DirectSendingRunner(event).reset(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                # 模拟工具直发（tool_direct_result）
                await _runner._target.send(
                    _FakeMessageChain(type="tool_direct_result", chain=["图"])
                )
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_send_reply = plugin._delivery.send_reply

        async def unknown_send_reply(umo, reply, expected_generation=None, **_kwargs):
            models = importlib.import_module(f"{main.__package__}.models")
            return models.SendOutcome(models.SendStatus.UNKNOWN, "adapter raised after submit")

        plugin._delivery.send_reply = unknown_send_reply
        try:
            result = await plugin._pipeline.check_session(UMO, trigger="patrol", force=True)
            assert "未自动重试" in result
            # 修复前：direct_send_count>0 时跳过记录 → daily_count 不增（红灯）
            assert state.daily_count >= 1
            assert state.last_proactive_observed_at >= state.last_active_at
            assert state.last_proactive_at >= state.last_active_at
        finally:
            plugin._delivery.send_reply = original_send_reply
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# 发送已提交后 after-send 取消仍须保留一次性状态记录
# ============================================================================


def test_after_send_cancellation_records_delivered_attempt(tmp_path: Path) -> None:
    """Cancellation after a delivered send cannot erase the external side effect."""

    async def scenario(plugin, main):
        from .host_stubs import DirectSendingRunner, FakeBuildResult, PipelineTestAdapter

        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        async def build_effect(kwargs, result):
            return FakeBuildResult(
                agent_runner=DirectSendingRunner(completion_text="after-send reply"),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=DirectSendingRunner().reset(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        original_hook = main.call_event_hook
        save_calls: list[int] = []
        original_save = plugin._save_storage

        async def counting_save() -> None:
            save_calls.append(1)
            await original_save()

        plugin._save_storage = counting_save
        original_finalize = plugin._pipeline._finalize_ledger
        captured_ledger: dict[str, Any] = {}

        async def capture_finalize(umo_arg, state_arg, ledger_arg, reply_arg, **kwargs):
            captured_ledger["value"] = ledger_arg
            return await original_finalize(umo_arg, state_arg, ledger_arg, reply_arg, **kwargs)

        plugin._pipeline._finalize_ledger = capture_finalize
        main._AGENT_RUNTIME = PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )

        async def cancel_after_send(event_obj, event_type, *args, **kwargs):
            if event_type.name == "OnAfterMessageSentEvent":
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                await asyncio.sleep(0)
            return await original_hook(event_obj, event_type, *args, **kwargs)

        main.call_event_hook = cancel_after_send
        try:
            task = asyncio.create_task(
                plugin._pipeline.check_session(UMO, trigger="patrol", force=True)
            )
            try:
                await task
                raise AssertionError("expected cancellation from the after-send hook")
            except asyncio.CancelledError:
                pass

            assert state.daily_count == 1
            assert state.last_proactive_observed_at >= state.last_active_at
            assert state.last_proactive_text == "after-send reply"
            assert state.recent[-1].text == "after-send reply"
            assert len(save_calls) == 1
            ledger = captured_ledger["value"]
            assert isinstance(ledger.ledger_id, str)
            assert ledger.ledger_id
            assert ledger.phase == "recorded"
            assert [attempt.state.value for attempt in ledger.attempts] == ["delivered"]
        finally:
            main.call_event_hook = original_hook
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# 配置回滚后延迟检查重新调度
# ============================================================================


def test_rollback_reschedules_delayed_check(tmp_path: Path) -> None:
    """回滚恢复会话后，被白名单变更取消的延迟检查必须重新调度。"""

    import sys

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._state_for(UMO)
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        old_task = plugin._delay_tasks.get(UMO)
        assert old_task is not None and not old_task.cancelled()

        async def boom():
            raise OSError("disk full")

        plugin._save_storage = boom
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"whitelist_sessions": []}
        result = await webapi_module(main)._api_post_config(plugin)
        assert result.get("ok") is False
        # 修复前：回滚不恢复延迟任务 → UMO 不在 _delay_tasks（红灯）
        new_task = plugin._delay_tasks.get(UMO)
        assert new_task is not None
        assert new_task is not old_task
        assert not new_task.cancelled()

    with_plugin(tmp_path, scenario)


# ============================================================================
# 生成超时先优雅停止
# ============================================================================


def test_timeout_requests_graceful_stop(tmp_path: Path) -> None:
    """超时时先调 request_stop 让 run_agent 走正常清理，而不是硬取消。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    stop_called: list[bool] = []

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0

        class HangingRunner:
            def reset(self, **_):
                return _FakeResetCoro()

            def request_stop(self):
                stop_called.append(True)

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="", result_chain=None)

            def close(self):
                pass

        async def build_effect(kwargs, result):
            return FakeBuildResult(
                agent_runner=HangingRunner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                await asyncio.sleep(3600)  # 永不结束
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_grace = main.GRACEFUL_STOP_GRACE_SEC
        main.GRACEFUL_STOP_GRACE_SEC = 0.05
        try:
            result = await plugin._generation.generate(
                UMO, plugin._state_for(UMO), expected_generation=1, force=True
            )
            # 修复前：wait_for 直接取消 run_agent → request_stop 从未被调（红灯）
            assert stop_called == [True]
            assert result.text == ""
        finally:
            main.GRACEFUL_STOP_GRACE_SEC = original_grace
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario, generation_timeout_sec=0.05)


# ============================================================================
# 配置 revision/CAS 与规范化反馈
# ============================================================================


def test_config_revision_rejects_stale_versioned_write(tmp_path: Path) -> None:
    """版本化 POST 只接受读取时的 revision，冲突不得部分应用。"""

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        current = await webapi_module(main)._api_get_config(plugin)
        original_min_silence = plugin.settings.min_silence_sec
        assert current["config_revision"].startswith("sha256:")

        web.request.payload = {
            "cooldown_sec": 111,
            "base_revision": current["config_revision"],
        }
        first = await webapi_module(main)._api_post_config(plugin)
        assert first["ok"] is True
        assert first["config_revision"] != current["config_revision"]
        assert plugin.settings.cooldown_sec == 111

        web.request.payload = {
            "min_silence_sec": 222,
            "base_revision": current["config_revision"],
        }
        stale = await webapi_module(main)._api_post_config(plugin)
        assert stale == {
            "ok": False,
            "error_code": "STALE_WRITE",
            "error": "配置已被其他请求修改",
            "config_revision": first["config_revision"],
        }
        assert plugin.settings.min_silence_sec == original_min_silence

    with_plugin(tmp_path, scenario)


def test_concurrent_versioned_writers_have_one_winner(tmp_path: Path) -> None:
    """同一 revision 的并发全量写入只能有一个赢家。"""

    async def scenario(plugin, main):
        webapi = webapi_module(main)
        revision = (await webapi_module(main)._api_get_config(plugin))["config_revision"]
        payloads = [
            {"cooldown_sec": 111, "base_revision": revision},
            {"min_silence_sec": 222, "base_revision": revision},
        ]
        original = webapi._request_json

        async def fake_json():
            payload = payloads.pop(0)
            await asyncio.sleep(0)
            return payload

        webapi._request_json = fake_json
        try:
            first, second = await asyncio.gather(
                webapi_module(main)._api_post_config(plugin),
                webapi_module(main)._api_post_config(plugin),
            )
        finally:
            webapi._request_json = original

        assert sorted([first["ok"], second["ok"]]) == [False, True]
        stale = first if first["ok"] is False else second
        assert stale["error_code"] == "STALE_WRITE"
        assert plugin.settings.cooldown_sec == 111
        assert plugin.settings.min_silence_sec != 222

    with_plugin(tmp_path, scenario)


def test_unversioned_config_write_reports_adjustment(
    tmp_path: Path,
) -> None:
    """旧调用（不带 base_revision）仍可写；规范化字段必须返回给前端。"""

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {
            "whitelist_sessions": ["a", "a"],
        }
        result = await webapi_module(main)._api_post_config(plugin)
        assert result["ok"] is True
        assert "whitelist_sessions" in result["adjusted_fields"]
        assert plugin.settings.whitelist == {"a"}

    with_plugin(tmp_path, scenario)


def test_get_config_enabled_is_persisted_value(tmp_path: Path) -> None:
    """GET config 的 enabled 必须是持久值，runtime_enabled 单独暴露。

    ``/off`` 会同时落盘 ``enabled``，故此处不用 ``/off`` 举例，
    改为直接构造「两者分叉」这个状态：webapi 仍须分开暴露，否则前端全量保存会把
    运行态固化成持久配置。分叉现在由 POST config 提交相同 enabled 值时产生。
    """

    async def scenario(plugin, main):
        plugin.runtime_enabled = False  # 直接构造分叉态（不再等同于 /off）
        cfg = await webapi_module(main)._api_get_config(plugin)
        # 修复前：enabled 返回 runtime_enabled=False → 前端全量保存会固化关闭（红灯）
        assert cfg["enabled"] is plugin.settings.enabled
        assert cfg["enabled"] is True
        assert cfg["runtime_enabled"] is False

    with_plugin(tmp_path, scenario)


# ============================================================================
# 同会话并发互斥
# ============================================================================


def test_concurrent_checks_are_mutexed(tmp_path: Path) -> None:
    """同一会话并发两个 _check_session：第二个必须被拒，配额只计一次。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        class Runner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        entered = asyncio.Event()

        async def build_effect(kwargs, result):
            entered.set()
            await asyncio.sleep(0.1)  # 第一个占住 _running_sessions，让第二个进入
            return FakeBuildResult(
                agent_runner=Runner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        try:
            results = await asyncio.gather(
                plugin._pipeline.check_session(UMO, trigger="patrol", force=True),
                plugin._pipeline.check_session(UMO, trigger="patrol", force=True),
            )
            rejected = [r for r in results if "已有判断任务在运行" in r]
            accepted = [r for r in results if "已有判断任务在运行" not in r]
            assert len(rejected) == 1, f"expected one rejection, got {results}"
            assert len(accepted) == 1
            # 只有一次实际执行：配额只计一次（修复前假并发测试掩盖此语义）
            assert state.daily_count == 1
        finally:
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# 非 force 检查的白名单闸门
# ============================================================================


def test_non_force_check_rejected_for_non_whitelisted_session(tmp_path: Path) -> None:
    """非白名单会话的非 force 检查必须被闸门拒绝，不进入决策管线。"""

    async def scenario(plugin, main):
        plugin._last_events[UMO] = _make_event()
        plugin._last_event_at[UMO] = 1.0
        plugin.settings.whitelist = set()
        result = await plugin._pipeline.check_session(UMO, trigger="patrol", force=False)
        assert result == "会话不在主动回复白名单。"

    with_plugin(tmp_path, scenario)


# ============================================================================
# 权限与配置键
# ============================================================================


def test_non_admin_write_command_does_not_cancel(tmp_path: Path) -> None:
    """非管理员发写指令：权限拒绝先行，在途延迟检查不得被取消。

    修复前 on_message 命令分支无条件 _cancel_event_session（白名单会话）
    → 任务被取消（红灯）。
    """

    async def scenario(plugin, main):
        event = _make_event(message_str="/selfreply add")
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin.settings.whitelist = {UMO}
        plugin._scheduler.schedule_delayed_check(
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
# 管理员写指令取消、只读指令不取消
# ============================================================================


def test_admin_write_cancels_but_read_does_not(tmp_path: Path) -> None:
    """管理员视角：只读（status）不打断进行中的检查，写（add）才取消。

    修复前只读指令同样被无条件取消 → status 后任务被取消（红灯）。
    """

    async def scenario(plugin, main):
        plugin.settings.whitelist = {UMO}
        plugin._scheduler.schedule_delayed_check(
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
# webapi 新键真实解析（防静默吞字段）
# ============================================================================


def test_new_config_keys_take_effect(tmp_path: Path) -> None:
    """POST 13 个规范键 + decision_history_min_messages 必须真实写入 settings。

    修复前这些键无处理分支 → ok:true 但 settings 不变（虚假绿灯，红灯）。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        assert plugin._scheduler.patrol_task is None, "前置：巡检未在跑"
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
        result = await webapi_module(main)._api_post_config(plugin)
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
        # 拓扑同步：只写 settings 不够，「保存成功、状态显示开启、实际巡检
        # 不跑」会持续到下次重启，且无任何日志。POST /config 是运行期改这些
        # 键的唯一入口（官方 Dashboard 走整插件 reload）。
        await asyncio.sleep(0)
        assert plugin._scheduler.patrol_task is not None, "开启巡检后未启动巡检循环"

    with_plugin(tmp_path, scenario)


# ============================================================================
# webapi 未知键 fail loud
# ============================================================================


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    """schema 之外的键必须被拒并列出未知键，而不是静默返回 ok:true。

    修复前未知键被忽略 → ok:true（虚假成功，红灯）。
    """

    async def scenario(plugin, main):
        web = sys.modules["astrbot.api.web"]
        web.request.payload = {"bogus_setting": 1}
        result = await webapi_module(main)._api_post_config(plugin)
        assert result.get("ok") is False
        assert "bogus_setting" in str(result.get("error", ""))

    with_plugin(tmp_path, scenario)


# ============================================================================
# 会话状态显式化
# ============================================================================


def test_aba_old_task_does_not_revive_after_re_add(tmp_path: Path) -> None:
    """会话移除后立即重加：运行中的旧任务必须被代次门拦截，不发送不记录。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._gate.advance(UMO)  # 真实会话：新消息已推进过代次
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        class Runner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        entered = asyncio.Event()

        async def build_effect(kwargs, result):
            entered.set()
            await asyncio.sleep(0.2)  # 旧任务在 build 中挂起，期间发生 ABA
            return FakeBuildResult(
                agent_runner=Runner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        try:
            task = asyncio.create_task(
                plugin._pipeline.check_session(UMO, trigger="patrol", force=True)
            )
            await entered.wait()
            # 会话运行中：白名单移除（级联失效+prune）→ 立即重加（新代次）
            await plugin._remove_whitelist_session(UMO)
            assert UMO not in plugin._last_events
            await plugin._add_whitelist_session(UMO)

            result = await task

            # 旧任务被代次门拦截：不发送、不记录任何状态
            assert "会话已经更新" in result or "放弃旧任务" in result, result
            assert state.last_proactive_at == 0.0
            assert state.daily_count == 0
        finally:
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# 会话失效清空观察素材
# ============================================================================


def test_invalidate_clears_observation_material(tmp_path: Path) -> None:
    """记录事件后会话持有观察素材；invalidate 必须清空事件表并推进代次。

    「持有观察素材」以事件表为准（_last_events/_last_event_at），不经任何
    阶段投影，残留一条就足以让下一轮决策拿到已失效会话的旧消息。
    """

    async def scenario(plugin, main):
        event = _make_event()
        plugin._coordinator.record_event(UMO, event, 1.0)
        plugin._coordinator.capture_images(UMO, 1.0, [])
        assert plugin._last_events is plugin._coordinator._events
        assert plugin._last_event_at is plugin._coordinator._event_at
        assert plugin._recent_image_events is plugin._coordinator._images
        assert UMO in plugin._last_events
        assert UMO in plugin._recent_image_events

        before = plugin._gate.current(UMO)
        plugin._coordinator.invalidate(UMO)

        assert UMO not in plugin._last_events
        assert UMO not in plugin._last_event_at
        assert UMO not in plugin._recent_image_events
        assert plugin._gate.current(UMO) > before

    with_plugin(tmp_path, scenario)


# ============================================================================
# 指令在降级/在途态的口径一致（§6 越权拒绝先行、文案分流）
# ============================================================================


def test_on_command_is_rejected_when_degraded(tmp_path: Path) -> None:
    """降级态下 ``/selfreply on`` 不得谎报"已启用"。

    缺陷形态：check 分支有精准文案（"插件已降级…需重启插件恢复"），而 on 分支
    完全没有生命周期门，降级态下回"主动回复插件已启用（重启后保持）"，同时把
    ``settings.enabled`` 写成 True。用户以为已恢复，实际该实例仍拒绝一切新任务
    （含 force check），巡检也不会重启，恢复只能靠重载插件。
    """
    from types import SimpleNamespace

    async def scenario(plugin, main):
        commands = sys.modules[f"{PACKAGE_NAME_R3}.commands"]
        plugin._mark_degraded("probe")
        event = SimpleNamespace(unified_msg_origin=UMO)
        text = await commands.dispatch_command_action(plugin, event, "on")
        assert "降级" in text, f"降级态 /on 未按降级文案拒绝，实际：{text}"
        assert "已启用" not in text, f"降级态 /on 谎报已启用：{text}"

    with_plugin(tmp_path, scenario)


def test_check_command_waits_for_previous_run_release(tmp_path: Path) -> None:
    """在途检查时 ``/selfreply check`` 必须等在途运行让出标记，而不是自拒。

    缺陷形态：check 先 ``invalidate(force_cancel=True)`` 取消旧检查，再立即进
    pipeline；取消是异步投递的，旧任务尚未 ``unmark_running``，于是撞上
    「已有判断任务在运行」净效果是旧检查被静默掐掉、本次也没执行，用户必须
    重发一次。
    """

    async def scenario(plugin, main):
        commands = sys.modules[f"{PACKAGE_NAME_R3}.commands"]
        event = _make_event()
        plugin.settings.whitelist = {UMO}
        plugin.settings.min_silence_sec = 0
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = main.now_ts()
        plugin._state_for(UMO).last_active_at = main.now_ts() - 300

        # 模拟"旧检查正在运行且需要一轮事件循环才让出"
        plugin._gate.mark_running(UMO)

        async def release_soon() -> None:
            await asyncio.sleep(0)
            plugin._gate.unmark_running(UMO)

        asyncio.ensure_future(release_soon())

        text = await commands.dispatch_command_action(plugin, event, "check")
        assert "已有判断任务在运行" not in text, f"check 自拒了，旧检查被取消而本次未执行：{text}"

    with_plugin(tmp_path, scenario)


# ============================================================================
# Agent 管线装配与宿主交互路径（build/run 效应、闸门恢复、_call_compat）
# ============================================================================


def _load_main():
    import tests.test_vision as vision

    from .host_stubs import install_astrbot_stubs

    # 本文件可独立运行：stub 安装不能依赖 test_vision 先跑（排序依赖），
    # 加载前显式安装，幂等，重复调用无副作用。
    install_astrbot_stubs()
    root = Path(vision.ROOT)
    package = vision.PACKAGE_NAME
    if package not in sys.modules:
        module = __import__("types").ModuleType(package)
        module.__path__ = [str(root)]
        sys.modules[package] = module
    return importlib.import_module(f"{package}.main")


# ============================================================================
# 0.1 P0：run_task 孤儿泄漏
# ============================================================================


def test_force_cancel_converges_agent_run_task(tmp_path: Path) -> None:
    """运行中检查被 force cancel：run_task 必须被收敛，request_stop 必须被调。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    stop_called: list[bool] = []
    run_finished: list[bool] = []

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        entered = asyncio.Event()

        class HangingRunner:
            def reset(self, **_):
                return _FakeResetCoro()

            def request_stop(self):
                stop_called.append(True)

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="", result_chain=None)

            def close(self):
                pass

        async def build_effect(kwargs, result):
            return FakeBuildResult(
                agent_runner=HangingRunner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                try:
                    entered.set()
                    await asyncio.sleep(3600)  # 永不结束的 run_agent
                    yield None
                finally:
                    run_finished.append(True)

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_grace = main.GRACEFUL_STOP_GRACE_SEC
        main.GRACEFUL_STOP_GRACE_SEC = 0.05
        try:
            task = asyncio.create_task(
                plugin._generation.generate(
                    UMO, plugin._state_for(UMO), expected_generation=1, force=True
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()  # 模拟 /off / terminate 等 force cancel
            try:
                await task
            except asyncio.CancelledError:
                pass
            # 修复前：run_task 未被 shield 收敛，成为孤儿继续运行（红灯）
            assert run_finished == [True]
            assert stop_called == [True]
        finally:
            main.GRACEFUL_STOP_GRACE_SEC = original_grace
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario, generation_timeout_sec=60)


def test_force_cancel_kills_running_check_task(tmp_path: Path) -> None:
    """scheduler.cancel_delay(force=True) 必须同时取消运行中的检查任务。

    变异锚定：cancel_delay_task 的 force 分支失效（不取消 running_task）
    后本测试必须变红。
    """

    async def scenario(plugin, main):
        started = asyncio.Event()

        async def hanging():
            started.set()
            await asyncio.sleep(3600)

        running = asyncio.create_task(hanging())
        await started.wait()
        plugin._running_check_tasks[UMO] = running
        delay_task = asyncio.create_task(asyncio.sleep(3600))
        plugin._delay_tasks[UMO] = delay_task

        plugin._scheduler.cancel_delay(UMO, force=True)
        # 事件驱动等待 cancel 生效，替代单次 sleep(0)（flaky 修复）
        await until(lambda: running.done() and delay_task.done())

        assert running.done() and running.cancelled()
        assert delay_task.done() and delay_task.cancelled()
        assert UMO not in plugin._delay_tasks
        for t in (running, delay_task):  # 变异下兜底取消，避免 gather 长挂
            if not t.done():
                t.cancel()
        await asyncio.gather(running, delay_task, return_exceptions=True)

    with_plugin(tmp_path, scenario)


def test_delayed_check_waits_for_running_session_release(tmp_path: Path) -> None:
    """延迟检查须等待前一个 check 结束（事件驱动，非轮询）。"""

    async def scenario(plugin, main):
        entered_wait = asyncio.Event()
        original_release = plugin._gate.release_event

        def patched_release(umo):
            ev = original_release(umo)
            entered_wait.set()  # 确定性锚点：B 已挂起在等待上
            return ev

        plugin._gate.release_event = patched_release
        plugin._gate.mark_running(UMO)  # 前一个 check 占住运行集
        try:
            task = asyncio.create_task(
                plugin._scheduler.delayed_check(
                    UMO,
                    delay_sec=0,
                    trigger="patrol",
                    force=True,
                    generation=plugin._gate.advance(UMO),
                )
            )
            await asyncio.wait_for(entered_wait.wait(), timeout=2)
            assert not task.done(), "运行集被占用时延迟检查不应完成"
            plugin._gate.unmark_running(UMO)  # 前一个 check 结束
            # 用 asyncio.wait（而非 wait_for）：task 吞掉取消时 wait_for 会
            # 正常返回（Python 3.12+ 行为），导致变异不被捕获。
            done, _pending = await asyncio.wait({task}, timeout=2)
            assert task in done, "释放后延迟检查应完成"
        finally:
            plugin._gate.release_event = original_release

    with_plugin(tmp_path, scenario)


def test_prune_wakes_waiting_delayed_check(tmp_path: Path) -> None:
    """移出白名单时 gate.prune 须唤醒挂起在运行释放上的延迟检查。"""

    async def scenario(plugin, main):
        entered_wait = asyncio.Event()
        original_release = plugin._gate.release_event

        def patched_release(umo):
            ev = original_release(umo)
            entered_wait.set()
            return ev

        plugin._gate.release_event = patched_release
        plugin._gate.mark_running(UMO)  # 模拟正在运行的 check 占住运行集
        try:
            task = asyncio.create_task(
                plugin._scheduler.delayed_check(
                    UMO,
                    delay_sec=0,
                    trigger="patrol",
                    force=True,
                    generation=plugin._gate.advance(UMO),
                )
            )
            await asyncio.wait_for(entered_wait.wait(), timeout=2)
            assert not task.done(), "白名单移除前延迟检查应等待"
            plugin._whitelist.replace(set())  # 移出全部会话
            done, _pending = await asyncio.wait({task}, timeout=2)
            assert task in done, "白名单移除后挂起的延迟检查应被唤醒退出"
        finally:
            plugin._gate.release_event = original_release

    with_plugin(tmp_path, scenario)


def test_stale_generation_rejected_at_session_entry(tmp_path: Path) -> None:
    """旧代次任务在会话入口即被放弃，不进入决策与发送。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        token = plugin._gate.advance(UMO)
        plugin._gate.advance(UMO)  # 抬代次使 token 过期
        result = await plugin._pipeline.check_session(
            UMO, trigger="patrol", force=True, expected_generation=token
        )
        assert result == "会话已经更新，放弃旧任务。"

    with_plugin(tmp_path, scenario)


def test_force_cancel_converges_before_grace_timeout(tmp_path: Path) -> None:
    """显式 cancel 应在 grace 超时前收敛 run_task（第一层保险的时序守卫）。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    run_finished: list[bool] = []

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        entered = asyncio.Event()

        class HangingRunner:
            def reset(self, **_):
                return _FakeResetCoro()

            def request_stop(self):
                pass

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="", result_chain=None)

            def close(self):
                pass

        async def build_effect(kwargs, result):
            return FakeBuildResult(
                agent_runner=HangingRunner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                try:
                    entered.set()
                    await asyncio.sleep(3600)  # 永不结束的 run_agent
                    yield None
                finally:
                    run_finished.append(True)

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_grace = main.GRACEFUL_STOP_GRACE_SEC
        # 30s 宽限期：若缺少显式 cancel（第一层保险），收敛只能等 grace 超时
        main.GRACEFUL_STOP_GRACE_SEC = 30
        try:
            task = asyncio.create_task(
                plugin._generation.generate(
                    UMO, plugin._state_for(UMO), expected_generation=1, force=True
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()  # 模拟 /off / terminate 等 force cancel
            await asyncio.sleep(0.5)  # 显式 cancel 应立即收敛，无需等待 30s grace
            assert run_finished == [True], "run_task 未在 grace 超时前收敛（显式 cancel 兜底缺失）"
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            main.GRACEFUL_STOP_GRACE_SEC = original_grace
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario, generation_timeout_sec=60)


# ============================================================================
# 0.2 P1：context 兜底发送误记 UNKNOWN
# ============================================================================


def test_context_send_none_is_delivered_and_writes_history(tmp_path: Path) -> None:
    """context 兜底发送正常完成（返回 None）：记 DELIVERED 并写入 assistant 历史。"""

    from types import SimpleNamespace

    from .host_stubs import FakeBuildResult, _FakeResetCoro

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        state = plugin._state_for(UMO)
        state.last_active_at = main.now_ts() - 300

        sent_via_context: list[tuple[str, object]] = []

        async def ctx_send(umo_, message):
            sent_via_context.append((umo_, message))
            return None  # 真实宿主 Context.send_message 正常完成返回 None

        plugin.context.send_message = ctx_send

        class Runner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="你好呀", result_chain=None)

            def close(self):
                pass

        async def build_effect(kwargs, result):
            return FakeBuildResult(
                agent_runner=Runner(),
                provider_request=kwargs["req"],
                provider=None,
                reset_coro=_FakeResetCoro(),
            )

        def run_effect(_runner, **_kwargs):
            async def gen():
                # 模拟生成期间事件被清理：send_reply 走 context 兜底路径
                plugin._coordinator.clear_event(
                    UMO,
                    expected_active_at=plugin._last_event_at.get(UMO),
                )
                yield None

            return gen()

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        try:
            result = await plugin._pipeline.check_session(UMO, trigger="patrol", force=True)
            # 修复前：None 被记 UNKNOWN → "主动发送状态未知，未自动重试。"（红灯）
            assert "已主动回复" in result
            assert sent_via_context
            assert state.last_proactive_text == "你好呀"
            assert state.recent[-1].role == "assistant"
            assert state.daily_count == 1
        finally:
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# 0.3 P1：只读命令误失效会话
# ============================================================================


def test_readonly_commands_do_not_invalidate_session(tmp_path: Path) -> None:
    """status 只读查询不得取消待执行的延迟检查，也不得清空事件缓存。"""

    async def scenario(plugin, main):
        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._state_for(UMO)
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        task = plugin._delay_tasks.get(UMO)
        assert task is not None and not task.done()

        await plugin._command_text(event, "status")
        # 修复前：status 也 invalidate → 延迟任务被取消移除、缓存被清（红灯）
        assert plugin._delay_tasks.get(UMO) is task
        assert not task.done()
        assert plugin._last_events.get(UMO) is event
        assert plugin._last_event_at.get(UMO) == 1.0

    with_plugin(tmp_path, scenario)


# ============================================================================
# 0.4 P2：配置回滚不恢复任务拓扑
# ============================================================================


def test_config_rollback_restores_task_topology(tmp_path: Path) -> None:
    """禁用路径 stop_patrol 失败回滚后，patrol 任务必须恢复运行。"""

    async def scenario(plugin, main):
        plugin._scheduler.ensure_patrol()
        assert plugin._scheduler.patrol_task is not None
        assert not plugin._scheduler.patrol_task.done()

        original_stop = plugin._scheduler.stop_patrol

        async def failing_stop():
            await original_stop()
            raise OSError("stop patrol failed")

        plugin._scheduler.stop_patrol = failing_stop
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"enabled": False}
            result = await webapi_module(main)._api_post_config(plugin)
            assert result.get("ok") is False
            # 修复前：回滚只恢复 settings/runtime_enabled，不重启 patrol（红灯）
            assert plugin.runtime_enabled is True
            assert plugin._scheduler.patrol_task is not None
            assert not plugin._scheduler.patrol_task.done()
        finally:
            plugin._scheduler.stop_patrol = original_stop

    with_plugin(tmp_path, scenario, enabled_patrol_trigger=True)


def test_config_rollback_reschedules_cancelled_delayed_checks(tmp_path: Path) -> None:
    """回滚后按快照重建被取消的延迟检查（message_delay 语义）。"""

    async def scenario(plugin, main):
        plugin._state_for(UMO)
        plugin._scheduler.schedule_delayed_check(
            UMO, delay_sec=None, trigger="message_delay", force=False
        )
        original_task = plugin._delay_tasks.get(UMO)
        assert original_task is not None and not original_task.done()

        original_stop = plugin._scheduler.stop_patrol

        async def failing_stop():
            await original_stop()
            raise OSError("stop patrol failed")

        plugin._scheduler.stop_patrol = failing_stop
        try:
            web = sys.modules["astrbot.api.web"]
            web.request.payload = {"enabled": False}
            result = await webapi_module(main)._api_post_config(plugin)
            assert result.get("ok") is False
            new_task = plugin._delay_tasks.get(UMO)
            assert new_task is not None and not new_task.done(), "回滚后延迟检查未重建"
        finally:
            plugin._scheduler.stop_patrol = original_stop

    with_plugin(tmp_path, scenario)

    with_plugin(tmp_path, scenario, enabled_patrol_trigger=True)


def test_gate_restore_recovers_running_set(tmp_path: Path) -> None:
    """restore 必须恢复运行集快照，否则回滚后运行标记漂移。

    变异锚定：session_gate.restore 删除 ``self._running_sessions = snap["running"]``
    后本测试必须变红。
    """

    async def scenario(plugin, main):
        gate = plugin._gate
        gate.mark_running(UMO)
        snap = gate.snapshot()
        gate.unmark_running(UMO)
        gate.mark_running("other:session")
        gate.restore(snap)
        assert gate.is_running(UMO) is True
        assert gate.is_running("other:session") is False

    with_plugin(tmp_path, scenario)


def test_gate_restore_clears_stale_release_for_still_running(tmp_path: Path) -> None:
    """回滚后仍标记运行中的会话：陈旧的 release set 必须清掉，事件身份必须不变。

    ``unmark_running`` 只 set 不 pop，所以回滚把运行标记恢复成快照态后，表里
    那个事件仍是已 set 的。此时 scheduler 的 ``while is_running: await
    release_event(umo).wait()`` 每轮立即返回，紧密空转独占事件循环，整个 bot
    卡死。

    变异锚定：删除 ``restore`` 中的 ``release.clear()`` 分支后本测试必须变红。
    """

    async def scenario(plugin, main):
        gate = plugin._gate
        gate.mark_running(UMO)
        waiter_event = gate.release_event(UMO)  # 等待者持有此对象
        snap = gate.snapshot()
        gate.unmark_running(UMO)  # set 但不 pop
        assert waiter_event.is_set()

        gate.restore(snap)

        assert gate.is_running(UMO) is True
        assert gate.release_event(UMO) is waiter_event, "等待者持有的事件被换掉（孤儿事件）"
        assert not waiter_event.is_set(), "陈旧 set 未清除：scheduler 将空转饿死事件循环"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(waiter_event.wait(), timeout=0.02)

    with_plugin(tmp_path, scenario)


def test_gate_restore_wakes_waiter_for_no_longer_running(tmp_path: Path) -> None:
    """回滚后不再运行的会话：等待者必须被唤醒，否则它等一个不会到来的信号。

    变异锚定：删除 ``restore`` 中的 ``release.set()`` 分支后本测试必须变红。
    """

    async def scenario(plugin, main):
        gate = plugin._gate
        snap = gate.snapshot()  # 快照时该会话未运行
        gate.mark_running(UMO)
        waiter_event = gate.release_event(UMO)
        assert not waiter_event.is_set()

        gate.restore(snap)

        assert gate.is_running(UMO) is False
        assert waiter_event.is_set(), "等待者未被唤醒：该会话主动回复静默死亡"
        await asyncio.wait_for(waiter_event.wait(), timeout=0.02)

    with_plugin(tmp_path, scenario)


# ============================================================================
# 0.5 P2：_call_compat TypeError 重试双执行
# ============================================================================


def test_call_compat_does_not_retry_body_type_error() -> None:
    """函数体内部抛 TypeError（与签名无关）：只调用一次，不触发 minimal 重试。"""

    main = _load_main()
    adapters = importlib.import_module(f"{main.__package__}.adapters")
    calls: list[str] = []

    def func(prompt, **rest):
        calls.append(prompt)
        raise TypeError("internal boom")  # 函数体内部 TypeError，非签名不匹配

    with pytest.raises(TypeError):
        asyncio.run(
            adapters.AstrBotBridge._call_compat(
                func,
                kwargs={"prompt": "x", "temperature": 0.5},
                minimal_kwargs={"prompt": "x"},
            )
        )
    # 修复前：TypeError 触发 minimal 重试 → 调用两次（对 LLM 即重复计费）（红灯）
    assert calls == ["x"]
