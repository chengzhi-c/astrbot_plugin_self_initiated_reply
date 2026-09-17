"""图片清理的事件循环友好性契约。

背景：``ImageParser.cleanup_source_cache`` 含 3 遍 ``rglob("*")`` + 全量
``stat()``，配额上限 256MB。两处不得在协程内同步执行该遍历：
① ``run_image_cleanup``（async，磁盘遍历必须进线程）；
② ``cleanup_events_if_needed``（由 ``on_message`` 协程同步调用）。

本文件锚定修复后的契约：磁盘遍历只允许经线程执行，事件清理路径不得触碰磁盘。
断言基于"遍历发生在哪个线程"这一可观测事实，而非实现细节。

同一断言形状后来扩到其它磁盘 IO（data URL 物化、配置写盘、webapi 端点写盘）：
它们共享同一条契约——fsync/遍历不得跑在事件循环线程上。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from .test_session_scheduler import _make_scheduler


def _patch_cleanup_probe(scheduler_mod, calls: list[int]):
    """把 cleanup_source_cache 换成记录调用线程的探针。"""
    original = scheduler_mod.ImageParser.cleanup_source_cache

    def probe(root, **kwargs):
        calls.append(threading.get_ident())
        return 0

    scheduler_mod.ImageParser.cleanup_source_cache = probe
    return original


async def test_run_image_cleanup_offloads_disk_walk_to_thread(tmp_path: Path) -> None:
    """run_image_cleanup 的磁盘遍历必须在工作线程执行，不占用事件循环。"""
    import sys

    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler_mod = sys.modules[type(scheduler).__module__]
    threads: list[int] = []
    original = _patch_cleanup_probe(scheduler_mod, threads)
    try:
        loop_thread = threading.get_ident()
        await scheduler.run_image_cleanup()
        assert threads, "磁盘清理未被调用"
        assert threads[0] != loop_thread, "磁盘遍历在事件循环线程内执行——rglob+stat 会阻塞所有会话"
    finally:
        scheduler_mod.ImageParser.cleanup_source_cache = original


async def test_cleanup_events_does_not_touch_disk(tmp_path: Path) -> None:
    """事件清理由 on_message 协程同步调用，不得触发磁盘遍历。"""
    import sys

    _, models, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler_mod = sys.modules[type(scheduler).__module__]
    threads: list[int] = []
    original = _patch_cleanup_probe(scheduler_mod, threads)
    try:
        scheduler._last_cleanup = models.now_ts() - 4000.0  # 跨过 1h 节流门槛
        scheduler.cleanup_events_if_needed()
        assert not threads, (
            "事件清理路径触发了磁盘遍历——该方法由 on_message 同步调用，会阻塞消息热路径"
        )
    finally:
        scheduler_mod.ImageParser.cleanup_source_cache = original


async def test_cleanup_events_still_prunes_image_index(tmp_path: Path) -> None:
    """移除磁盘清理后，纯内存的图片索引回收必须保留（否则索引只增不减）。"""
    from collections import deque

    _, models, scheduler, _, _ = _make_scheduler(tmp_path)
    now = models.now_ts()
    stale_umo = "qq:GroupMessage:stale"
    # 远超 vision_image_age_sec 的索引条目
    scheduler._recent_image_events[stale_umo] = deque([(now - 100000.0, [])])
    scheduler._last_cleanup = now - 4000.0
    scheduler.cleanup_events_if_needed()
    assert stale_umo not in scheduler._recent_image_events, "过期图片索引未被回收"


async def test_image_cleanup_serialized_under_concurrency(tmp_path: Path) -> None:
    """to_thread 引入真实交错后，锁必须防止两次清理并发 unlink 同一文件。"""
    import sys

    _, _, scheduler, _, _ = _make_scheduler(tmp_path)
    scheduler_mod = sys.modules[type(scheduler).__module__]
    original = scheduler_mod.ImageParser.cleanup_source_cache
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()

    def probe(root, **kwargs):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        # 留出真实重叠窗口：无锁保护时另一协程的探针会在此期间进入
        threading.Event().wait(0.05)
        with lock:
            concurrent["now"] -= 1
        return 0

    scheduler_mod.ImageParser.cleanup_source_cache = probe
    try:
        await asyncio.gather(scheduler.run_image_cleanup(), scheduler.run_image_cleanup())
        assert concurrent["max"] == 1, (
            f"清理并发重叠 {concurrent['max']} 次——并发 unlink 同一文件会互相报错"
        )
    finally:
        scheduler_mod.ImageParser.cleanup_source_cache = original


async def test_prepare_materializes_data_url_off_the_event_loop(tmp_path: Path) -> None:
    """prepare() 物化 data URL 必须离开事件循环线程。

    远程图经 _resolve_image_url 变成 data: 后走这条路径；同步解码+写盘
    会阻塞整 bot，与 cleanup 已钉死的 to_thread 契约同一类问题。
    """
    from .test_vision_parser_gaps import PNG_DATA_URL, _load_modules, _make_parser

    _, image, _ = _load_modules()
    parser = _make_parser(image, tmp_path)
    threads: list[int] = []
    original = parser._materialize_data_url

    def probe(data_url: str):
        threads.append(threading.get_ident())
        return original(data_url)

    async def resolved(_info):
        return PNG_DATA_URL

    parser._resolve_image_url = resolved
    parser._materialize_data_url = probe
    loop_thread = threading.get_ident()
    info = image.ImageInfo(url="https://cdn.example/x.png")
    assert await parser.prepare(info) is True
    assert threads, "prepare() 未物化 data URL"
    assert threads[0] != loop_thread, (
        "prepare() 在事件循环线程内物化 data URL——base64 解码+写盘会阻塞所有会话"
    )
    assert info.prepared_source


async def test_settings_config_write_offloads_file_io_to_thread(tmp_path: Path) -> None:
    """配置写盘的文件 IO 必须在工作线程执行，宿主同步留在事件循环。

    POST /config 与 /on|/off 都在 async 上下文写配置：JSON 落盘含 fsync，
    在事件循环线程内执行会阻塞所有会话。宿主 save_config 的线程安全性未知，
    故只有纯本插件的文件写进线程，宿主同步仍在循环内。
    """
    from .test_storage_and_umo import _load_modules

    models, _, storage = _load_modules()
    threads: list[int] = []
    original = storage.write_json_atomic

    def probe(path, data):
        threads.append(threading.get_ident())
        return original(path, data)

    storage.write_json_atomic = probe
    try:
        settings = models.Settings.from_config({})
        ok = await storage.apersist_settings_config(tmp_path / "cfg.json", {}, settings)
        assert ok is True
        assert threads, "配置写盘未发生"
        assert threads[0] != threading.get_ident(), (
            "配置写盘在事件循环线程内执行——fsync 会阻塞所有会话"
        )
    finally:
        storage.write_json_atomic = original


def test_startup_image_cleanup_offloads_disk_walk_when_loop_runs(tmp_path: Path) -> None:
    """插件构造期的启动清理：事件循环在跑时不得同步遍历磁盘。

    历史实现无条件同步调 ``cleanup_image_sources``（rglob + 全量 stat，配额
    256MB），插件每次 reload 都会在宿主事件循环上阻塞数百毫秒。契约：有
    运行中的事件循环 → 经后台任务走 ``run_image_cleanup``（磁盘部分由其
    内部 to_thread 承担）；无循环（同步加载的宿主）→ 保持同步路径。
    """
    from .host_stubs import load_main, until, with_plugin

    main = load_main()
    cls = main.SessionScheduler
    calls: list[str] = []
    original_sync = cls.cleanup_image_sources
    original_async = cls.run_image_cleanup

    async def async_probe(self) -> int:
        calls.append("async")
        return 0

    def sync_probe(self, *, now: float | None = None) -> int:
        calls.append("sync")
        return 0

    cls.cleanup_image_sources = sync_probe  # type: ignore[method-assign]
    cls.run_image_cleanup = async_probe  # type: ignore[method-assign]
    try:

        async def scenario(plugin, _main):
            # 后台清理任务在构造后由事件循环调度；等它真正跑到调用点
            await until(lambda: "async" in calls)
            return None

        with_plugin(tmp_path, scenario)
        assert "sync" not in calls, (
            "构造期在事件循环内同步执行了磁盘遍历（rglob+stat 阻塞插件加载）"
        )
        assert "async" in calls, "构造期没有调度后台清理（run_image_cleanup 未被调用）"
    finally:
        cls.cleanup_image_sources = original_sync
        cls.run_image_cleanup = original_async


def test_mutating_webapi_endpoints_write_off_the_event_loop(tmp_path: Path) -> None:
    """会落盘的 webapi POST 端点，写盘必须离开事件循环线程。

    ``POST /config`` 与 ``POST /ui/theme`` 都在 async 上下文写 JSON：写盘含
    fsync，跑在事件循环线程上会阻塞所有会话（同 ``apersist_settings_config``
    已钉的同一类问题）。

    两个绑定点都要换：``webapi`` 用 ``from .storage import write_json_atomic``
    在导入期就持了自己的副本，只换 ``storage`` 会完全观测不到 ui-prefs 的写盘，
    断言退化成恒绿的假契约。每个端点另断言至少发生一次写盘，否则端点改名或
    提前 return 会让用例静默失去覆盖对象。
    """
    from .host_stubs import with_plugin

    async def scenario(plugin, _main):
        import sys

        # 宿主桩由 make_plugin 装入，此处才保证存在（模块顶层读会在单独跑本用例时 KeyError）。
        web = sys.modules["astrbot.api.web"]
        package = type(plugin).__module__.rsplit(".", 1)[0]
        webapi = sys.modules[f"{package}.webapi"]
        storage = sys.modules[f"{package}.storage"]
        originals = {storage: storage.write_json_atomic, webapi: webapi.write_json_atomic}
        threads: list[int] = []

        def make_probe(original):
            def probe(path, data):
                threads.append(threading.get_ident())
                return original(path, data)

            return probe

        loop_thread = threading.get_ident()
        try:
            for module in (storage, webapi):
                module.write_json_atomic = make_probe(originals[module])
            endpoints = (
                ("POST /config", plugin._api_post_config, {"cooldown_sec": 7}),
                ("POST /ui/theme", plugin._api_post_ui_theme, {"theme": "dark"}),
            )
            for label, drive, payload in endpoints:
                before = len(threads)
                web.request.payload = payload
                result = await drive()
                assert result["ok"] is True, f"{label} 未成功：{result}"
                assert len(threads) > before, f"{label} 没有发生写盘——用例已失去覆盖对象"
                assert all(thread != loop_thread for thread in threads[before:]), (
                    f"{label} 的写盘在事件循环线程内执行——fsync 会阻塞所有会话"
                )
        finally:
            for module, original in originals.items():
                module.write_json_atomic = original
            web.request.payload = {}

    with_plugin(tmp_path, scenario)
