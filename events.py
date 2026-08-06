"""消息事件判定（自 main.py 拆分，ticket 06）。

消息接收的纯判定部分：忽略判定（自消息/命令/纯图无识图/忽略名单/直接
点名）。与实例状态无关，纯函数便于独立单测；main.py 保留同名委托壳。
"""

from __future__ import annotations

from typing import Any

from .utils import event_sender_id, is_explicit_direct_call, is_self_message


def should_ignore_event(
    event: Any,
    text: str,
    *,
    vision_has_images: bool,
    ignored_sender_ids: set[str],
) -> bool:
    """判定一条消息是否应被忽略（不进主动回复观察）。

    - 机器人自己的消息忽略
    - 以 ``/`` 开头的命令消息忽略
    - 纯图片消息没有文本，但在识图开启时仍需观察，否则图片无法进入缓存
    - 忽略名单中的发送者忽略
    - 直接点名（@Bot）的请求消息忽略（由回复请求窗口另行处理）
    """
    if is_self_message(event):
        return True
    if text.startswith("/"):
        return True
    if not text and not vision_has_images:
        return True
    if event_sender_id(event) in ignored_sender_ids:
        return True
    return is_explicit_direct_call(event, text)
