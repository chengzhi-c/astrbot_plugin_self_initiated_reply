"""下载失败日志的凭证脱敏契约。

背景：图床直链常把凭证放在 query（OSS/COS 的 Signature、腾讯 rkey 等），
历史实现 ``logger.info(..., url[:80])`` 会把凭证原样写进日志文件。

断言基于"日志里实际出现了什么文本"这一可观测事实，不引用内部函数名，
因此重命名脱敏实现不会让本文件变成假绿灯。
"""

from __future__ import annotations

import asyncio

import pytest

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


def _make_failing_maker(
    error_text: str = f"Client error '401' for url '{SIGNED_URL}'",
) -> tuple[object, object, object]:
    """构造一个判断模型必抛「异常文本含签名 URL」的 DecisionMaker。

    provider SDK 的异常串常把请求 URL 整段带出来（openai 的 APIStatusError
    形如 ``Client error '401' for url '...?api_key=...'``），这是本文件要守的
    真实泄漏形态，不是臆想。
    """
    install_astrbot_stubs()
    models = load_package(f"{PACKAGE_NAME}_models", "models")
    decision = load_package(f"{PACKAGE_NAME}_decision", "decision")

    async def boom(_provider_id: str, _prompt: str):
        raise RuntimeError(error_text)

    async def resolve(_umo: str) -> str:
        return "judge-provider"

    async def no_history(*_a, **_k):
        return []

    async def no_image(*_a, **_k):
        return ""

    maker = decision.DecisionMaker(
        settings=models.Settings.from_config({"decision_model_enabled": True}),
        resolve_provider=resolve,
        llm_generate=boom,
        read_history=no_history,
        build_image_context=no_image,
    )
    return models, maker, decision


@pytest.mark.parametrize(
    "error_text",
    [
        f"Client error '401' for url '{SIGNED_URL}'",
        "HTTP 401 Authorization: Bearer SECRET_TOKEN",
        'HTTP 401 {"api_key": "SECRET_TOKEN", "password": "SECRET_SIGNATURE", '
        '"client_secret": "SECRET_TOKEN", "secret_key": "SECRET_RKEY_VALUE"}',
    ],
)
def test_decision_exception_reason_drops_credentials(error_text: str) -> None:
    """reason 会经 GET /status 的 last_decisions 出网，不得带出 query 凭证。

    这是比日志更宽的面：日志落在服务器本地，reason 却回给任何能访问控制台的
    调用方。异常文本里的签名 URL 必须先脱敏再进 reason。
    """
    models, maker, _ = _make_failing_maker(error_text)
    result = asyncio.run(
        maker.ask_decision_model("s1", models.SessionState(), trigger="message_delay"),
    )
    assert result["should_reply"] is False
    assert result["reason"] == "判断模型异常，请查看 AstrBot 日志"
    for secret in SECRETS:
        assert secret not in result["reason"], f"reason 泄漏凭证 {secret}：{result['reason']}"


def test_storage_failure_log_drops_url_credentials() -> None:
    """持久化适配器的异常日志也不能原样写入请求 URL。"""
    install_astrbot_stubs()
    storage = load_package(f"{PACKAGE_NAME}_storage", "storage")

    class BrokenConfig:
        def clear(self) -> None:
            raise RuntimeError(f"request failed for {SIGNED_URL}")

        def update(self, _data: object) -> None:
            raise AssertionError("clear should fail first")

    lines: list[str] = []

    class Recorder:
        def warning(self, template: str, *args: object) -> None:
            lines.append(template % args if args else template)

        def __getattr__(self, _name: str):
            return lambda *args, **kwargs: None

    original_logger = storage.logger
    storage.logger = Recorder()
    try:
        assert storage._update_config_obj(BrokenConfig(), {}) is False
    finally:
        storage.logger = original_logger

    assert lines
    for secret in SECRETS:
        assert secret not in lines[-1]


def test_decision_exception_reason_drops_relative_url_credentials() -> None:
    """urllib3 的 MaxRetryError 把请求 URL 以**相对路径**形态带出来，同样要脱敏。

    缺陷形态：``redact_exc_text`` 的模式只认 ``scheme://`` 绝对 URL，而
    httpx/requests/urllib3 连接失败的标准文本是
    ``... Max retries exceeded with url: /v1/chat?api_key=sk-...&x=1``——
    没有 scheme，匹配不到，凭证原样进 reason 并经 GET /status 出网。
    reason 的回出面比日志更宽（任何能访问控制台的调用方），这条必须先堵。
    """
    install_astrbot_stubs()
    utils_mod = load_package(f"{PACKAGE_NAME}_utils", "utils")
    relative = (
        "HTTPSConnectionPool(host='api.example.com', port=443): "
        "Max retries exceeded with url: /v1/chat?api_key=sk-REL123&x=1 "
        "(Caused by NewConnectionError('connection failed'))"
    )
    redacted = utils_mod.redact_exc_text(RuntimeError(relative))
    assert "sk-REL123" not in redacted, f"相对路径 URL 的 query 凭证漏进 reason：{redacted}"
    assert "/<redacted>" in redacted, "相对路径未脱敏"
    assert "?<redacted>" in redacted, "未留下脱敏标记，读者不知道有参数被抹掉"

    # 绝对 URL 与 userinfo 两种既有形态不得被这次扩展破坏
    absolute = "Client error for url 'https://api.example.com/v1/chat?api_key=sk-ABS123'"
    assert "sk-ABS123" not in utils_mod.redact_exc_text(RuntimeError(absolute))
    userinfo = "failed https://user:pa55@api.example.com/v1/chat"
    assert "pa55" not in utils_mod.redact_exc_text(RuntimeError(userinfo))


def test_decision_exception_log_drops_url_credentials() -> None:
    """同一条异常写进日志时同样不得带出凭证。"""
    models, maker, decision = _make_failing_maker()
    lines: list[str] = []

    class _Recorder:
        def warning(self, template: str, *args: object) -> None:
            lines.append(template % args if args else template)

        def __getattr__(self, _name: str):
            return lambda *_a, **_k: None

    original = decision.logger
    decision.logger = _Recorder()
    try:
        asyncio.run(
            maker.ask_decision_model("s1", models.SessionState(), trigger="message_delay"),
        )
    finally:
        decision.logger = original
    rendered = "\n".join(lines)
    assert rendered, "未捕获判断模型异常日志"
    for secret in SECRETS:
        assert secret not in rendered, f"日志泄漏凭证 {secret}：{rendered}"


def test_image_batch_exception_log_drops_url_credentials() -> None:
    parser_mod = _load_parser()
    parser = parser_mod.ImageParser(object())
    lines: list[str] = []

    class _Recorder:
        def debug(self, template: str, *args: object) -> None:
            lines.append(template % args if args else template)

        def __getattr__(self, _name: str):
            return lambda *args, **kwargs: None

    async def fail(_image: object) -> bool:
        raise RuntimeError(f"request failed for url '{SIGNED_URL}'")

    original = parser_mod.logger
    parser_mod.logger = _Recorder()
    try:
        result = asyncio.run(
            parser._run_concurrent([object()], fail, max_concurrent=1, error_value=False)
        )
    finally:
        parser_mod.logger = original

    assert result == [False]
    assert lines
    for secret in SECRETS:
        assert secret not in lines[-1]


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
    assert "path" not in line, f"URL 路径泄漏：{line}"
    assert "<redacted>" in line, f"未标记已剥离的 query：{line}"


def _load_utils():
    install_astrbot_stubs()
    return load_package(f"{PACKAGE_NAME}_utils", "utils")


def test_redaction_degrades_on_non_url_shapes() -> None:
    """空值、非 URL 与畸形输入走裸截断兜底，不抛异常。"""
    redact = _load_utils().redact_url

    assert redact("") == ""
    assert redact("   ") == ""
    # 无 scheme/netloc 的本地路径原样保留（便于排查落盘文件）
    assert redact("C:/tmp/local.jpg") == "C:/tmp/local.jpg"
    assert redact("/var/data/img.png") == "/var/data/img.png"
    assert redact("https://img.example.com/a.jpg") == "https://img.example.com/<redacted>"
    # urlparse 对畸形端口抛 ValueError，需走兜底而非崩溃
    assert redact("http://[::1") == "http://[::1"


def test_redaction_strips_userinfo_credentials() -> None:
    """netloc 里的 user:password@ 与 query 里的签名同等敏感，必须一并剥掉。

    缺陷形态：只剥 query/fragment 时 ``https://alice:SECRET@host/a.jpg`` 会
    原样输出（无 query 时连 ``<redacted>`` 标记都没有），basic-auth 凭证进日志
    并经 GET /status 出网。
    """
    redact = _load_utils().redact_url

    out = redact("https://alice:SUPERSECRET@img.example.com/a.jpg")
    assert "SUPERSECRET" not in out, f"userinfo 密码泄漏：{out}"
    assert "alice" not in out, f"userinfo 用户名泄漏：{out}"
    assert out == "https://img.example.com/<redacted>"
    # 与 query 同时出现时两个都剥
    both = redact("https://alice:SECRET@img.example.com/a.jpg?Signature=X&token=Y")
    assert "SECRET" not in both and "Signature" not in both, f"组合形态泄漏：{both}"
    assert both == "https://img.example.com/<redacted>?<redacted>"
    assert redact("http://[::1]:8080/a.jpg") == "http://[::1]:8080/<redacted>"
    assert redact("https://img.example.com:8443/a.jpg") == "https://img.example.com:8443/<redacted>"
    # 越界/非数字端口不得让脱敏自身抛异常（对端可控文本即可构造）
    assert redact("http://host:99999/a.jpg") == "http://host:99999/<redacted>"
    assert redact("http://host:abc/a.jpg") == "http://host:abc/<redacted>"


def test_exception_text_extraction_survives_broken_str() -> None:
    """``__str__`` 抛异常的异常不得让取文本本身崩溃，也不得因此泄漏。

    缺陷形态：投递路径在 ``except`` 块里构造 ``SendOutcome(status, str(exc))``，
    二次抛出会让异常逃出 gateway/send_reply → ledger 停在 sealed →
    ``has_submission=False`` 不消耗冷却与配额 → 已提交的消息重复发送。
    ``redact_exc_text`` 同样跑在异常处理路径上，故一并要求它不抛。
    """
    utils_mod = _load_utils()

    class BrokenStr(Exception):
        def __str__(self) -> str:  # pragma: no cover - 由被测代码触发
            raise RuntimeError("__str__ is broken")

    text = utils_mod.safe_exc_text(BrokenStr())
    assert text == "BrokenStr", f"退化值应是类型名，实得 {text!r}"
    # 含 URL 的坏 ``__str__`` 异常：redact_exc_text 也不得抛
    redacted = utils_mod.redact_exc_text(BrokenStr())
    assert redacted == "BrokenStr"
    # 正常异常照旧脱敏
    assert "SECRET" not in utils_mod.redact_exc_text(
        RuntimeError("Client error '401' for url 'https://h/a?api_key=SECRET'")
    )


def test_redacted_length_never_exceeds_budget() -> None:
    """输出长度恒 <= LOG_URL_MAX_CHARS，标记必须计入截断预算。

    缺陷形态：写成 clean[:80] + "?<redacted>" 时超长 path 会产出 91 字符，
    比被替换的 url[:80] 更宽，日志行反而变长。
    """
    utils_mod = _load_utils()
    redact = utils_mod.redact_url
    limit = utils_mod.LOG_URL_MAX_CHARS

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


def _capture_vision_parse_failure_log(parser_mod, exc: BaseException) -> str:
    """让 provider 调用抛指定异常，返回 ``image parsing failed`` 那行日志。"""
    lines: list[str] = []

    class _Recorder:
        def __getattr__(self, _name):
            def _log(message, *args, **_kwargs):
                try:
                    lines.append(str(message) % args if args else str(message))
                except Exception:
                    lines.append(str(message))

            return _log

    class _RaisingBridge:
        """桥接替身：provider 解析与调用都抛指定异常。"""

        async def resolve_provider_id(self, _umo, _configured):
            return "vision-provider"

        async def llm_generate_direct(self, **_kwargs):
            raise exc

    original_logger = parser_mod.logger
    parser_mod.logger = _Recorder()
    try:
        parser = parser_mod.ImageParser(_RaisingBridge())

        # 绕开网络：直接给出可解析的 data URL
        async def _fake_resolve(_image):
            return "data:image/png;base64,iVBORw0KGgo="

        parser._resolve_image_url = _fake_resolve  # type: ignore[method-assign]
        image = parser_mod.ImageInfo(url=SIGNED_URL)
        asyncio.run(parser.parse(image, umo="s1"))
    finally:
        parser_mod.logger = original_logger
    failures = [line for line in lines if "image parsing failed" in line]
    return failures[-1] if failures else ""


def test_vision_provider_exception_log_drops_credentials() -> None:
    """Vision provider 异常日志必须脱敏（与 decision/adapters 同一口径）。

    缺陷形态：``image/parser.py`` 的 ``except Exception`` 直接记 ``exc``，而
    provider SDK 的异常串常把请求 URL 整段带出来（含 api_key/Signature 等
    query 凭证），直接记录会泄露签名。
    """
    parser_mod = _load_parser()
    exc = RuntimeError(f"Client error '401' for url '{SIGNED_URL}'")
    line = _capture_vision_parse_failure_log(parser_mod, exc)
    assert line, "未捕获 vision 解析失败日志，用例已失去覆盖对象"
    for secret in SECRETS:
        assert secret not in line, f"vision 异常日志泄漏凭证 {secret}：{line}"
    assert "<redacted>" in line, f"未标记 query 已被剥离：{line}"


def test_history_read_failure_log_drops_credentials() -> None:
    """宿主会话存储后端失败时，降级 debug 日志不得带出连接串凭证。

    会话后端可能是 redis/db，其异常文本常含带 user:pass 的连接 URL；本方法与
    ``_call_host`` 走同一脱敏口径，级别低不是豁免理由。
    """
    install_astrbot_stubs()
    adapters = load_package(f"{PACKAGE_NAME}_adapters", "adapters")

    class _Manager:
        async def get_curr_conversation_id(self, _umo):
            raise RuntimeError(f"connect failed for {SIGNED_URL}")

    class _Context:
        conversation_manager = _Manager()

    lines: list[str] = []

    class _Recorder:
        def debug(self, template: str, *args: object) -> None:
            lines.append(template % args if args else template)

        def __getattr__(self, _name: str):
            return lambda *a, **k: None

    original_logger = adapters.logger
    adapters.logger = _Recorder()
    try:
        bridge = adapters.AstrBotBridge(_Context())
        assert asyncio.run(bridge.read_astrbot_history("s1", limit=5)) == []
    finally:
        adapters.logger = original_logger

    assert lines, "未捕获历史读取失败日志"
    for secret in SECRETS:
        assert secret not in lines[-1], f"日志泄漏凭证 {secret}：{lines}"


def test_redact_exc_text_covers_ipv6_and_bracket_urls() -> None:
    """URL 里出现 ``]``/``)`` 时 query 凭证不得幸存。

    缺陷形态：绝对 URL 模式原字符类排除 ``]`` 与 ``)``，IPv6 字面量主机
    （http://[::1]:11434/v1/chat?api_key=...）或路径含 ``)`` 的 URL 会被
    截断，redact_url 只收到 ``http://[::1`` 这种残缺片段（urlparse 直接
    ValueError），其后的端口、路径与整个 query 原样进日志与 GET /status
    的 reason。右界只该是空白/引号/尖括号。
    """
    utils_mod = _load_utils()
    for text in (
        "Client error '401' for url 'http://[::1]:11434/v1/chat?api_key=SECRET_TOKEN'",
        "Client error '401' for url 'https://[2001:db8::1]:8443/v1/chat?api_key=SECRET_TOKEN'",
        "Client error '401' for url 'https://h/a)b?token=SECRET_TOKEN'",
        "Client error '401' for url 'https://h/a]b?token=SECRET_TOKEN'",
        "redis://[::1]:6379/0?password=SECRET_TOKEN",
    ):
        redacted = utils_mod.redact_exc_text(RuntimeError(text))
        assert "SECRET_TOKEN" not in redacted, f"IPv6/括号 URL 泄漏凭证：{redacted}"
        assert "?<redacted>" in redacted, f"脱敏标记缺失：{redacted}"


def test_redact_relative_url_honours_length_budget() -> None:
    """相对路径形态同样受 LOG_URL_MAX_CHARS 约束：它也进 reason 出网。

    缺陷形态：_redact_relative_url 只截 query 不截长度，urllib3 的
    MaxRetryError 带几百字符 path 时，reason 的 URL 部分可以无限长。
    """
    utils_mod = _load_utils()
    limit = utils_mod.LOG_URL_MAX_CHARS
    text = (
        "HTTPSConnectionPool(host='api.example.com', port=443): Max retries exceeded "
        "with url: /" + "v1/chat/" * 60 + "?api_key=SECRET_TOKEN (Caused by "
        "NewConnectionError('connection failed'))"
    )
    redacted = utils_mod.redact_exc_text(RuntimeError(text))
    url_part = redacted.split("with url: ", 1)[1].split(" (Caused", 1)[0]
    assert len(url_part) <= limit, f"相对 URL 超出 {limit} 字符预算：{len(url_part)}"
    assert "SECRET_TOKEN" not in redacted
    assert url_part.startswith("/<redacted>"), f"相对路径未脱敏：{url_part}"


def test_redact_url_fallback_scrubs_userinfo_and_query() -> None:
    """urlparse 分解失败或没有 netloc 时不得原样放行。

    缺陷形态：两个早退分支直接 ``return text[:LOG_URL_MAX_CHARS]``，而匹配到
    的整段本就是可疑 URL——分解失败（畸形 IPv6）或 netloc 为空都不让它的
    userinfo 与 query 变安全。例如 ``http://user:pass@[::1/v1?api_key=...``
    与 ``https:///api#token=...`` 的凭证原样进出。
    """
    utils_mod = _load_utils()
    redact = utils_mod.redact_url
    for value in (
        "http://user:pass@[::1/v1",
        "http://user:pass@[::1/v1?api_key=SECRET_TOKEN",
        "https:///api#token=SECRET_TOKEN",
        "a://#token=SECRET_TOKEN",
    ):
        out = redact(value)
        assert "SECRET_TOKEN" not in out, f"兜底分支泄漏凭证：{out}"
        assert "user:pass" not in out, f"兜底分支泄漏 userinfo：{out}"
        assert len(out) <= utils_mod.LOG_URL_MAX_CHARS

    #  pinned 的非 URL 形状仍原样（无 @、无 ?/#、无 &key= 就不该被改）
    assert redact("http://[::1") == "http://[::1"
    assert redact("C:/tmp/local.jpg") == "C:/tmp/local.jpg"
    assert redact("/var/data/img.png") == "/var/data/img.png"


def test_redact_url_scrubs_key_runs_in_netloc_and_path() -> None:
    """``&key=`` / ``;key=`` 运行落在 netloc 或 path 时同样要截掉。

    缺陷形态：只去 @userinfo、原样复制 path。urlparse 不把无 ``?`` 的
    ``&api_key=`` 归进 query，于是 ``https://h/v1/chat&api_key=sk-...`` 整段
    幸存；更糟的是后面再带一个 ``?`` 时输出以 ``?<redacted>`` 结尾，读起来
    像"query 已脱敏"，凭证却摆在它前面。
    """
    utils_mod = _load_utils()
    redact = utils_mod.redact_url
    for value, expected in (
        ("https://host/v1/chat&api_key=SECRET_TOKEN", "https://host/<redacted>"),
        (
            "https://host/a&api_key=SECRET_TOKEN?b=1",
            "https://host/<redacted>?<redacted>",
        ),
        ("http://example.com:8080&token=SECRET_TOKEN", "http://example.com:8080?<redacted>"),
    ):
        out = redact(value)
        assert "SECRET_TOKEN" not in out, f"key 运行泄漏凭证：{out}"
        assert out == expected, f"脱敏结果不符：{out}"

    assert redact("https://img.example.com/a.jpg") == "https://img.example.com/<redacted>"


def test_redact_url_drops_credentials_in_path() -> None:
    utils_mod = _load_utils()
    rendered = utils_mod.redact_url(
        "https://api.telegram.org/bot123456:SECRET/sendMessage",
    )

    assert "123456:SECRET" not in rendered
    assert "sendMessage" not in rendered


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        # 相对路径形态：urllib3 MaxRetryError 的标准文本
        ("Max retries exceeded with url: /v1/chat?api_key=TOK0", "TOK0"),
        # 本机端点：lookbehind 会拒掉黏在单词字符后的起点
        ("Connection to 127.0.0.1:11434/v1/chat?api_key=TOK1 failed", "TOK1"),
        # 裸 host 带 query 无路径
        ("connect to api.example.com?api_key=TOK2 refused", "TOK2"),
        # 单标签主机
        ("localhost/v1/chat?api_key=TOK3 boom", "TOK3"),
        # 无 scheme 的 IPv6 字面量
        ("[::1]:11434/a?api_key=TOK4 boom", "TOK4"),
        # 无 scheme 的 userinfo：密码不在 query 里
        ("proxy user:PW5@cdn.example.com/a?key=TOK5", "PW5"),
    ],
)
def test_redact_exc_text_catches_credentials_without_url_boundary(text: str, secret: str) -> None:
    """不依赖 URL 边界也要抹掉凭证，普通文本原样保留。

    按 URL 边界切分时，无路径的 ``host?key=``、单标签主机、无 scheme 的 IPv6
    与 ``user:pass@host`` 都切不出 URL 形状，凭证会整段进日志与 reason。
    """
    redacted = _load_utils().redact_exc_text(RuntimeError(text))
    assert secret not in redacted, f"凭证原样出网：{redacted}"
    assert "<redacted>" in redacted, f"脱敏标记缺失：{redacted}"
    plain = _load_utils().redact_exc_text(RuntimeError("read /etc/astrbot/config.json failed"))
    assert plain == "read /etc/astrbot/config.json failed", f"无凭证文本被改写：{plain}"


@pytest.mark.parametrize(
    "value",
    [
        # 畸形 scheme：urlparse 认得出 scheme 却给不出 netloc
        "http:/cdn.example.com/img.png?api_key=sk-SECRET123",
        # 协议相对 URL：无 scheme，两个斜杠后才是 authority
        "//cdn.example.com/x?api_key=sk-SECRET123",
        # 正常带 query 凭证：happy path
        "https://cdn.example.com/x?api_key=sk-SECRET123",
    ],
)
def test_redact_url_scrubs_scheme_and_netloc_malformed_urls(value: str) -> None:
    """无 netloc 形状不得裸截断放行。

    缺陷形态：``if not parsed.scheme or not parsed.netloc`` 分支里还有
    ``if "://" not in text: return text[:80]`` 的捷径。畸形 scheme 与协议
    相对 URL 都落进这一支，凭证因此原样返回；该文本经 image/parser.py 的
    ``_warn_policy_refusal`` → ``logger.warning`` 直接落日志。
    """
    utils_mod = _load_utils()
    out = utils_mod.redact_url(value)
    assert "sk-" not in out, f"sk- 凭证前缀原样返回：{out}"
    assert "sk-SECRET123" not in out, f"凭证值原样返回：{out}"
    assert "api_key" not in out, f"参数名随值一起泄漏：{out}"
    assert "cdn.example.com" in out, f"丢失 host，无法定位失败来源：{out}"
    assert "<redacted>" in out, f"未标记已剥离的 query：{out}"
    assert len(out) <= utils_mod.LOG_URL_MAX_CHARS


@pytest.mark.parametrize(
    "value",
    [
        # 兜底分支：协议相对 URL（无 scheme，两个斜杠后才是 authority）
        "//host\x1fFAKE-LOG-LINE/a?api_key=sk-SECRET123",
        # 兜底分支：畸形 scheme（urlparse 认得出 scheme 却给不出 netloc）
        "http:/host\x00FAKE-LOG-LINE/a?api_key=sk-SECRET123",
        # happy path：urlparse 不剥离 netloc 里的 \x0b / \x7f
        "https://host\x0bFAKE-LOG-LINE/a?api_key=sk-SECRET123",
        "https://host\x7fFAKE-LOG-LINE/a?api_key=sk-SECRET123",
        # 换行是最直接的伪造行载体，两种分支都要挡
        "https://host\rFAKE-LOG-LINE/a?api_key=sk-SECRET123",
        "//host\nFAKE-LOG-LINE/a?api_key=sk-SECRET123",
        # str.splitlines 与 \s 同样把 C1 与 U+2028/U+2029 当换行
        "https://host\u0085FAKE-LOG-LINE/a?api_key=sk-SECRET123",
        "https://host\u2028FAKE-LOG-LINE/a?api_key=sk-SECRET123",
        "https://host\u2029FAKE-LOG-LINE/a?api_key=sk-SECRET123",
    ],
)
def test_redact_url_strips_control_chars(value: str) -> None:
    """脱敏结果不得含控制字符或行分隔符，否则可疑 URL 能伪造日志行。

    兜底按字符边界重拼文本、happy path 复制 netloc，两者都会原样保留这些
    字符；日志查看器把换行当成记录边界，对端只要在 URL 里塞一个换行就能
    凭空多写一行日志。只挡 C0 不够：C1 与 U+2028/U+2029 同样是换行。
    """
    utils_mod = _load_utils()
    out = utils_mod.redact_url(value)
    assert out.splitlines() == [out], f"脱敏结果仍可被拆成多行：{out!r}"
    assert len(out.splitlines()) == 1
    assert "sk-SECRET123" not in out
