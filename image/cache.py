"""解析结果的内存 LRU，避免同一图片重复问模型。"""

from collections import OrderedDict


class ImageCache:
    """单事件循环内的有序字典 LRU；无锁，勿跨线程共享。"""

    def __init__(self, max_size: int = 50, max_bytes: int | None = None) -> None:
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_size = max(0, int(max_size))
        self._max_bytes = None if max_bytes is None else max(0, int(max_bytes))
        self._bytes_used = 0

    @staticmethod
    def _value_size(value: str) -> int:
        return len(value.encode("utf-8"))

    @property
    def bytes_used(self) -> int:
        """当前缓存值按 UTF-8 编码计算的字节数。"""
        return self._bytes_used

    def get(self, key: str) -> str | None:
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return self._cache[key]

    def put(self, key: str, value: str) -> bool:
        """写入值并按条目数和字节预算驱逐最旧条目。

        返回值表示该值是否最终留在缓存中；超出整个预算的单值不会被缓存。
        """
        value = str(value)
        value_size = self._value_size(value)
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._bytes_used -= self._value_size(previous)
        if self._max_size == 0 or (self._max_bytes is not None and value_size > self._max_bytes):
            return False

        self._cache[key] = value
        self._bytes_used += value_size
        while self._cache and (
            len(self._cache) > self._max_size
            or (self._max_bytes is not None and self._bytes_used > self._max_bytes)
        ):
            _, removed = self._cache.popitem(last=False)
            self._bytes_used -= self._value_size(removed)
        return key in self._cache
