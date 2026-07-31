"""Transcripts."""

from __future__ import annotations

import json
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
    result: list[dict[str, Any]] = []
    for transcript in rows:
        refinement: dict[str, Any] = {}
        if transcript.auxiliary_json:
            try:
                auxiliary = json.loads(transcript.auxiliary_json)
            except (json.JSONDecodeError, TypeError):
                auxiliary = {}
            if isinstance(auxiliary, dict) and isinstance(auxiliary.get("transcript_refinement"), dict):
                refinement = auxiliary["transcript_refinement"]
        result.append(
            {
                "id": transcript.id,
                "segment_id": transcript.segment_id,
                "language": transcript.language,
                "text": transcript.text,
                "raw_text": transcript.final_text or transcript.text,
                "summary": str(refinement.get("summary", "")),
                "llm_refined": refinement.get("applied") is True,
                "primary_backend": transcript.primary_backend,
                "created_at": transcript.created_at.isoformat() if transcript.created_at else None,
            }
        )
    return result
