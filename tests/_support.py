"""跨用例文件共享的引导：模块清单、默认会话事件桩与识图夹具数据。

宿主 stub 与加载原语归 ``host_stubs``，本文件只放「多个文件都要用的同一组插件
模块」、同一个默认会话和同一份图片夹具，避免把某个千行主题用例文件当依赖库私取
符号。动态包名仍由各文件自持：同一份源码在不同文件里用不同包名隔离 ``sys.modules``
是刻意的，收成一个全局包名会让跨测试的对象身份意外重合。
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from .host_stubs import FakeEvent, load_modules

# 识图链与配置面用例共用的模块集合。
CORE_MODULES = ("adapters", "image", "models")

# 指令面与配置面安全用例的模块集合。
COMMAND_SURFACE_MODULES = ("models", "utils", "commands", "image", "image.recorder_bridge")

UMO = "fake:group:123"

# 最小合法 PNG：字节头正确、体积远小于任何上限档位，识图用例只关心「是不是一张图」。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")


def core_loader(package_name: str) -> Callable[[], tuple[ModuleType, ...]]:
    """按调用方自己的包名生成"加载共用模块集合"的函数。

    模块清单在本文件单源，包名仍由各文件持有（隔离语义见模块 docstring）。
    """
    return lambda: load_modules(package_name, *CORE_MODULES)


def command_surface_loader(package_name: str) -> Callable[[], tuple[ModuleType, ...]]:
    """指令面/配置面用例的模块集合加载器（同 ``core_loader`` 的包名口径）。"""
    return lambda: load_modules(package_name, *COMMAND_SURFACE_MODULES)


def make_event(umo: str = UMO, **kwargs: Any) -> FakeEvent:
    return FakeEvent(umo=umo, **kwargs)


def make_parser(image: ModuleType, tmp_path: Path, **kwargs: Any) -> Any:
    """识图解析器夹具：缓存目录固定在 ``tmp_path/image_cache``。

    清理与物化两条线程契约用例都按这个形状构造，构造细节散到各文件后，夹具改动
    只会让一部分用例跟上。
    """
    return image.ImageParser(object(), source_cache_dir=tmp_path / "image_cache", **kwargs)
