"""录制转写."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.web import service
from app.web.services.transcripts import TranscriptNotFoundError, TranscriptRetranscribeConflict

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


router = APIRouter()


class TranscriptCorrectionRequest(BaseModel):
    """人工转写纠错请求。"""

    corrected_text: str = Field(min_length=1, max_length=200_000)
    aliases: dict[str, str] = Field(default_factory=dict)
    learn_dictionary: bool = True


class ReanalysisRequest(BaseModel):
    """场次重分析请求。"""

    reason: str = Field(default="manual", min_length=1, max_length=200)
    retranscribe: bool = False


@router.get("/recording")
def get_recording() -> list[dict[str, Any]]:
    """返回录制会话状态列表。"""
    return service.recording_status()


@router.get("/sessions/timeline")
def get_session_timelines(limit: int = 30, room_db_id: int | None = None) -> list[dict[str, Any]]:
    """返回最近录制场次的高光时间线概览。"""
    from app.web.services.timeline import list_session_timelines

    return list_session_timelines(limit=limit, room_db_id=room_db_id)


@router.get("/sessions/{session_id}/timeline")
def get_session_timeline(session_id: int, include_rejected: bool = False) -> dict[str, Any]:
    """返回指定录制场次的 GMT+8 高光时间轴。"""
    from app.web.services.timeline import get_session_timeline as load_timeline

    try:
        return load_timeline(session_id, include_rejected=include_rejected)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.patch("/transcripts/{transcript_id}")
def correct_transcript(
    transcript_id: int,
    payload: TranscriptCorrectionRequest,
    request: Request,
) -> dict[str, Any]:
    """保存人工纠错、回流房间词典并安全重排场次分析。"""
    actor = str(getattr(request.state, "auth_user", "local-admin"))
    try:
        return service.correct_transcript(
            transcript_id,
            payload.corrected_text,
            aliases=payload.aliases,
            learn_dictionary=payload.learn_dictionary,
            actor=actor,
        )
    except TranscriptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/reanalyze")
def reanalyze_session(session_id: int, payload: ReanalysisRequest) -> dict[str, int | bool]:
    """持久化重分析请求；旧流水线稳定后整场重排，且保留人工成果。"""
    from app.analysis.reanalysis import request_session_reanalysis

    try:
        requested = request_session_reanalysis(
            session_id,
            reason=payload.reason,
            retranscribe=payload.retranscribe,
        )
        return {"session_id": session_id, "requested": requested}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/danmaku")
def get_danmaku(limit: int = 50, session_id: int | None = None) -> dict[str, Any]:
    """返回最近弹幕与各会话弹幕热度统计。

    :param limit: 返回的最近弹幕条数。
    :param session_id: 仅查询指定会话(可选)。
    """
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.danmaku_overview(limit=limit, session_id=session_id)
