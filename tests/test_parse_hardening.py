"""判断模型输出的解析加固：扫描耗时上限、深嵌套、非有限浮点。

三条回归对应三类生产输入：模型吐出成千上万左花括号、超深嵌套 JSON、
以及 ``NaN`` 这类 json 模块默认接受的非法字面量。
"""

from __future__ import annotations

import logging

from .host_stubs import capture_logs, load_modules, messages_at_least

PACKAGE_NAME = "selfreply_test_package"


def _utils():
    return load_modules(PACKAGE_NAME, "models", "utils")[1]


def test_deep_nesting_recursion_error_fails_closed() -> None:
    """C 扫描器对深嵌套抛 RecursionError（RuntimeError 子类），不是 JSONDecodeError。

    漏捕时异常穿出 parse_decision_json，判断在 /status 与 INFO 行里连记录都没有。
    """
    utils = _utils()

    raw = (
        '{"should_reply": true, "reason": "深嵌套", "evidence": '
        + "[" * 50_000
        + "]" * 50_000
        + "}"
    )
    assert utils.parse_decision_json(raw) is None


def test_non_finite_should_reply_is_rejected() -> None:
    """``bool(float('nan')) is True`` ⇒ NaN 会被判成"该回复"，唯一 fail-open。

    数字兼容（``1`` / ``0``）是既有契约，保留；只拒非有限值。
    """
    utils = _utils()

    assert utils.parse_decision_json('{"should_reply": NaN}') is None
    assert utils.parse_decision_json('{"should_reply": Infinity}') is None
    assert utils.parse_decision_json('{"should_reply": -Infinity}') is None
    # 既有字符串/数字兼容不得回退
    assert utils.parse_decision_json('{"should_reply": "true"}')["should_reply"] is True
    assert utils.parse_decision_json('{"should_reply": "no"}')["should_reply"] is False
    assert utils.parse_decision_json('{"should_reply": 1}')["should_reply"] is True


def test_valid_json_survives_many_invalid_objects() -> None:
    """扫描预算不能是「对象数」硬上限：模型先铺示例对象时，本轮判断被静默丢弃。

    64 个 ``{bad}`` 后的结果仍应被解析。
    """
    utils = _utils()

    raw = "{bad}" * 64 + '{"should_reply": true, "reason": "被点名"}'
    parsed = utils.parse_decision_json(raw)
    assert parsed is not None, "非法对象后的合法裁决被扫描上限丢弃"
    assert parsed["should_reply"] is True
    assert parsed["reason"] == "被点名"


def test_scan_attempt_limit_logs_warning(caplog: object, monkeypatch) -> None:
    """达到扫描尝试上限必须有告警，且尝试次数保持有界。"""
    utils = _utils()
    calls = 0
    raw_decode = utils._JSON_DECODER.raw_decode

    def count_decode(*args, **kwargs):
        nonlocal calls
        calls += 1
        return raw_decode(*args, **kwargs)

    monkeypatch.setattr(utils._JSON_DECODER, "raw_decode", count_decode)

    with capture_logs(caplog, utils.logger, logging.WARNING):
        assert utils.parse_decision_json("{bad}" * 10_000) is None
    assert calls == utils._JSON_SCAN_MAX_ATTEMPTS
    warnings = messages_at_least(caplog, logging.WARNING)
    assert any("reason=attempts" in line and "discarded_chars=" in line for line in warnings), (
        warnings
    )
