"""红灯测试（第六轮）：高频常规路径日志降噪固化

清单（审查报告 P2-4 + 方案 v2 §3，HANDOFF 已逐行核实）：
- main.py 7 处 INFO → DEBUG：wait silence / skip session / decision /
  skip before send / proactive reply sent（if/else 双分支）/ event send completed
- image/parser.py 2 处 INFO → DEBUG：image frozen to local cache / in-memory data URL

降级前本文件全部失败（红灯）；降级后全部通过（绿灯）。消息锚定定位，
与行号无关，天然免疫其他阶段造成的行号漂移。
"""

from __future__ import annotations

from pathlib import Path


def _log_call_level(source: str, frag: str) -> list[str]:
    """定位 frag 消息对应的 logger 调用前缀（rfind 最近调用点，支持多处）。"""
    levels: list[str] = []
    search_from = 0
    while True:
        idx = source.find(frag, search_from)
        if idx == -1:
            break
        call = source.rfind("logger.", 0, idx)
        assert call != -1, f"no logger call before {frag!r}"
        levels.append(source[call : source.index("(", call)])
        search_from = idx + len(frag)
    return levels


def _main_source() -> str:
    import tests.test_vision as vision

    return (Path(vision.ROOT) / "main.py").read_text(encoding="utf-8")


def _parser_source() -> str:
    import tests.test_vision as vision

    return (Path(vision.ROOT) / "image" / "parser.py").read_text(encoding="utf-8")


# ============================================================================
# main.py：7 处降级点
# ============================================================================


def test_main_logs_wait_silence_is_debug() -> None:
    source = _main_source()
    assert _log_call_level(source, "[%s] wait for minimum silence session=") == ["logger.debug"]


def test_main_logs_skip_session_is_debug() -> None:
    source = _main_source()
    assert _log_call_level(source, "[%s] skip session=%s trigger=") == ["logger.debug"]


def test_main_logs_decision_is_debug() -> None:
    source = _main_source()
    assert _log_call_level(source, "[%s] decision session=%s trigger=") == ["logger.debug"]


def test_main_logs_skip_before_send_is_debug() -> None:
    source = _main_source()
    assert _log_call_level(source, "[%s] skip before send session=") == ["logger.debug"]


def test_main_logs_reply_sent_both_branches_are_debug() -> None:
    """log_reply_content 的 if/else 双分支都必须降级（两处调用点）。"""
    source = _main_source()
    levels = _log_call_level(source, "[%s] proactive reply sent session=")
    assert levels == ["logger.debug", "logger.debug"]


def test_main_logs_event_send_completed_is_debug() -> None:
    source = _main_source()
    assert _log_call_level(source, "[%s] event send completed session=") == ["logger.debug"]


# ============================================================================
# image/parser.py：2 处降级点
# ============================================================================


def test_parser_logs_image_frozen_to_cache_is_debug() -> None:
    source = _parser_source()
    assert _log_call_level(source, "image frozen to local cache: %s") == ["logger.debug"]


def test_parser_logs_image_frozen_data_url_is_debug() -> None:
    source = _parser_source()
    assert _log_call_level(source, "image frozen as in-memory data URL") == ["logger.debug"]
