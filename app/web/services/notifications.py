"""进程内前端通知缓冲。"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

_NOTIFICATIONS: deque[dict[str, Any]] = deque(maxlen=200)
_NOTIFICATION_SEQ = 0

__all__ = ["get_notifications", "push_notification"]


def push_notification(message: str, kind: str = "info", data: dict[str, Any] | None = None) -> None:
    """登记一条供控制台轮询显示的通知。"""
    global _NOTIFICATION_SEQ
    _NOTIFICATION_SEQ += 1
    _NOTIFICATIONS.append(
        {
            "id": _NOTIFICATION_SEQ,
            "message": message,
            "kind": kind,
            "data": data or {},
            "created_at": time.time(),
        }
    )


def get_notifications(since_id: int = 0) -> list[dict[str, Any]]:
    """返回指定序号之后的通知。"""
    return [item for item in _NOTIFICATIONS if int(item["id"]) > since_id]
