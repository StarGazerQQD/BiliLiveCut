"""控制台 REST API 路由。

所有写操作的业务逻辑都委托给 :mod:`app.web.service`,本层只负责:

* 请求体校验(pydantic 模型);
* 把领域异常(``ValueError``)转换为 HTTP 400;
* 返回 JSON 或媒体文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.db.models import CandidateStatus
from app.web import service

router = APIRouter(prefix="/api")


# ----------------------------- 请求模型 ----------------------------- #
class AddRoomRequest(BaseModel):
    """添加直播间请求。"""

    url: str
    authorized: bool = False


class UpdateRoomRequest(BaseModel):
    """更新直播间可调参数请求(字段均可选)。"""

    mode: str | None = None
    highlight_threshold: float | None = None
    auto_publish_threshold: float | None = None
    authorized: bool | None = None
    title: str | None = None
    uploader_name: str | None = None


class StartRequest(BaseModel):
    """启动录制请求。"""

    pipeline: bool = True
    produce: bool = False


class SettingsRequest(BaseModel):
    """运行时开关更新请求。"""

    biliup_enabled: bool | None = None
    auto_upload: bool | None = None
    trend_schedule_enabled: bool | None = None
    trend_schedule_start: str | None = None
    trend_schedule_end: str | None = None
    trend_schedule_interval_min: int | None = None


class TrendCollectRequest(BaseModel):
    """网感采集请求(主题可选)。"""

    topic: str = ""


class LLMProviderIn(BaseModel):
    """单个大模型配置(提交)。``api_key`` 留空表示不修改沿用旧值。"""

    id: str = ""
    name: str = ""
    base_url: str
    model: str
    api_key: str = ""
    web_search_param: str = ""
    price_input_per_m: float = 0.0
    price_output_per_m: float = 0.0
    enabled: bool = True
    priority: int = 100


class LLMProvidersRequest(BaseModel):
    """多大模型配置保存请求。"""

    providers: list[LLMProviderIn]


# ----------------------------- 概览 ----------------------------- #
@router.get("/dashboard")
def get_dashboard() -> dict[str, Any]:
    """返回仪表盘概览数据。"""
    return service.dashboard_state()


# ----------------------------- 直播间 ----------------------------- #
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


# ----------------------------- 录制 / 转写 ----------------------------- #
@router.get("/recording")
def get_recording() -> list[dict[str, Any]]:
    """返回录制会话状态列表。"""
    return service.recording_status()


@router.get("/transcripts")
def get_transcripts(limit: int = 30) -> list[dict[str, Any]]:
    """返回最近转写文本。"""
    return service.list_transcripts(limit=limit)


@router.get("/danmaku")
def get_danmaku(limit: int = 50, session_id: int | None = None) -> dict[str, Any]:
    """返回最近弹幕与各会话弹幕热度统计。

    :param limit: 返回的最近弹幕条数。
    :param session_id: 仅查询指定会话(可选)。
    """
    return service.danmaku_overview(limit=limit, session_id=session_id)


# ----------------------------- 候选审核 ----------------------------- #
@router.get("/candidates")
def get_candidates(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """返回高光候选列表。"""
    return service.list_candidates(limit=limit, status=status)


@router.post("/candidates/{candidate_id}/approve")
async def approve_candidate(candidate_id: int) -> dict[str, Any]:
    """批准候选并出片(切片+文案)。"""
    try:
        clip_id = await service.approve_candidate(candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "approved", "clip_id": clip_id}


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: int) -> dict[str, str]:
    """拒绝候选。"""
    try:
        service.set_candidate_status(candidate_id, CandidateStatus.REJECTED)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "rejected"}


@router.delete("/candidates/{candidate_id}")
def remove_candidate(candidate_id: int) -> dict[str, str]:
    """删除候选。"""
    service.delete_candidate(candidate_id)
    return {"status": "deleted"}


# ----------------------------- 成品切片 ----------------------------- #
@router.get("/clips")
def get_clips(limit: int = 50) -> list[dict[str, Any]]:
    """返回成品切片列表。"""
    return service.list_clips(limit=limit)


@router.post("/clips/{clip_id}/publish")
def publish_clip(clip_id: int) -> dict[str, Any]:
    """人工发布:置 ready 并导出待上传清单;上传模块开启时入队上传。"""
    try:
        result = service.publish_clip(clip_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ready", **result}


@router.post("/clips/{clip_id}/reject")
def reject_clip(clip_id: int) -> dict[str, str]:
    """拒绝成品切片。"""
    service.reject_clip(clip_id)
    return {"status": "rejected"}


# ----------------------------- 设置开关 / 上传 / 通知 ----------------------------- #
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


# ----------------------------- 多大模型配置 ----------------------------- #
@router.get("/llm-providers")
def get_llm_providers() -> dict[str, Any]:
    """返回多大模型配置(key 掩码)与可用数量。"""
    return service.list_llm_providers()


@router.put("/llm-providers")
def put_llm_providers(req: LLMProvidersRequest) -> dict[str, Any]:
    """保存多大模型配置(按优先级失败回退;未填 key 沿用旧值)。"""
    return service.save_llm_providers([p.model_dump() for p in req.providers])


@router.post("/llm-providers/test")
async def test_llm_providers() -> dict[str, Any]:
    """逐个测试已启用大模型的连通性。"""
    return await service.test_llm_providers()


# ----------------------------- 网感资料库 ----------------------------- #
@router.get("/trends")
def get_trends(limit: int = 30, days: int = 7) -> dict[str, Any]:
    """返回网感资料库概览(近期热门条目 + 热词排行)。"""
    return service.trends_overview(limit=limit, days=days)


@router.post("/trends/collect")
async def collect_trends(req: TrendCollectRequest | None = None) -> dict[str, Any]:
    """立即触发一次联网采集并写入资料库。"""
    topic = req.topic if req else ""
    return await service.collect_trends_now(topic=topic or "")


# ----------------------------- 日志 ----------------------------- #
@router.get("/logs")
def get_logs(limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
    """返回系统日志(WARNING 及以上)。"""
    return service.list_logs(limit=limit, level=level)


# ----------------------------- 媒体预览 ----------------------------- #
@router.get("/clips/{clip_id}/video")
def clip_video(clip_id: int) -> FileResponse:
    """返回成品 MP4 以便页面内预览。"""
    clips = {c["id"]: c for c in service.list_clips(limit=1000)}
    clip = clips.get(clip_id)
    if not clip or not clip["file_path"] or not Path(clip["file_path"]).exists():
        raise HTTPException(status_code=404, detail="视频不存在")
    return FileResponse(clip["file_path"], media_type="video/mp4")


@router.get("/clips/{clip_id}/cover")
def clip_cover(clip_id: int) -> FileResponse:
    """返回成品封面图。"""
    clips = {c["id"]: c for c in service.list_clips(limit=1000)}
    clip = clips.get(clip_id)
    if not clip or not clip["cover_path"] or not Path(clip["cover_path"]).exists():
        raise HTTPException(status_code=404, detail="封面不存在")
    return FileResponse(clip["cover_path"], media_type="image/jpeg")
