"""
简单的 LRU 缓存实现（纯内存）
"""

from collections import OrderedDict


class ImageCache:
    """简单的 LRU 缓存

    使用 OrderedDict 实现的内存 LRU 缓存，用于避免重复解析相同图片。

    并发约束：仅供单事件循环内串行使用，勿跨线程共享（无锁保护）。

    Attributes:
        _cache: 有序字典存储缓存项
        _max_size: 最大缓存容量

    Examples:
        >>> cache = ImageCache(max_size=50)
        >>> cache.put("key1", "description1")
        >>> cache.get("key1")
        'description1'
    """

    def __init__(self, max_size: int = 50):
        """初始化缓存

        Args:
            max_size: 最大缓存容量
        """
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> str | None:
        """获取缓存值

        Args:
            key: 缓存键

        Returns:
            缓存的描述文本，不存在返回 None
        """
        if key in self._cache:
            # 移到末尾（标记为最近使用）
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, value: str) -> None:
        """存入缓存

        Args:
            key: 缓存键
            value: 描述文本
        """
        if key in self._cache:
            # 更新并移到末尾
            self._cache.move_to_end(key)
        self._cache[key] = value

        # 超出容量时移除最旧的（最前面的）
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
