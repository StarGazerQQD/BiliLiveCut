"""Logs (V0.1.14.1)."""
from __future__ import annotations

from typing import Any

from sqlmodel import select

from app.db.models import (
    SystemLog,
)
from app.db.session import get_session


def list_logs(limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
    """列出系统日志(WARNING 及以上写入了 system_logs)。

    :param limit: 数量上限。
    :param level: 可选级别过滤。
    :returns: 日志字典列表(按时间降序)。
    """
    with get_session() as db:
        stmt = select(SystemLog).order_by(SystemLog.created_at.desc())  # type: ignore[attr-defined]
        if level:
            stmt = stmt.where(SystemLog.level == level)
        rows = db.exec(stmt).all()[:limit]
    return [
        {
            "id": x.id,
            "level": x.level,
            "module": x.module,
            "event": x.event,
            "message": x.message,
            "created_at": x.created_at.isoformat() if x.created_at else None,
        }
        for x in rows
    ]


# --------------------------------------------------------------------------- #
# 录制预约(V0.1.2)
# --------------------------------------------------------------------------- #
