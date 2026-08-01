"""
图片信息数据模型
"""

from dataclasses import dataclass


@dataclass
class ImageInfo:
    """图片信息数据类
    
    存储单张图片的完整信息，包括 URL、文件路径和元数据。
    
    Attributes:
        url: 图片URL（网络图片）
        file_path: 图片本地文件路径（本地图片）
        format: 图片格式（jpg/jpeg/png/gif/webp）
        message_id: 关联的消息ID
        sender_id: 发送者ID
        timestamp: 时间戳
    
    Examples:
        >>> info = ImageInfo(
        ...     url="https://example.com/image.jpg",
        ...     format="jpg",
        ...     message_id="msg_123"
        ... )
        >>> info.has_url
        True
    """
    
    url: str = ""
    file_path: str = ""
    format: str = ""
    message_id: str = ""
    sender_id: str = ""
    timestamp: float = 0.0
    
    @property
    def has_url(self) -> bool:
        """是否有URL
        
        Returns:
            是否有URL
        """
        return bool(self.url)
    
    @property
    def has_file_path(self) -> bool:
        """是否有文件路径
        
        Returns:
            是否有文件路径
        """
        return bool(self.file_path)
    
    @property
    def has_any_source(self) -> bool:
        """是否有任何可用的图片源
        
        Returns:
            是否有URL或文件路径
        """
        return self.has_url or self.has_file_path
    
    def cache_key(self) -> str:
        """生成缓存键
        
        Returns:
            基于URL或文件路径的缓存键
        """
        if self.url:
            return f"url:{self.url}"
        if self.file_path:
            return f"file:{self.file_path}"
        return f"id:{self.message_id}"
