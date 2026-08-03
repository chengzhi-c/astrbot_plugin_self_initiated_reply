"""红灯测试（第三轮）：0.7.0 全面审查发现的缺陷

每个测试都断言"期望的正确行为"。在修复前应当失败（红灯），
修复后转绿。分组对应审查报告中的编号。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "selfreply_round3_package"


def _install_astrbot_stubs() -> None:
    astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
    event = sys.modules.setdefault("astrbot.api.event", types.ModuleType("astrbot.api.event"))
    star = sys.modules.setdefault("astrbot.api.star", types.ModuleType("astrbot.api.star"))
    components = sys.modules.setdefault(
        "astrbot.api.message_components", types.ModuleType("astrbot.api.message_components")
    )

    class AstrMessageEvent:
        pass

    class Context:
        pass

    class At:
        pass

    if not hasattr(api, "logger"):
        api.logger = logging.getLogger("selfreply-round3")
    if not hasattr(event, "AstrMessageEvent"):
        event.AstrMessageEvent = AstrMessageEvent
    if not hasattr(star, "Context"):
        star.Context = Context
    if not hasattr(components, "At"):
        components.At = At
    setattr(astrbot, "api", api)


def _load_modules():
    _install_astrbot_stubs()
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE_NAME] = package
    models = importlib.import_module(f"{PACKAGE_NAME}.models")
    utils = importlib.import_module(f"{PACKAGE_NAME}.utils")
    commands = importlib.import_module(f"{PACKAGE_NAME}.commands")
    image = importlib.import_module(f"{PACKAGE_NAME}.image")
    recorder = importlib.import_module(f"{PACKAGE_NAME}.image.recorder_bridge")
    return models, utils, commands, image, recorder


def _main_source() -> str:
    return (ROOT / "main.py").read_text(encoding="utf-8")


# ============================================================================
# RL-1 任意本地文件读取并外传给 Vision Provider（高危）
# ============================================================================


def test_non_image_local_file_is_not_converted_to_data_url(tmp_path: Path) -> None:
    """image_to_data_url 必须拒绝非图片文件，而不是伪造 image/jpeg。"""
    _, _, _, _, recorder = _load_modules()

    secret = tmp_path / "credentials.env"
    secret.write_text("OPENAI_API_KEY=sk-super-secret-value", encoding="utf-8")

    result = recorder.MessageRecorderBridge.image_to_data_url(secret)

    assert result is None, (
        "非图片文件被转成 data URL：mimetypes 无法识别时回退 image/jpeg，"
        "任意文件内容都会被 base64 外传给 Vision Provider"
    )


def test_resolver_rejects_absolute_path_outside_media_scope(tmp_path: Path) -> None:
    """适配器给出的任意绝对路径不应被无条件读取。"""
    _, _, _, image, _ = _load_modules()

    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n", encoding="utf-8")

    parser = image.ImageParser(object(), provider_id="vision", recorder_bridge=None)
    info = image.ImageInfo(file_path=str(secret))

    resolved = asyncio.run(parser._resolve_image_url(info))

    assert resolved is None, (
        "任意绝对路径被读取并 base64 化：缺少扩展名白名单与目录约束，"
        "存在本地文件读取与外传风险"
    )


def test_image_extension_allowlist_is_enforced(tmp_path: Path) -> None:
    """伪装成图片后缀的文本文件不得通过。"""
    _, _, _, _, recorder = _load_modules()

    fake = tmp_path / "payload.png"
    fake.write_bytes(b"not-an-image-just-text-content")

    result = recorder.MessageRecorderBridge.image_to_data_url(fake)

    assert result is None, (
        "缺少图片魔数校验：任意内容只要改成 .png 后缀就会被当作图片外传"
    )


# --- 对照组：收紧后合法图片必须仍能通过（防止误杀识图功能）---

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_GIF_BYTES = b"GIF89a" + b"\x00" * 32
_WEBP_BYTES = b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 32


def test_real_images_are_still_accepted(tmp_path: Path) -> None:
    """真实图片必须仍然转成 data URL，且 MIME 来自字节而非文件名。"""
    _, _, _, _, recorder = _load_modules()

    cases = (
        ("a.png", _PNG_BYTES, "image/png"),
        ("b.jpg", _JPEG_BYTES, "image/jpeg"),
        ("c.gif", _GIF_BYTES, "image/gif"),
        ("d.webp", _WEBP_BYTES, "image/webp"),
        # 无扩展名的缓存文件（recorder 插件常见）也必须识别
        ("cache_12345", _PNG_BYTES, "image/png"),
    )

    for name, payload, expected_mime in cases:
        path = tmp_path / name
        path.write_bytes(payload)
        result = recorder.MessageRecorderBridge.image_to_data_url(path)
        assert result is not None, f"合法图片被误杀: {name}"
        assert result.startswith(f"data:{expected_mime};base64,"), (
            f"MIME 应根据魔数推定为 {expected_mime}: {name}"
        )


def test_sniffer_rejects_non_image_payloads() -> None:
    """魔数嗅探器本身的边界行为。"""
    _, _, _, image, _ = _load_modules()

    assert image.sniff_image_mime(_PNG_BYTES) == "image/png"
    assert image.sniff_image_mime(b"") == ""
    assert image.sniff_image_mime(b"OPENAI_API_KEY=sk-x") == ""
    assert image.sniff_image_mime(b"RIFF\x24\x00\x00\x00WAVE") == "", "WAV 不是图片"
    assert not image.is_image_payload(b"%PDF-1.7")


def test_resolver_still_accepts_real_local_image(tmp_path: Path) -> None:
    """端到端正向用例：真实本地图片仍能进入 Vision 解析。

    这条用于防止安全收紧把识图功能整体误杀。
    """
    _, _, _, image, _ = _load_modules()

    media_root = tmp_path / "media"
    real = media_root / "photo.png"
    media_root.mkdir()
    real.write_bytes(_PNG_BYTES)

    parser = image.ImageParser(
        object(),
        provider_id="vision",
        recorder_bridge=None,
        source_cache_dir=media_root,
    )
    resolved = asyncio.run(parser._resolve_image_url(image.ImageInfo(file_path=str(real))))

    assert resolved is not None, "合法本地图片被误杀，识图功能将完全失效"
    assert resolved.startswith("data:image/png;base64,")


def test_resolver_rejects_valid_image_outside_trusted_root(tmp_path: Path) -> None:
    """有效图片也不能绕过本地可信媒体根目录。"""
    _, _, _, image, _ = _load_modules()

    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_BYTES)

    parser = image.ImageParser(
        object(),
        provider_id="vision",
        recorder_bridge=None,
        source_cache_dir=media_root,
    )
    resolved = asyncio.run(parser._resolve_image_url(image.ImageInfo(file_path=str(outside))))

    assert resolved is None


# ============================================================================
# RL-2 allow_multiline_reply 配置失效（中危）
# ============================================================================


def test_clean_reply_preserves_newlines_when_multiline_allowed() -> None:
    """allow_multiline=True 时必须保留换行，否则该配置项形同虚设。"""
    _, utils, _, _, _ = _load_modules()

    reply = utils.clean_reply("第一行内容\n第二行内容", allow_multiline=True, max_chars=200)

    assert "\n" in reply, (
        "allow_multiline=True 仍然折叠了换行：clean_reply 中无条件执行的 "
        r"re.sub(r'\s+', ' ') 让 allow_multiline_reply 配置完全失效"
    )


def test_clean_reply_still_collapses_when_multiline_disabled() -> None:
    """allow_multiline=False 时仍应折叠为单行（对照组，应保持绿灯）。"""
    _, utils, _, _, _ = _load_modules()

    reply = utils.clean_reply("第一行内容\n第二行内容", allow_multiline=False, max_chars=200)

    assert "\n" not in reply
    assert reply == "第一行内容 第二行内容"


def test_reply_request_detection_truncates_overlong_input() -> None:
    """超长畸形输入只检测头部语义，不会造成正则放大或误报。"""
    _, utils, _, _, _ = _load_modules()

    # 标准别名接话请求不受影响
    assert utils.looks_like_reply_request("阿c回一下", ["阿c"]) is True
    assert utils.looks_like_reply_request("阿c在吗", ["阿c"]) is True
    # 别名 + 超长尾巴：全匹配语义下本就不是接话请求，截断后行为一致
    assert utils.looks_like_reply_request("阿c" + "很长的尾巴" * 200, ["阿c"]) is False
    # 超长普通闲聊不得误判为接话请求
    assert utils.looks_like_reply_request("今天天气不错" + "啊" * 500, []) is False
    # 全匹配语义："在吗"+超长尾巴截断后仍不得误匹配锚定模式（glm52 红灯复核场景）
    assert utils.looks_like_reply_request("在吗" + "普通聊天内容" * 50, []) is False
    assert utils.looks_like_reply_request("发个表情包" + "了" * 300, []) is False


# ============================================================================
# RL-3 判断提示词的多行结构被清洗破坏（中危）
# ============================================================================


def test_recent_messages_block_keeps_line_structure() -> None:
    """recent_messages 是多行聊天记录，清洗不应把它压成一行。

    main.py 的 _build_decision_prompt 必须用 allow_newlines=True 调用。
    """
    models, _, _, _, _ = _load_modules()

    block = "小明: 今天好热\n小红: 是啊\nBot: 记得多喝水"
    # 多行块必须传 allow_newlines=True，否则默认压扎为单行
    safe = models.sanitize_prompt_variable(block, max_length=2000, allow_newlines=True)

    assert "\n" in safe, (
        "allow_newlines=True 时多行聊天记录被压成单行，"
        "判断模型无法区分发言人和轮次"
    )


def test_sanitize_default_still_collapses_newlines() -> None:
    """allow_newlines 默认是 False：单字段变量应压成单行（对照组）。"""
    models, _, _, _, _ = _load_modules()

    block = "第一行\n第二行"
    safe = models.sanitize_prompt_variable(block, max_length=500)

    assert "\n" not in safe


def test_sanitize_does_not_inject_literal_backslashes() -> None:
    """判断提示词是纯文本，不应插入 JSON 转义反斜杠。"""
    models, _, _, _, _ = _load_modules()

    safe = models.sanitize_prompt_variable('他说"确实如此"', max_length=200)

    assert '\\"' not in safe, (
        "纯文本提示词里出现了 JSON 转义反斜杠，"
        "模型看到的是被污染的字面量而不是原始引号"
    )


def test_sanitize_still_removes_control_characters() -> None:
    """控制字符仍必须被清除（对照组，应保持绿灯）。"""
    models, _, _, _, _ = _load_modules()

    safe = models.sanitize_prompt_variable("正常\x00文本\x07内容", max_length=200)

    assert "\x00" not in safe and "\x07" not in safe


# ============================================================================
# RL-4 任意用户可触发命令并吞掉事件（中危）
# ============================================================================


def test_inline_command_requires_slash_or_mention() -> None:
    """裸词 selfreply 不应在任意会话里被当成命令并 stop_event。"""
    source = _main_source()
    start = source.index("    async def on_message(")
    end = source.index("\n    def _should_ignore_event(", start)
    handler = source[start:end]

    gated = (
        'startswith("/")' in handler
        or "is_at_or_wake_command_event" in handler
        or "_looks_like_command_entry" in handler
    )

    assert gated, (
        "on_message 未校验命令前缀：任意群成员发送裸词 selfreply 就能让 Bot "
        "回复整段帮助文本并 stop_event()，从而吞掉该消息、阻断其他插件"
    )


def test_help_action_is_reachable_without_admin() -> None:
    """确认 help 不在管理员动作集合内（用于说明上一条的影响面）。"""
    source = _main_source()
    line = next(
        item for item in source.splitlines() if item.startswith("ADMIN_COMMAND_ACTIONS")
    )
    assert '"help"' not in line, (
        "help 不受管理员限制，配合缺失的前缀校验构成无门槛触发面"
    )


def test_bare_command_word_is_parsed_as_command() -> None:
    """记录当前解析行为：裸词即命令（说明缺陷来源，非断言修复）。"""
    _, _, commands, _, _ = _load_modules()
    assert commands.parse_command_text("selfreply") == ("help", "")
    assert commands.parse_command_text("selfreply add") == ("add", "")


# ============================================================================
# RL-5 Web 配置读取失败返回 None（低危）
# ============================================================================


def test_api_get_config_returns_payload_on_failure() -> None:
    """Quart 路由不能返回 None，否则失败时抛 TypeError 变成 500。"""
    source = _main_source()
    start = source.index("    async def _api_get_config(")
    end = source.index("\n    async def _api_providers(", start)
    method = source[start:end]

    tail = method[method.index("except Exception"):]
    stripped = [item.strip() for item in tail.splitlines()]

    assert "return" not in stripped, (
        "_api_get_config 异常分支使用裸 return（None），"
        "Quart 无法序列化 None，会把可恢复的读取失败放大成 500"
    )


# ============================================================================
# RL-6 会话代次表无界增长（低危）
# ============================================================================


def test_session_generation_map_is_pruned_on_whitelist_removal() -> None:
    """移出白名单时应清理会话代次记录，避免长期运行内存缓慢增长。"""
    source = _main_source()
    start = source.index("    def _replace_whitelist(")
    end = source.index("\n    async def _add_whitelist_session(", start)
    method = source[start:end]

    assert "_session_generation" in method, (
        "_replace_whitelist 未清理 _session_generation；"
        "该字典按 UMO 累积且从不回收，长期运行会持续增长"
    )


def test_terminate_clears_image_event_cache() -> None:
    """terminate 应清理含图事件缓存，避免插件重载时残留事件对象。"""
    source = _main_source()
    method = source[source.index("    async def terminate("):]

    assert "_recent_image_events" in method, (
        "terminate 未清理 _recent_image_events，每会话最多 20 个事件对象残留"
    )


def test_terminate_waits_for_cancelled_background_tasks() -> None:
    """取消后台任务后必须等待其收尾，避免旧任务越过终止边界。"""
    source = _main_source()
    terminate = source[source.index("    async def terminate("):]
    wait_method_start = source.index("    async def _wait_background_tasks(")
    wait_method_end = source.index("\n    async def _stop_patrol_task(", wait_method_start)
    wait_method = source[wait_method_start:wait_method_end]

    assert "await self._wait_background_tasks()" in terminate
    assert "asyncio.gather" in wait_method
    assert "self._background_tasks" in wait_method


def test_image_cache_cleanup_has_manual_api_and_startup_sweep() -> None:
    """图片缓存既要能手动清理，也要在插件重载时立即扫一次。"""
    source = _main_source()
    register_start = source.index("    def _register_web_apis(")
    register_end = source.index("\n    @staticmethod\n    def _config_value", register_start)
    registration = source[register_start:register_end]

    assert 'f"{route}/image-cache/cleanup"' in registration
    assert '"POST"' in registration
    assert "_api_cleanup_image_cache" in registration
    assert "self._cleanup_image_sources(now=now_ts())" in source
    assert "if not self.settings.vision_enabled" not in source[source.index("    def _cleanup_image_sources("):source.index("    def _cleanup_old_events_if_needed(")]


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


def test_successful_image_cache_logs_are_debug_only() -> None:
    """高频成功路径不应在 INFO 级别刷屏，失败日志仍保留原级别。"""
    source = _main_source()
    parser_source = (ROOT / "image" / "parser.py").read_text(encoding="utf-8")

    assert 'logger.debug(\n                "[%s] captured %s/%s images into local vision cache for umo=%s",' in source
    assert 'logger.info(\n                "[%s] captured %s/%s images into local vision cache for umo=%s",' not in source
    assert 'logger.debug(\n                "[selfreply] host image snapshot created: %s",' in parser_source
    assert 'logger.info(\n                "[selfreply] host image snapshot created: %s",' not in parser_source


def test_config_mutations_share_one_lock_and_settings_normalizer() -> None:
    """白名单和 Web 配置更新不能交错覆盖，配置必须经统一入口规范化。"""
    source = _main_source()
    api_start = source.index("    async def _api_post_config(")
    api_end = source.index("\n    async def _api_status(", api_start)
    api = source[api_start:api_end]

    assert "async with self._config_lock" in api
    assert "_api_post_config_locked" in api
    assert "_add_whitelist_session_locked" in source
    assert "_remove_whitelist_session_locked" in source
    assert "Settings.from_config(candidate)" in api


def test_proactive_agent_starts_with_restricted_tool_scope() -> None:
    """主动 Agent 默认不得继承全局插件、跨会话消息和高危工具。"""
    source = _main_source()
    start = source.index("    async def _generate_reply_via_pipeline(")
    end = source.index("\n    def _enforce_final_tool_policy(", start)
    method = source[start:end]

    assert "req.func_tool = _AGENT_RUNTIME.new_tool_set()" in method
    assert "_install_agent_tool_boundary(last_event, inherit_tools)" in method
    assert "_enforce_final_tool_policy(req, inherit_tools)" in method
    boundary_start = source.index("    def _install_agent_tool_boundary(")
    boundary_end = source.index("\n    @staticmethod\n    def _resolve_paths", boundary_start)
    boundary = source[boundary_start:boundary_end]
    assert "setattr(event, \"plugins_name\", [])" in boundary
    # 共享 platform_meta 是适配器单例，禁止原地修改；边界必须靠最终工具集策略。
    assert "support_proactive_message" not in boundary
    policy_start = source.index("    def _enforce_final_tool_policy(")
    policy_end = source.index("\n    def _main_agent_build_config(", policy_start)
    policy = source[policy_start:policy_end]
    assert "filter_final_tools(req, keep=PROACTIVE_ALLOWED_TOOL_IDS)" in policy
    assert "_restore_agent_tool_boundary" in method


def test_new_message_does_not_cancel_running_decorating_hook() -> None:
    """新消息应使旧回复失效，但不能取消正在执行的装饰钩子。"""
    source = _main_source()
    cancel_start = source.index("    def _cancel_delay_task(")
    cancel_end = source.index("\n    def _clear_cached_event(", cancel_start)
    cancel_method = source[cancel_start:cancel_end]
    invalidate_start = source.index("    def _invalidate_session(")
    invalidate_end = source.index("\n    def _cancel_event_session(", invalidate_start)
    invalidate_method = source[invalidate_start:invalidate_end]
    bulk_start = source.index("    def _cancel_delay_tasks(")
    bulk_end = source.index("\n    async def _stop_patrol_task(", bulk_start)
    bulk_method = source[bulk_start:bulk_end]

    assert "self._running_sessions" in cancel_method
    assert "force" in cancel_method
    assert "force_cancel" in invalidate_method
    assert "force_cancel=True" in bulk_method


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
