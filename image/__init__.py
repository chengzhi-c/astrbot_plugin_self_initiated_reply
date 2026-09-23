"""图片识别：提取、冻结、描述。"""

from ._support import (
    ImageCache,
    ImageInfo,
    format_image_context,
    sniff_image_mime,
)
from .extractor import ImageExtractor
from .parser import ImageParser

# 传输层函数（to_data_url）刻意不出现在 façade：它只被 parser / recorder_bridge
# 从 ._support 直接 import，是 data URL 装配的内部细节，不是本包的公开能力。
__all__ = [
    "ImageInfo",
    "ImageCache",
    "ImageExtractor",
    "ImageParser",
    "format_image_context",
    "sniff_image_mime",
]
