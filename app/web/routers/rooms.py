"""直播间管理 (V0.1.14.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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
async def stop_recording(db_id: int) -> dict[str, str]:
    """停止某直播间录制。"""
    await service.recorder_manager.stop(db_id)
    return {"status": "stopped"}
