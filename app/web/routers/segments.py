"""录制转写."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.web import service
from app.web.services.transcripts import TranscriptNotFoundError, TranscriptRetranscribeConflict

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


router = APIRouter()


@router.get("/recording")
def get_recording() -> list[dict[str, Any]]:
    """返回录制会话状态列表。"""
    return service.recording_status()


@router.get("/transcripts")
def get_transcripts(limit: int = 30) -> list[dict[str, Any]]:
    """返回最近转写文本。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.list_transcripts(limit=limit)


@router.post("/transcripts/{transcript_id}/retranscribe")
def retranscribe_transcript(transcript_id: int) -> dict[str, int]:
    """安全清理旧自动分析结果并重新排队识别。"""
    try:
        return service.retranscribe_transcript(transcript_id)
    except TranscriptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TranscriptRetranscribeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/danmaku")
def get_danmaku(limit: int = 50, session_id: int | None = None) -> dict[str, Any]:
    """返回最近弹幕与各会话弹幕热度统计。

    :param limit: 返回的最近弹幕条数。
    :param session_id: 仅查询指定会话(可选)。
    """
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.danmaku_overview(limit=limit, session_id=session_id)
