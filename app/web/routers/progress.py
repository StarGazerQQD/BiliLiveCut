"""进度学习 (V0.1.14.1)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.web import service

router = APIRouter()

@router.get("/progress")
def get_progress(session_id: int | None = None) -> dict[str, Any]:
    """返回录制→转写→评分的进度统计。"""
    return service.pipeline_progress(session_id=session_id)


# ----------------------------- V0.1.2 新增:阈值自学习 ----------------------------- #
@router.get("/rooms/{db_id}/threshold-learning")
def threshold_learning(db_id: int) -> dict[str, Any]:
    """返回某房间的阈值自学习摘要。"""
    return service.threshold_learning_status(db_id)

