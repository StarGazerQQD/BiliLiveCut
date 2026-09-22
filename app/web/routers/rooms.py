"""直播间管理."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.plugins.live_source import SourceError, SourceRateLimited, SourceUnavailable
from app.sources.registry import source_registry
from app.sources.rooms import room_source_view
from app.web import service
from app.web.services.rooms import RoomNotFoundError, RoomUpdateConflictError


class AddRoomRequest(BaseModel):
    """添加直播间请求体。"""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4096)
    authorized: bool = False
    platform: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class UpdateRoomRequest(BaseModel):
    """直播间配置更新请求体。"""

    model_config = ConfigDict(extra="forbid")

    highlight_threshold: float | None = None
    auto_publish_threshold: float | None = None
    authorized: bool | None = None
    title: str | None = None
    uploader_name: str | None = None
    schedule_enabled: bool | None = None
    auto_threshold_enabled: bool | None = None
    danmaku_sentiment_enabled: bool | None = None
    auto_record: bool | None = None
    auto_analyze: bool | None = None
    auto_render: bool | None = None
    auto_approve: bool | None = None
    auto_upload: bool | None = None
    auto_approve_threshold: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    review_threshold: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    room_config: dict | None = None


class StartRequest(BaseModel):
    """录制/流水线启动请求体。"""

    model_config = ConfigDict(extra="forbid")

    pipeline: bool | None = None
    produce: bool = False


class StopRequest(BaseModel):
    """停止录制请求体。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["graceful", "force"] = "graceful"
    cancel_pending: bool = False


class MarkerRequest(BaseModel):
    """直播中人工高光打点请求体。"""

    model_config = ConfigDict(extra="forbid")

    pre_roll_s: float = Field(default=60.0, ge=0.0, le=300.0, allow_inf_nan=False)
    post_roll_s: float = Field(default=40.0, ge=2.0, le=300.0, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=200)


router = APIRouter()


@router.get("/live-sources")
def live_sources() -> list[dict[str, object]]:
    """列出已注册来源及输入域名，供外部调用方识别平台能力。"""
    return [descriptor.model_dump(mode="json") for descriptor in source_registry.descriptors()]


@router.post("/rooms")
async def create_room(req: AddRoomRequest) -> dict[str, Any]:
    """添加直播间。"""
    try:
        room = await service.add_room(req.url, req.authorized, req.platform)
    except SourceRateLimited as exc:
        headers = {"Retry-After": str(max(1, int(exc.retry_after or 1)))}
        raise HTTPException(status_code=429, detail=str(exc), headers=headers) from exc
    except SourceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ValueError, SourceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": room.id,
        "room_id": room.room_id,
        **room_source_view(room),
        "title": room.title,
        "uploader_name": room.uploader_name,
    }


@router.patch("/rooms/{db_id}")
def patch_room(db_id: int, req: UpdateRoomRequest) -> dict[str, Any]:
    """更新直播间阈值/模式等参数。"""
    try:
        room = service.update_room(db_id, req.model_dump(exclude_none=True))
    except RoomNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RoomUpdateConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, SourceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": room.id,
        "highlight_threshold": room.highlight_threshold,
        "title": room.title,
        "uploader_name": room.uploader_name,
    }


@router.post("/rooms/{db_id}/start")
async def start_recording(db_id: int, req: StartRequest) -> dict[str, str]:
    """启动某直播间录制。"""
    try:
        await service.recorder_manager.start(db_id, pipeline=req.pipeline, produce=req.produce)
    except (ValueError, SourceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "started"}


@router.post("/rooms/{db_id}/arm-auto")
async def arm_auto_recording(db_id: int) -> dict[str, str]:
    """组合开启录制与分析，显式解除暂停后守候开播。"""
    try:
        await service.recorder_manager.arm_auto_recording(db_id)
    except (ValueError, SourceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "waiting_live"}


@router.post("/rooms/{db_id}/stop")
async def stop_recording(db_id: int, req: StopRequest | None = None) -> dict[str, Any]:
    """停止某直播间录制。"""
    payload = req or StopRequest()
    try:
        result = await service.recorder_manager.stop(
            db_id,
            mode=payload.mode,
            pause_auto_restart=True,
            mark_paused=False,
            cancel_pending=payload.cancel_pending,
        )
    except (ValueError, SourceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": result["state"], **result}


@router.post("/rooms/{db_id}/pause")
async def pause_recording(db_id: int) -> dict[str, Any]:
    """优雅暂停录制;恢复时会创建新会话并明确形成时间缺口。"""
    result = await service.recorder_manager.stop(
        db_id,
        mode="graceful",
        pause_auto_restart=True,
        mark_paused=True,
    )
    return {"status": result["state"], **result}


@router.post("/rooms/{db_id}/resume")
async def resume_recording(db_id: int, req: StartRequest) -> dict[str, Any]:
    """恢复人工暂停的房间,并创建新的录制会话。"""
    try:
        await service.recorder_manager.start(db_id, pipeline=req.pipeline, produce=req.produce)
    except (ValueError, SourceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "starting", **service.recorder_manager.status(db_id)}


@router.get("/rooms/{db_id}/recording-state")
def recording_state(db_id: int) -> dict[str, Any]:
    """返回可轮询的录制生命周期状态。"""
    return service.recorder_manager.status(db_id)


@router.post("/rooms/{db_id}/markers")
def create_manual_marker(db_id: int, req: MarkerRequest) -> dict[str, Any]:
    """在当前直播时刻创建带前后缓冲的人工高光候选。"""
    try:
        return service.recorder_manager.mark_highlight(
            db_id,
            pre_roll_s=req.pre_roll_s,
            post_roll_s=req.post_roll_s,
            note=req.note,
        )
    except (ValueError, SourceError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
