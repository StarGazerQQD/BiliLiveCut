"""录制预约 (V0.1.14.1)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.web import service


class ScheduleRequest(BaseModel):
    room_id: int
    scheduled_at: str
    recurrent: str = ""


router = APIRouter()

@router.get("/schedules")
def get_schedules() -> list[dict[str, Any]]:
    """返回所有录制预约。"""
    return service.list_schedules()


@router.post("/schedules")
def create_schedule(req: ScheduleRequest) -> dict[str, Any]:
    """创建一个录制预约。"""
    try:
        return service.create_schedule(req.room_id, req.scheduled_at, req.recurrent)
    except (ValueError, Exception) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int) -> dict[str, str]:
    """删除一个录制预约。"""
    try:
        service.delete_schedule(schedule_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted"}


@router.patch("/schedules/{schedule_id}")
def patch_schedule(schedule_id: int, enabled: bool) -> dict[str, Any]:
    """启用/禁用录制预约。"""
    try:
        return service.toggle_schedule(schedule_id, enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

