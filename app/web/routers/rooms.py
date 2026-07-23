"""直播间管理."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.web import service


class AddRoomRequest(BaseModel):
    """添加直播间请求体。"""

    url: str
    authorized: bool = False


class UpdateRoomRequest(BaseModel):
    """直播间配置更新请求体。"""

    mode: str | None = None
    highlight_threshold: float | None = None
    auto_publish_threshold: float | None = None
    authorized: bool | None = None
    title: str | None = None
    uploader_name: str | None = None
    schedule_enabled: bool | None = None
    auto_threshold_enabled: bool | None = None
    danmaku_sentiment_enabled: bool | None = None
    room_config: dict | None = None


class StartRequest(BaseModel):
    """录制/流水线启动请求体。"""

    pipeline: bool = True
    produce: bool = False


class StopRequest(BaseModel):
    """停止录制请求体。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["graceful", "force"] = "graceful"
    cancel_pending: bool = False


class MarkerRequest(BaseModel):
    """直播中人工高光打点请求体。"""

    model_config = ConfigDict(extra="forbid")

    pre_roll_s: float = Field(default=20.0, ge=0.0, le=300.0, allow_inf_nan=False)
    post_roll_s: float = Field(default=40.0, ge=2.0, le=300.0, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=200)


router = APIRouter()


@router.post("/rooms")
async def create_room(req: AddRoomRequest) -> dict[str, Any]:
    """添加直播间。"""
    try:
        room = await service.add_room(req.url, req.authorized)
    except (ValueError, Exception) as exc:  # noqa: BLE001 — 取流/解析失败统一报 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": room.id, "room_id": room.room_id}


@router.patch("/rooms/{db_id}")
def patch_room(db_id: int, req: UpdateRoomRequest) -> dict[str, Any]:
    """更新直播间阈值/模式等参数。"""
    try:
        room = service.update_room(db_id, req.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": room.id, "mode": room.mode, "highlight_threshold": room.highlight_threshold}


@router.post("/rooms/{db_id}/start")
async def start_recording(db_id: int, req: StartRequest) -> dict[str, str]:
    """启动某直播间录制。"""
    try:
        await service.recorder_manager.start(db_id, pipeline=req.pipeline, produce=req.produce)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "started"}


@router.post("/rooms/{db_id}/stop")
async def stop_recording(db_id: int, req: StopRequest | None = None) -> dict[str, Any]:
    """停止某直播间录制。"""
    payload = req or StopRequest()
    try:
        result = await service.recorder_manager.stop(
            db_id,
            mode=payload.mode,
            pause_auto_restart=True,
            cancel_pending=payload.cancel_pending,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": result["state"], **result}


@router.post("/rooms/{db_id}/pause")
async def pause_recording(db_id: int) -> dict[str, Any]:
    """优雅暂停录制;恢复时会创建新会话并明确形成时间缺口。"""
    result = await service.recorder_manager.stop(db_id, mode="graceful", pause_auto_restart=True)
    return {"status": result["state"], **result}


@router.post("/rooms/{db_id}/resume")
async def resume_recording(db_id: int, req: StartRequest) -> dict[str, Any]:
    """恢复人工暂停的房间,并创建新的录制会话。"""
    try:
        await service.recorder_manager.start(db_id, pipeline=req.pipeline, produce=req.produce)
    except ValueError as exc:
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
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
