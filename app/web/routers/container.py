"""设置上传."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from app.web.services import clips as clips_service
from app.web.services import notifications as notifications_service
from app.web.services import publishing as publishing_service
from app.web.services import settings as settings_service

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


class WebPortRequest(BaseModel):
    """下次启动时生效的 Web 端口。"""

    model_config = ConfigDict(extra="forbid")

    web_port: int


router = APIRouter()


from app.core.configuration import (  # noqa: E402
    ConfigurationChange,
    ConfigurationConflict,
    configuration_view,
    save_configuration,
)


@router.get("/settings/configuration")
def get_configuration() -> dict[str, Any]:
    """返回完整业务配置注册表及脱敏后的有效值。"""
    return configuration_view()


@router.patch("/settings/configuration")
def patch_configuration(req: ConfigurationChange) -> dict[str, Any]:
    """原子保存业务配置；过期表单返回冲突而不覆盖新设置。"""
    try:
        return save_configuration(req)
    except ConfigurationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    """返回可切换的运行时开关与上传配置概览。"""
    return settings_service.get_settings_view()


@router.patch("/settings/port")
def patch_web_port(req: WebPortRequest) -> dict[str, Any]:
    """更新下次启动的端口。"""
    try:
        return settings_service.update_web_port(req.web_port)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/uploads")
def get_uploads(limit: int = 50) -> list[dict[str, Any]]:
    """返回上传任务队列。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return publishing_service.list_uploads(limit=limit)


@router.post("/clips/{clip_id}/enqueue")
async def enqueue_upload(clip_id: int, request: Request) -> dict[str, Any]:
    """把成品上传提交到后台作业。"""
    from app.db.entities import FinalClip
    from app.db.session import get_session
    from app.web.services.background_jobs import web_job_manager
    from app.web.services.review_workflow import review_actor

    with get_session() as db:
        if db.get(FinalClip, clip_id) is None:
            raise HTTPException(status_code=404, detail="成片不存在")
    actor, _ = review_actor(request)
    job = await web_job_manager.enqueue(
        "clip_upload",
        {"clip_id": clip_id},
        label=f"成片 #{clip_id} 上传",
        owner=actor,
        dedup_key=f"clip-upload:{clip_id}",
        cancellable_while_running=False,
    )
    return {"status": "accepted", "job": job}


@router.post("/uploads/{task_id}/retry")
async def retry_upload(task_id: int, request: Request) -> dict[str, Any]:
    """把上传重试提交到后台作业。"""
    from app.db.entities import UploadTask
    from app.db.session import get_session
    from app.web.services.background_jobs import web_job_manager
    from app.web.services.review_workflow import review_actor

    with get_session() as db:
        if db.get(UploadTask, task_id) is None:
            raise HTTPException(status_code=404, detail="上传任务不存在")
    actor, _ = review_actor(request)
    job = await web_job_manager.enqueue(
        "upload_retry",
        {"upload_task_id": task_id},
        label=f"上传任务 #{task_id} 重试",
        owner=actor,
        dedup_key=f"upload-retry:{task_id}",
        cancellable_while_running=False,
    )
    return {"status": "accepted", "job": job}


@router.get("/notifications")
def get_notifications(since_id: int = 0) -> list[dict[str, Any]]:
    """返回比 since_id 更新的通知(供前端轮询弹出提示)。"""
    return notifications_service.get_notifications(since_id=since_id)


@router.post("/open-clips-dir")
def open_clips_dir(
    session_id: int | None = Query(default=None, gt=0),
    unknown_session: bool = False,
) -> dict[str, str]:
    """在本机文件管理器打开切片目录。"""
    if session_id is not None and unknown_session:
        raise HTTPException(status_code=422, detail="场次筛选与未关联场次不能同时使用")
    try:
        return {"clips_dir": clips_service.open_clips_directory(session_id=session_id, unknown_session=unknown_session)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
