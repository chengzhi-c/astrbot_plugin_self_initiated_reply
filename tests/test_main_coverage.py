"""main.py 覆盖补盲（ticket 08：60% → 80%）。

按缺失行分组补齐关键路径与分支：
- on_message 门卫（命令已处理/禁用/非白名单/忽略/空文本/纯图/图片捕获）
- _prepare_images_for_session 出口（陈旧代次/超时/异常/全部失败）
- 图片 parser 缓存与描述上下文构建
- check 流程守卫与判断早退分支、直发文本重复抑制
- 命令文本与全部子命令处理器（含 stopping 拒绝、非白名单回收）
- 管理员热读缓存与路径解析分支、事件 extra 容错

补盲原则：优先走真实逻辑，仅对不可控外部依赖（图片下载/parser 网络）注入。
"""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

from .host_stubs import until, with_plugin
from .test_main_runtime import UMO, _make_event, _PipelineTestAdapter


def _image():
    return type(
        "Image",
        (),
        {
            "url": "https://cdn.example.test/cat.png",
            "local_path": "C:\\media\\cat.png",
            "message_id": "m1",
            "sender_id": "u1",
            "cache_key": lambda self: self.url,
        },
    )()


def _image_event(message_str: str = "", umo: str = UMO):
    event = _make_event(message_str=message_str, umo=umo)
    event.get_messages = lambda: [_image()]
    return event


class _FakeParser:
    """图片解析桩：snapshot/prepare/parse 全可控。"""

    def __init__(self, batch=None, parse=None, exc=None, hang: bool = False) -> None:
        self._batch = batch
        self._parse = parse
        self._exc = exc
        self._hang = hang
        self.prepared: list | None = None

    async def snapshot_local_sources(self, images, *, max_concurrent=2) -> None:
        return None

    async def prepare_batch(self, images, max_concurrent=2):
        if self._hang:
            await asyncio.sleep(60)
        if self._exc is not None:
            raise self._exc
        self.prepared = list(images)
        return self._batch if self._batch is not None else [True] * len(images)

    async def parse_batch(self, images, *, umo="", max_concurrent=2):
        if self._parse is not None:
            return self._parse(images)
        return [f"图 {index}" for index in range(len(images))]


async def _consume(gen):
    return await gen.__anext__()


# ============================================================================
# on_message 门卫分支
# ============================================================================


def test_on_message_early_return_when_command_handled(tmp_path: Path) -> None:
    """事件已标记为本插件命令处理后，on_message 不再消费。"""

    async def scenario(plugin, main):
        models = importlib.import_module(main.__package__ + ".models")
        event = _make_event(message_str="/selfreply help")
        event.set_extra(models.COMMAND_HANDLED_KEY, True)
        await plugin.on_message(event)
        assert UMO not in plugin._last_events
        assert UMO not in plugin._delay_tasks

    with_plugin(tmp_path, scenario)


def test_on_message_skips_when_disabled(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.runtime_enabled = False
        await plugin.on_message(_make_event(message_str="今天天气不错"))
        assert UMO not in plugin._last_events

    with_plugin(tmp_path, scenario)


def test_on_message_skips_non_whitelisted_session(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        await plugin.on_message(_make_event(umo="fake:group:999", message_str="你好"))
        assert "fake:group:999" not in plugin._last_events

    with_plugin(tmp_path, scenario)


def test_on_message_ignored_sender_invalidates_session(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        event = _make_event(message_str="忽略我", sender_id="spammer")
        plugin._coordinator.record_event(UMO, event, 1.0)
        plugin.settings.ignored_sender_ids = {"spammer"}
        await plugin.on_message(event)
        assert UMO not in plugin._last_events  # 失效级联清掉了缓存事件

    with_plugin(tmp_path, scenario)


def test_on_message_ignored_direct_call_still_tracks_activity(tmp_path: Path) -> None:
    """被忽略的直接点名仍推进 last_active（避免被巡逻误判为静默）。"""

    async def scenario(plugin, main):
        event = _make_event(message_str="忽略我")
        event.is_at_or_wake_command = True
        plugin.settings.ignored_sender_ids = {"spammer"}
        event.get_sender_id = lambda: "spammer"
        await plugin.on_message(event)
        state = plugin._state_for(UMO)
        assert state.last_active_at > 0

    with_plugin(tmp_path, scenario)


def test_on_message_empty_text_and_no_images_invalidates(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        event = _make_event(message_str="   ")
        plugin._coordinator.record_event(UMO, event, 1.0)
        await plugin.on_message(event)
        assert UMO not in plugin._last_events

    with_plugin(tmp_path, scenario)


def test_on_message_image_only_event_records_placeholder(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = True
        plugin._get_image_parser = lambda *a, **k: _FakeParser(batch=[True])
        event = _image_event(message_str="")
        await plugin.on_message(event)
        state = plugin._state_for(UMO)
        assert state.recent[-1].text == "[图片]"

    with_plugin(tmp_path, scenario)


# ============================================================================
# 图片捕获与 parser 缓存
# ============================================================================


def test_on_message_captures_images_in_background(tmp_path: Path) -> None:
    """图片事件：快照后由后台任务写入会话图片索引。"""

    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = True
        plugin.settings.enabled_message_trigger = False
        parser = _FakeParser(batch=[True])
        plugin._get_image_parser = lambda *a, **k: parser
        await plugin.on_message(_image_event(message_str="看看这张图"))
        await until(lambda: UMO in plugin._recent_image_events)
        events = plugin._recent_image_events[UMO]
        assert len(events) == 1
        assert len(events[0][1]) == 1

    with_plugin(tmp_path, scenario)


def test_prepare_images_stale_generation_skips_capture(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        parser = _FakeParser(batch=[True])
        plugin._get_image_parser = lambda *a, **k: parser
        token = plugin._gate.advance(UMO)
        plugin._gate.advance(UMO)  # 会话已更新，旧 token 失效
        await plugin._prepare_images_for_session(
            UMO, generation=token, active_at=1.0, images=[_image()]
        )
        assert UMO not in plugin._recent_image_events

    with_plugin(tmp_path, scenario)


def test_prepare_images_timeout_warns_without_capture(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        parser = _FakeParser(batch=[True], exc=asyncio.TimeoutError())
        plugin._get_image_parser = lambda *a, **k: parser
        token = plugin._gate.advance(UMO)
        await plugin._prepare_images_for_session(
            UMO, generation=token, active_at=1.0, images=[_image()]
        )
        assert UMO not in plugin._recent_image_events

    with_plugin(tmp_path, scenario)


def test_prepare_images_error_warns_without_capture(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        parser = _FakeParser(batch=[True], exc=RuntimeError("download failed"))
        plugin._get_image_parser = lambda *a, **k: parser
        token = plugin._gate.advance(UMO)
        await plugin._prepare_images_for_session(
            UMO, generation=token, active_at=1.0, images=[_image()]
        )
        assert UMO not in plugin._recent_image_events

    with_plugin(tmp_path, scenario)


def test_prepare_images_all_failed_keeps_no_index(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        parser = _FakeParser(batch=[False])
        plugin._get_image_parser = lambda *a, **k: parser
        token = plugin._gate.advance(UMO)
        await plugin._prepare_images_for_session(
            UMO, generation=token, active_at=1.0, images=[_image()]
        )
        assert UMO not in plugin._recent_image_events

    with_plugin(tmp_path, scenario)


def test_get_image_parser_cache_shared_and_timeout_rebuild(tmp_path: Path) -> None:
    """同 provider 共享实例；超时值变化整体重建；vision 关闭返回 None。"""

    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = True
        plugin.settings.vision_timeout_sec = 5
        first = plugin._get_image_parser("v1")
        assert plugin._get_image_parser("v1") is first
        assert plugin._get_image_parser("") is not first  # 不同 provider 键
        plugin.settings.vision_timeout_sec = 30
        assert plugin._get_image_parser("v1") is not first
        plugin.settings.vision_main_enabled = False
        assert plugin._get_image_parser("v1") is None

    with_plugin(tmp_path, scenario)


def test_build_image_context_describes_recent_images(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = True
        plugin.settings.vision_max_images = 2
        plugin._get_image_parser = lambda *a, **k: _FakeParser(parse=lambda images: ["一只猫"])
        package = main.__package__
        models = importlib.import_module(package + ".models")
        plugin._coordinator.capture_images(UMO, models.now_ts(), [_image()])

        context = await plugin._build_image_context(UMO, enabled=True, provider_id="v1")
        assert "一只猫" in context

        assert await plugin._build_image_context(UMO, enabled=False) == ""
        plugin._coordinator.clear(UMO)
        assert await plugin._build_image_context(UMO, enabled=True) == ""

    with_plugin(tmp_path, scenario)


def test_build_image_context_empty_descriptions_yield_empty(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = True
        plugin._get_image_parser = lambda *a, **k: _FakeParser(parse=lambda images: [""])
        package = main.__package__
        models = importlib.import_module(package + ".models")
        plugin._coordinator.capture_images(UMO, models.now_ts(), [_image()])
        assert await plugin._build_image_context(UMO, enabled=True) == ""

    with_plugin(tmp_path, scenario)


# ============================================================================
# check 流程守卫与判断早退
# ============================================================================


def test_session_check_guard_reasons(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        assert (
            plugin._session_check_guard(UMO, force=False, expected_generation=None)
            == "没有可用的最近消息事件。"
        )
        assert plugin._session_check_guard(UMO, force=True, expected_generation=None) is None
        plugin._stopping = True
        assert (
            plugin._session_check_guard(UMO, force=False, expected_generation=None)
            == "插件未启用。"
        )
        plugin._stopping = False
        other = "fake:group:999"
        assert (
            plugin._session_check_guard(other, force=False, expected_generation=None)
            == "会话不在主动回复白名单。"
        )
        assert plugin._session_check_guard(other, force=True, expected_generation=None) is None
        plugin._gate.mark_running(UMO)
        assert (
            plugin._session_check_guard(UMO, force=True, expected_generation=None)
            == "已有判断任务在运行。"
        )
        plugin._gate.unmark_running(UMO)
        assert (
            plugin._session_check_guard(UMO, force=False, expected_generation=None)
            == "没有可用的最近消息事件。"
        )

    with_plugin(tmp_path, scenario)


def test_decide_session_reply_early_reason_passthrough(tmp_path: Path) -> None:
    """判断模型给出字符串早退原因时原样返回。"""

    async def scenario(plugin, main):
        original_decide = plugin._decision.decide

        async def fake_decide(*args, **kwargs):
            return "判断模型解析失败"

        plugin._decision.decide = fake_decide
        try:
            state = plugin._state_for(UMO)
            result = await plugin._decide_session_reply(
                UMO, state, trigger="message_delay", force=False, expected_generation=None
            )
            assert result == "判断模型解析失败"
        finally:
            plugin._decision.decide = original_decide

    with_plugin(tmp_path, scenario)


def test_decide_session_reply_stale_generation_rejected(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        token = plugin._gate.advance(UMO)
        plugin._gate.advance(UMO)
        state = plugin._state_for(UMO)
        result = await plugin._decide_session_reply(
            UMO, state, trigger="manual", force=True, expected_generation=token
        )
        assert result == "会话已经更新，放弃旧任务。"

    with_plugin(tmp_path, scenario)


def test_decide_session_reply_no_reply_returns_reason(tmp_path: Path) -> None:
    """真实链路：判断模型关闭且无明确请求时给出明确早退原因。"""

    async def scenario(plugin, main):
        state = plugin._state_for(UMO)
        result = await plugin._decide_session_reply(
            UMO, state, trigger="message_delay", force=False, expected_generation=None
        )
        assert result.startswith("判断不回复：")

    with_plugin(tmp_path, scenario)


def test_check_suppresses_duplicate_direct_text(tmp_path: Path) -> None:
    """直发文本与最终回复一致时抑制最终回复（避免重复发送）。"""

    async def scenario(plugin, main):
        from .host_stubs import FakeBuildResult, _FakeMessageChain, _FakeResetCoro

        event = _make_event()
        plugin._last_events[UMO] = event
        plugin._last_event_at[UMO] = 1.0
        plugin._gate.advance(UMO)

        class Runner:
            def reset(self, **_):
                return _FakeResetCoro()

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="重复文本", result_chain=None)

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
                # 直发一次与最终回复相同的文本（走真实直发收集路径）
                await event.send(_FakeMessageChain(type="tool_direct_result", chain=["重复文本"]))
                yield None

            return gen()

        async def fake_decide(*args, **kwargs):
            return {"should_reply": True, "reason": "手动", "elapsed_sec": 0.0}

        original_runtime = main._AGENT_RUNTIME
        main._AGENT_RUNTIME = _PipelineTestAdapter(
            original_runtime, build_effect=build_effect, run_effect=run_effect
        )
        original_decide = plugin._decision.decide
        plugin._decision.decide = fake_decide
        original_deliver = plugin._deliver_session_reply
        delivered: list[str] = []

        async def fake_deliver(umo, state, reply, direct_send_count, **kwargs):
            delivered.append(reply)
            return "已投递"

        plugin._deliver_session_reply = fake_deliver
        try:
            state = plugin._state_for(UMO)
            state.last_active_at = 1.0
            token = plugin._gate.advance(UMO)
            result = await plugin._check_session_locked(
                UMO, trigger="manual", force=True, expected_generation=token
            )
            assert result == "已投递"
            assert delivered == [""]  # 重复文本被抑制为空
        finally:
            plugin._deliver_session_reply = original_deliver
            plugin._decision.decide = original_decide
            main._AGENT_RUNTIME = original_runtime

    with_plugin(tmp_path, scenario)


# ============================================================================
# 协作壳与只读视图（逻辑在子模块，主类保留转发）
# ============================================================================


def test_delegate_shells_work(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        state = plugin._state_for(UMO)
        assert isinstance(plugin._scheduler.last_cleanup_at, float)
        assert plugin._patrol_task is None  # patrol 触发默认关闭，不启动任务
        assert isinstance(await plugin._run_image_cleanup(), int)
        assert isinstance(plugin._scheduler.remaining_silence_sec(state), float)
        plugin._generation.main_agent_build_config("")
        assert isinstance(plugin._decision.recent_reply_request_reason(state), str)
        assert plugin._recent_images_for(UMO) == []
        prompt = await plugin._decision.build_decision_prompt(UMO, state, "patrol")
        assert isinstance(prompt, str)

    with_plugin(tmp_path, scenario)


# ============================================================================
# 命令文本与子命令处理器
# ============================================================================


def test_command_text_help_list_and_unknown(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        event = _make_event()
        assert "主动回复" in await plugin._command_text(event, "help")
        assert await plugin._command_text(event, "unknown_action") == await plugin._command_text(
            event, "help"
        )
        assert "fake:group:123" in await plugin._command_text(event, "list")
        no_umo = _make_event(umo="")
        assert "无法识别当前会话" in await plugin._command_text(no_umo, "add")

    with_plugin(tmp_path, scenario)


def test_command_text_remove_flow(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        event = _make_event()
        await plugin._add_whitelist_session(UMO)
        text = await plugin._command_text(event, "remove")
        assert "已移出主动回复白名单" in text
        again = await plugin._command_text(event, "remove")
        assert "本不在主动回复白名单" in again

    with_plugin(tmp_path, scenario)


def test_command_text_check_flow_and_non_whitelist_recycle(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        original_check = plugin._check_session

        async def fake_check(*args, **kwargs):
            return "完成"

        plugin._check_session = fake_check
        try:
            event = _make_event(message_str="/selfreply check")
            text = await plugin._command_text(event, "check")
            assert text == "主动回复检查结果：完成"
            # check 是写操作：结束后回收缓存事件，但会话代次已推进
            assert plugin._last_events.get(UMO) is None
            assert UMO in plugin._session_generation

            other = "fake:group:999"
            event2 = _make_event(umo=other)
            plugin._gate.advance(other)
            text = await plugin._command_text(event2, "check")
            assert text == "主动回复检查结果：完成"
            assert other not in plugin._session_generation  # 非白名单会话已回收
        finally:
            plugin._check_session = original_check

    with_plugin(tmp_path, scenario)


def test_command_text_toggle_and_debug(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        event = _make_event()
        plugin.runtime_enabled = False
        assert "已临时启用" in await plugin._command_text(event, "on")
        assert plugin.runtime_enabled is True
        assert "已临时暂停" in await plugin._command_text(event, "off")
        assert plugin.runtime_enabled is False
        assert await plugin._command_text(event, "debug")

    with_plugin(tmp_path, scenario)


def test_whitelist_add_remove_raise_when_stopping(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin._stopping = True
        import pytest

        with pytest.raises(RuntimeError):
            await plugin._add_whitelist_session(UMO)
        with pytest.raises(RuntimeError):
            await plugin._remove_whitelist_session(UMO)

    with_plugin(tmp_path, scenario)


def test_subcommand_handlers_run(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        original_check = plugin._check_session

        async def fake_check(*args, **kwargs):
            return "完成"

        plugin._check_session = fake_check
        try:
            event = _make_event()
            assert (await _consume(plugin.selfreply_help(event))).text
            assert (await _consume(plugin.selfreply_status(event))).text
            assert "fake:group:123" in (await _consume(plugin.selfreply_list(event))).text
            await _consume(plugin.selfreply_add(event))
            assert UMO in plugin.settings.whitelist
            await _consume(plugin.selfreply_remove(event))
            assert UMO not in plugin.settings.whitelist
            text = await _consume(plugin.selfreply_check(event))
            assert text.text == "主动回复检查结果：完成"
            assert "已临时启用" in (await _consume(plugin.selfreply_on(event))).text
            assert "已临时暂停" in (await _consume(plugin.selfreply_off(event))).text
            assert (await _consume(plugin.selfreply_debug(event))).text
        finally:
            plugin._check_session = original_check

    with_plugin(tmp_path, scenario)


def test_send_command_text_falls_back_on_send_failure(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        event = _make_event()

        async def failing_send(_message):
            raise RuntimeError("send failed")

        def failing_stop():
            raise RuntimeError("stop failed")

        event.send = failing_send
        event.stop_event = failing_stop
        await plugin._send_command_text(event, "文本")
        assert event.get_result() is not None  # plain_result 兜底已设置

    with_plugin(tmp_path, scenario)


# ============================================================================
# 管理员热读与路径解析
# ============================================================================


def test_refresh_admin_ids_cache_and_bad_file(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        cmd_file = plugin._data_path / "cmd_config.json"
        cmd_file.parent.mkdir(parents=True, exist_ok=True)
        cmd_file.write_text('{"admins_id": ["a", "b"]}', encoding="utf-8")
        plugin._admin_probe_ts = 0.0  # __init__ 已消耗首探窗口，重置后强制重探
        assert plugin._refresh_admin_ids() == {"a", "b"}
        cached = plugin._refresh_admin_ids()
        assert cached is plugin._admin_ids  # 窗口内直接返回缓存（同一对象）
        cmd_file.write_text('{"admins_id": ["c"]}', encoding="utf-8")
        # NTFS mtime 粒度下连续两次写入可能落入同一时间片：强制推进 mtime，
        # 否则热读缓存误判为未变更（全量测试负载下偶发抖动）。
        stat = cmd_file.stat()
        os.utime(cmd_file, (stat.st_atime, stat.st_mtime + 1))
        plugin._admin_probe_ts = 0.0  # 推进探测窗口，强制重探
        assert plugin._refresh_admin_ids() == {"c"}
        cmd_file.write_text("{broken json", encoding="utf-8")
        plugin._admin_probe_ts = 0.0
        assert plugin._refresh_admin_ids() == {"c"}  # 坏文件不炸，保留上次集合

    with_plugin(tmp_path, scenario)


def test_refresh_admin_ids_window_suppresses_probe(tmp_path: Path) -> None:
    """窗口内跳过 stat：文件修改（mtime 已变）窗口内不可见；推进窗口后重探生效。"""

    async def scenario(plugin, main):
        cmd_file = plugin._data_path / "cmd_config.json"
        cmd_file.parent.mkdir(parents=True, exist_ok=True)
        cmd_file.write_text('{"admins_id": ["a"]}', encoding="utf-8")
        plugin._admin_probe_ts = 0.0  # __init__ 已消耗首探窗口，重置后强制重探
        assert plugin._refresh_admin_ids() == {"a"}
        cmd_file.write_text('{"admins_id": ["b"]}', encoding="utf-8")
        stat = cmd_file.stat()
        os.utime(cmd_file, (stat.st_atime, stat.st_mtime + 1))
        assert plugin._refresh_admin_ids() == {"a"}  # 窗口内不探测，仍旧值
        plugin._admin_probe_ts = 0.0
        assert plugin._refresh_admin_ids() == {"b"}  # 推进窗口后立即生效

    with_plugin(tmp_path, scenario)


def test_resolve_paths_branches(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        original_config = main.get_astrbot_config_path
        original_data = main.get_astrbot_plugin_data_path
        main.get_astrbot_config_path = None
        main.get_astrbot_plugin_data_path = None
        try:
            config_with_path = SimpleNamespace(config_path=str(tmp_path / "custom" / "config.json"))
            config_path, storage_path = plugin._resolve_paths(config_with_path)
            assert str(tmp_path / "custom" / "config.json") in str(config_path)
            assert "state.json" in str(storage_path)

            config_plain = SimpleNamespace()
            fallback_config, fallback_storage = plugin._resolve_paths(config_plain)
            assert str(fallback_config).endswith("astrbot_plugin_self_initiated_reply_config.json")
        finally:
            main.get_astrbot_config_path = original_config
            main.get_astrbot_plugin_data_path = original_data

    with_plugin(tmp_path, scenario)


# ============================================================================
# 事件 extra 容错与后台任务取消
# ============================================================================


def test_event_extra_tolerates_host_signature_differences(tmp_path: Path) -> None:
    """0.9.3：`_event_extra` 已外迁为 `utils.event_extra`（同族宿主字段兼容探测）。"""

    async def scenario(plugin, main):
        utils = importlib.import_module(f"{main.__package__}.utils")
        event = _make_event()
        event.set_extra("key", "value")
        assert utils.event_extra(event, "key") == "value"
        assert utils.event_extra(event, "missing", default="d") == "d"

        def type_error_extra(*_args):
            raise TypeError("signature mismatch")

        def broken_extra(*_args):
            raise RuntimeError("host error")

        event.get_extra = type_error_extra
        assert utils.event_extra(event, "key", default="d") == "d"
        event.get_extra = broken_extra
        assert utils.event_extra(event, "key", default="d") == "d"

        def bad_set_extra(*_args, **_kwargs):
            raise RuntimeError("set failed")

        event.set_extra = bad_set_extra
        plugin._set_command_handled(event)  # 不炸

    with_plugin(tmp_path, scenario)


def test_cancel_background_tasks_cancels_pending(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        async def long_running():
            await asyncio.sleep(60)

        task = plugin._track_background_task(long_running())
        assert task is not None
        plugin._cancel_background_tasks()
        await asyncio.sleep(0)
        assert task.cancelled()

    with_plugin(tmp_path, scenario)


# ============================================================================
# 第二批补盲：snapshot 失败/提取为空/命令入口判定/parser 关闭/自回复组入口
# ============================================================================


def test_on_message_snapshot_failure_still_captures(tmp_path: Path) -> None:
    """本地快照失败不阻断图片缓存（降级为直接 prepare）。"""

    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = True
        plugin.settings.enabled_message_trigger = False

        class SnapshotFailingParser(_FakeParser):
            async def snapshot_local_sources(self, images, *, max_concurrent=2) -> None:
                raise RuntimeError("snapshot failed")

        parser = SnapshotFailingParser(batch=[True])
        plugin._get_image_parser = lambda *a, **k: parser
        await plugin.on_message(_image_event(message_str="看看这张图"))
        await until(lambda: UMO in plugin._recent_image_events)

    with_plugin(tmp_path, scenario)


def test_on_message_has_images_but_extract_empty(tmp_path: Path) -> None:
    """has_images 命中但提取为空（组件缺来源）时走 debug 分支不阻塞消息流。"""

    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = True
        plugin.settings.enabled_message_trigger = False
        event = _make_event(message_str="一张图")
        event.get_messages = lambda: [type("Image", (), {})()]  # 无 url/file 的图片组件
        await plugin.on_message(event)
        assert UMO in plugin._last_events
        assert UMO not in plugin._recent_image_events

    with_plugin(tmp_path, scenario)


def test_is_command_entry_rejects_bare_command_word(tmp_path: Path) -> None:
    """命令判定：裸命令词（非 / 前缀、无 @Bot/唤醒词）不算命令入口。"""

    async def scenario(plugin, main):
        event = _make_event(message_str="selfreply help")
        assert plugin._is_command_entry(event, "selfreply help") is False

    with_plugin(tmp_path, scenario)


def test_track_background_task_close_failure_tolerated(tmp_path: Path) -> None:
    """停止屏障中协程 close 抛错也不上抛（回收路径的兜底）。"""

    async def scenario(plugin, main):
        plugin._stopping = True

        class BadCoro:
            def __await__(self):
                return asyncio.sleep(0).__await__()

            def close(self):
                raise RuntimeError("close failed")

        assert plugin._track_background_task(BadCoro()) is None

    with_plugin(tmp_path, scenario)


def test_prepare_images_without_vision_returns_early(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = False
        plugin.settings.vision_judge_enabled = False
        token = plugin._gate.advance(UMO)
        await plugin._prepare_images_for_session(
            UMO, generation=token, active_at=1.0, images=[_image()]
        )
        assert UMO not in plugin._recent_image_events

    with_plugin(tmp_path, scenario)


def test_decide_session_reply_dict_no_reply(tmp_path: Path) -> None:
    """判断 dict 明确不回复时返回格式化原因。"""

    async def scenario(plugin, main):
        original_decide = plugin._decision.decide

        async def fake_decide(*args, **kwargs):
            return {"should_reply": False, "reason": "无意图", "elapsed_sec": 0.0}

        plugin._decision.decide = fake_decide
        try:
            state = plugin._state_for(UMO)
            result = await plugin._decide_session_reply(
                UMO, state, trigger="message_delay", force=False, expected_generation=None
            )
            assert result == "判断不回复：无意图"
        finally:
            plugin._decision.decide = original_decide

    with_plugin(tmp_path, scenario)


def test_build_image_context_without_parser_returns_empty(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        plugin.settings.vision_main_enabled = False
        plugin.settings.vision_judge_enabled = False
        assert await plugin._build_image_context(UMO, enabled=True) == ""

    with_plugin(tmp_path, scenario)


def test_event_extra_non_callable_getter_returns_default(tmp_path: Path) -> None:
    async def scenario(plugin, main):
        utils = importlib.import_module(f"{main.__package__}.utils")
        event = _make_event()
        event.get_extra = None
        assert utils.event_extra(event, "key", default="d") == "d"

    with_plugin(tmp_path, scenario)
