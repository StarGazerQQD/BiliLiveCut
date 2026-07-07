"""Transcripts (V0.1.14.1)."""

from __future__ import annotations

from typing import Any

from sqlmodel import select

from app.db.models import (
    Transcript,
)
from app.db.session import get_session


def list_transcripts(limit: int = 30) -> list[dict[str, Any]]:
    """列出最近的转写文本(用于"实时转写"视图)。

    :param limit: 数量上限。
    :returns: 转写字典列表(按时间降序)。
    """
    with get_session() as db:
        rows = db.exec(
            select(Transcript).order_by(Transcript.created_at.desc())  # type: ignore[attr-defined]
        ).all()[:limit]
    return [
        {
            "id": t.id,
            "segment_id": t.segment_id,
            "language": t.language,
            "text": t.text,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in rows
    ]
