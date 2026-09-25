"""``capture_logs`` 的日志捕获契约：不重复、不丢失、不泄漏宿主 handler。

``tests/host_stubs.capture_logs`` 是所有日志断言的前置：它临时放行宿主
``propagate`` 让记录流进 caplog。这件"临时"必须满足三条契约，缺一条都会有
一类假信号：

- **不重复**：宿主真实 ``astrbot`` logger（``astrbot.api.logger``）上挂着
  LogManager 桥接的 caplog 同实例 handler。``propagate=True`` 后同一条记录
  经目标 logger 与 root 各进该 handler 一次，``caplog.records`` 里出现两份
  相同正文。计数型断言（"只许一条告警"）会被这份重复直接打红。
- **不丢失**：普通测试桩 logger 无自定义 handler，完全依赖流到 root 的
  capture handler。任何"顺手关掉 handler / 断开 root"的修复都会让事件不进
  caplog，日志断言变成假绿灯。
- **不泄漏**：块退出后目标 logger 的 handler 列表与 ``propagate`` 必须原样
  恢复。宿主的 handler 属于 AstrBot 全局日志桥，被永久摘除会同时打断
  loguru 转发与 WebUI 日志流，且这种破坏跨测试文件累积、难以归因。

宿主持的那条桥接链用 ``_bridged_host_logger`` 在此等价重建（``propagate=False``
+ 与 root 共享 caplog handler），不真的 ``import astrbot``：本仓库的 compat 作业
只跑 ``compat_check.py`` 与 ``test_vision.py -k host_platform_adapters``，从不在
装了真宿主的解释器里跑全量 pytest，所以一旦有文件在默认收集阶段导入真实
astrbot，``install_astrbot_stubs`` 的补全式桩就再也补不上
（``components.At`` 停在真实 pydantic 模型），依赖 ``_FakeAt`` 的用例随之假红。
等价重建既钉住 target logger 与 root 双路径这一重复根因，又让三条契约在两个
环境下都是硬红灯。
"""

from __future__ import annotations

import logging

import pytest

from .host_stubs import capture_logs


@pytest.fixture
def bridged_host_logger():
    """复刻宿主 ``astrbot`` logger 的桥接形态：``propagate=False`` 且与 root 共享处理器。

    ``LogManager.GetLogger`` 会把 root 的 handler 全量桥到具名 logger 上并关掉
    propagate。于是该 logger 一放行传播，同一条记录就从"目标 logger 自带的同一
    handler"与"root 上的同一个 handler"两条路各入账一次——这正是真实宿主下
    ``caplog.records`` 翻倍的机制。
    """
    logger = logging.getLogger("capture-contract-bridged-host")
    logger.handlers = []
    logger.propagate = False
    shared = logging.StreamHandler()
    logger.addHandler(shared)
    logging.getLogger().addHandler(shared)
    try:
        yield logger
    finally:
        logger.removeHandler(shared)
        logging.getLogger().removeHandler(shared)
        logger.handlers = []


class _StubLogger:
    """普通测试桩 logger：无自定义 handler，靠 propagate 到 root 完成捕获。

    这正是 ``install_astrbot_stubs`` 里 ``astrbot.api.logger`` 的形态
    （``logging.getLogger("selfreply-main-test")``），也是绝大多数被测模块
    在 .venv 桩环境下拿到的 logger。
    """

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    def warning(self, message: str) -> None:
        self._logger.warning(message)


# ============================================================================
# 不重复：单条日志只进 caplog 一次
# ============================================================================


def test_capture_logs_records_each_log_once_for_stub_logger(caplog) -> None:
    """桩 logger 下单条 warning 只被捕获一次（流经 root 的 capture handler）。"""
    stub = _StubLogger("capture-contract-stub")
    with capture_logs(caplog, stub.logger, logging.WARNING):
        stub.warning("stub once")
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["stub once"], messages


def test_capture_logs_does_not_duplicate_bridged_host_logger_records(
    caplog, bridged_host_logger
) -> None:
    """桥接宿主持下单条 warning 只被捕获一次。

    宿主 ``astrbot`` logger 的 handler 列表里已有本次 caplog 的同实例
    capture handler（LogManager 把 root 的 handler 桥接了上去）。
    ``propagate=True`` 会让同一条记录经该 handler 与 root 各处理一次，
    产物是 ``caplog.records`` 里两份同正文。历史上三个断言
    「失败只许一条告警」的用例就是这样被打红的。
    """
    logging.getLogger().addHandler(caplog.handler)
    try:
        with capture_logs(caplog, bridged_host_logger, logging.WARNING):
            bridged_host_logger.warning("host once")
    finally:
        logging.getLogger().removeHandler(caplog.handler)
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["host once"], f"宿主 logger 记录被重复捕获：{messages}"


# ============================================================================
# 不丢失：多级别日志按级别过滤完整进入 caplog
# ============================================================================


def test_capture_logs_still_delivers_records_for_stub_logger(caplog) -> None:
    """桩 logger 下各级别记录照常进入 caplog（捕获没被顺手关掉）。"""
    stub = _StubLogger("capture-contract-stub")
    with capture_logs(caplog, stub.logger, logging.DEBUG):
        stub.logger.debug("debug line")
        stub.logger.info("info line")
        stub.logger.warning("warning line")
        stub.logger.error("error line")
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["debug line", "info line", "warning line", "error line"], messages

    with capture_logs(caplog, stub.logger, logging.WARNING):
        stub.logger.info("filtered out")
        stub.logger.warning("kept")
    warnings = [record.getMessage() for record in caplog.records if record.levelno >= 30]
    assert warnings == ["kept"], warnings


def test_capture_logs_still_delivers_records_for_bridged_host_logger(
    caplog, bridged_host_logger
) -> None:
    """桥接宿主持下记录同样完整进入 caplog（消重不能误伤捕获）。"""
    logging.getLogger().addHandler(caplog.handler)
    try:
        with capture_logs(caplog, bridged_host_logger, logging.WARNING):
            bridged_host_logger.info("filtered out")
            bridged_host_logger.warning("kept")
    finally:
        logging.getLogger().removeHandler(caplog.handler)
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["kept"], messages


# ============================================================================
# 不泄漏：handler 列表与 propagate 原样恢复
# ============================================================================


def test_capture_logs_restores_stub_logger_state(caplog) -> None:
    """块退出后桩 logger 的 handler 列表与 propagate 保持进入前的值。"""
    stub = _StubLogger("capture-contract-stub")
    handlers_before = list(stub.logger.handlers)
    propagate_before = stub.logger.propagate
    with capture_logs(caplog, stub.logger, logging.WARNING):
        stub.warning("inside")
    assert list(stub.logger.handlers) == handlers_before
    assert stub.logger.propagate == propagate_before


def test_capture_logs_restores_bridged_host_logger_handlers_verbatim(
    caplog, bridged_host_logger
) -> None:
    """退出后桥接宿主持的 handler 列表必须逐实例、逐顺序原样恢复。

    LogManager 在宿主启动时把 root 的 handler 桥到 ``astrbot`` logger 上，
    ``capture_logs`` 只许临时借走与 caplog 重复的那份；这些 handler 同时
    承担 loguru 转发与 WebUI 日志流，被永久摘除会静默打断宿主日志输出，
    且破坏跨测试文件累积、难以归因。
    """
    logging.getLogger().addHandler(caplog.handler)
    try:
        handlers_before = list(bridged_host_logger.handlers)
        propagate_before = bridged_host_logger.propagate
        with capture_logs(caplog, bridged_host_logger, logging.WARNING):
            inside = list(bridged_host_logger.handlers)
            # 块内只少了 caplog 同实例处理器，承担转发的 shared handler 原样在位
            assert [h for h in handlers_before if h is not caplog.handler] == inside
            bridged_host_logger.warning("inside")
        assert list(bridged_host_logger.handlers) == handlers_before
        assert bridged_host_logger.propagate == propagate_before
    finally:
        logging.getLogger().removeHandler(caplog.handler)


def test_capture_logs_restores_bridged_host_logger_handlers_after_exception(
    caplog, bridged_host_logger
) -> None:
    """异常路径下也要原样恢复：被测代码在 capture 块内 raise 是最常见形态。"""
    logging.getLogger().addHandler(caplog.handler)
    try:
        handlers_before = list(bridged_host_logger.handlers)
        propagate_before = bridged_host_logger.propagate
        with pytest.raises(RuntimeError, match="boom"):
            with capture_logs(caplog, bridged_host_logger, logging.WARNING):
                bridged_host_logger.warning("before raise")
                raise RuntimeError("boom")
        assert list(bridged_host_logger.handlers) == handlers_before
        assert bridged_host_logger.propagate == propagate_before
    finally:
        logging.getLogger().removeHandler(caplog.handler)


def test_capture_logs_does_not_mutate_root_logger_handlers(caplog, bridged_host_logger) -> None:
    """修复只作用于目标 logger：root 的 handler 集合（含 caplog 自己）不受影响。"""
    root = logging.getLogger()
    root_before = list(root.handlers)
    with capture_logs(caplog, bridged_host_logger, logging.WARNING):
        bridged_host_logger.warning("inside")
    assert list(root.handlers) == root_before
