"""设置上传 (V0.1.14.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.web import service

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


class SettingsRequest(BaseModel):
    """运行时开关与上传配置请求体。"""

    biliup_enabled: bool | None = None
    auto_upload: bool | None = None
    trend_schedule_enabled: bool | None = None
    trend_schedule_start: str | None = None
    trend_schedule_end: str | None = None
    trend_schedule_interval_min: int | None = None


router = APIRouter()


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    """返回可切换的运行时开关与上传配置概览。"""
    return service.get_settings_view()


@router.patch("/settings")
def patch_settings(req: SettingsRequest) -> dict[str, Any]:
    """更新运行时开关(含 biliup 上传总开关、网感定时采集)。"""
    try:
        return service.update_settings(req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/uploads")
def get_uploads(limit: int = 50) -> list[dict[str, Any]]:
    """返回上传任务队列。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.list_uploads(limit=limit)


@router.post("/clips/{clip_id}/enqueue")
async def enqueue_upload(clip_id: int) -> dict[str, Any]:
    """把成品加入上传队列并执行。"""
    return await service.enqueue_clip_upload(clip_id)


@router.post("/uploads/{task_id}/retry")
async def retry_upload(task_id: int) -> dict[str, Any]:
    """重试一个上传任务。"""
    return await service.retry_upload(task_id)


@router.get("/notifications")
def get_notifications(since_id: int = 0) -> list[dict[str, Any]]:
    """返回比 since_id 更新的通知(供前端轮询弹出提示)。"""
    return service.get_notifications(since_id=since_id)


@router.post("/open-clips-dir")
def open_clips_dir() -> dict[str, str]:
    """在本机文件管理器打开切片目录。"""
    return {"clips_dir": service.open_clips_directory()}
