"""解析结果的内存 LRU，避免同一图片重复问模型。"""

from collections import OrderedDict


class ImageCache:
    """单事件循环内的有序字典 LRU；无锁，勿跨线程共享。"""

    def __init__(self, max_size: int = 50) -> None:
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> str | None:
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return self._cache[key]

    def put(self, key: str, value: str) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
