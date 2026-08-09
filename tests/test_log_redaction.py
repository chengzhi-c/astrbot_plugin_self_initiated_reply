"""下载失败日志的凭证脱敏契约。

背景：图床直链常把凭证放在 query（OSS/COS 的 Signature、腾讯 rkey 等），
历史实现 ``logger.info(..., url[:80])`` 会把凭证原样写进日志文件。

断言基于"日志里实际出现了什么文本"这一可观测事实，不引用内部函数名，
因此重命名脱敏实现不会让本文件变成假绿灯。
"""

from __future__ import annotations

import asyncio

from .host_stubs import install_astrbot_stubs, load_package

PACKAGE_NAME = "selfreply_log_redaction_package"

SIGNED_URL = (
    "https://img.example.com/group/abc/0/media.jpg"
    "?rkey=SECRET_RKEY_VALUE&Signature=SECRET_SIGNATURE&token=SECRET_TOKEN"
)
SECRETS = ("SECRET_RKEY_VALUE", "SECRET_SIGNATURE", "SECRET_TOKEN")


def _load_parser():
    install_astrbot_stubs()
    return load_package(PACKAGE_NAME, "image.parser")


def _capture_download_failure_log(parser_mod, *, field: str) -> str:
    """驱动下载失败路径，返回该次 info 日志的渲染文本。

    field="file_path" 覆盖 file_path 分支，field="url" 覆盖 url 分支。
    """
    lines: list[str] = []

    class _Recorder:
        @staticmethod
        def info(template: str, *args: object) -> None:
            lines.append(template % args if args else template)

        def __getattr__(self, _name: str):  # debug/warning 等一律忽略
            return lambda *a, **k: None

    original_logger = parser_mod.logger
    parser_mod.logger = _Recorder()
    try:
        instance = parser_mod.ImageParser(object())
        # 下载恒失败：使日志分支必达，且不触碰真实网络
        instance._fetch_image_data_url = lambda _url: asyncio.sleep(0, result=None)
        kwargs = {field: SIGNED_URL}
        image = parser_mod.ImageInfo(**kwargs)
        asyncio.run(instance._resolve_image_url(image))
    finally:
        parser_mod.logger = original_logger

    failures = [line for line in lines if "download failed" in line]
    assert failures, f"未捕获下载失败日志，实际记录：{lines}"
    return failures[-1]


def test_url_branch_log_drops_credentials() -> None:
    """url 分支：日志不得包含 query 中的任何凭证片段。"""
    parser_mod = _load_parser()
    line = _capture_download_failure_log(parser_mod, field="url")
    for secret in SECRETS:
        assert secret not in line, f"日志泄漏凭证 {secret}：{line}"


def test_file_path_branch_log_drops_credentials() -> None:
    """file_path 分支（http URL 走 file_path 字段）同样不得泄漏凭证。"""
    parser_mod = _load_parser()
    line = _capture_download_failure_log(parser_mod, field="file_path")
    for secret in SECRETS:
        assert secret not in line, f"日志泄漏凭证 {secret}：{line}"


def test_log_keeps_diagnosability_and_marks_redaction() -> None:
    """脱敏后仍须能定位失败对象，并标记 query 已被剥离。"""
    parser_mod = _load_parser()
    line = _capture_download_failure_log(parser_mod, field="url")
    assert "img.example.com" in line, f"丢失 host，无法定位失败来源：{line}"
    assert "media.jpg" in line, f"丢失 path，无法定位失败对象：{line}"
    assert "<redacted>" in line, f"未标记已剥离的 query：{line}"


def test_redaction_degrades_on_non_url_shapes() -> None:
    """空值、非 URL 与畸形输入走裸截断兜底，不抛异常。"""
    parser_mod = _load_parser()
    redact = parser_mod._redact_url

    assert redact("") == ""
    assert redact("   ") == ""
    # 无 scheme/netloc 的本地路径原样保留（便于排查落盘文件）
    assert redact("C:/tmp/local.jpg") == "C:/tmp/local.jpg"
    assert redact("/var/data/img.png") == "/var/data/img.png"
    # 无 query 的正常 URL 不加标记
    assert redact("https://img.example.com/a.jpg") == "https://img.example.com/a.jpg"
    # urlparse 对畸形端口抛 ValueError，需走兜底而非崩溃
    assert redact("http://[::1") == "http://[::1"


def test_redacted_length_never_exceeds_budget() -> None:
    """输出长度恒 <= LOG_URL_MAX_CHARS——标记必须计入截断预算。

    复审实测缺陷：早期实现写成 clean[:80] + "?<redacted>"，超长 path 时
    产出 91 字符，比它要替换的原实现（url[:80]）更宽，日志行反而变长。
    """
    parser_mod = _load_parser()
    redact = parser_mod._redact_url
    limit = parser_mod.LOG_URL_MAX_CHARS

    long_signed = "https://img.example.com/" + "p" * 300 + ".jpg?Signature=SECRET_SIGNATURE"
    for value in (
        long_signed,
        "data:image/png;base64," + "X" * 300,
        "/" + "a" * 300,
        "https://img.example.com/a.jpg#" + "f" * 300,
    ):
        rendered = redact(value)
        assert len(rendered) <= limit, f"超出 {limit} 字符预算（{len(rendered)}）：{rendered}"

    # 超长带签名的 URL 仍不得泄漏凭证
    assert "SECRET_SIGNATURE" not in redact(long_signed)
