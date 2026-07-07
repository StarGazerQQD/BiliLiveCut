"""录制转写 (V0.1.14.1)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.web import service

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


@router.get("/danmaku")
def get_danmaku(limit: int = 50, session_id: int | None = None) -> dict[str, Any]:
    """返回最近弹幕与各会话弹幕热度统计。

    :param limit: 返回的最近弹幕条数。
    :param session_id: 仅查询指定会话(可选)。
    """
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.danmaku_overview(limit=limit, session_id=session_id)

