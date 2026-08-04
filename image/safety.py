"""Content-based image validation shared by the recorder bridge and parser.

File extensions and HTTP ``content-type`` headers are both attacker- or
adapter-influenced, so they are treated as hints only. The authoritative check
is the leading magic bytes of the payload itself: if the bytes do not describe a
known image container, the data never becomes a ``data:`` URL and therefore
never reaches a Vision provider.
"""

from __future__ import annotations

# Longest prefix we need to inspect (RIFF/WEBP needs 12 bytes).
IMAGE_SNIFF_BYTES = 12

_JPEG_PREFIX = b"\xff\xd8\xff"
_PNG_PREFIX = b"\x89PNG\r\n\x1a\n"
_GIF_PREFIXES = (b"GIF87a", b"GIF89a")
_BMP_PREFIX = b"BM"
_RIFF_PREFIX = b"RIFF"
_WEBP_TAG = b"WEBP"


def sniff_image_mime(data: bytes) -> str:
    """Return the MIME type implied by an image's magic bytes, else ``""``.

    Args:
        data: Leading bytes of the candidate file. At least ``IMAGE_SNIFF_BYTES``
            bytes are needed to recognise every supported container.

    Returns:
        A concrete ``image/*`` MIME type, or an empty string when the payload is
        not a recognised image.
    """
    if not data:
        return ""
    if data.startswith(_JPEG_PREFIX):
        return "image/jpeg"
    if data.startswith(_PNG_PREFIX):
        return "image/png"
    if data.startswith(_GIF_PREFIXES):
        return "image/gif"
    if data.startswith(_RIFF_PREFIX) and data[8:12] == _WEBP_TAG:
        return "image/webp"
    if data.startswith(_BMP_PREFIX):
        return "image/bmp"
    return ""


def is_image_payload(data: bytes) -> bool:
    """Return whether the payload's magic bytes describe a known image."""
    return bool(sniff_image_mime(data))
