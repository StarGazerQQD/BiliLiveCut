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
from app.web.login_handler import get_cookie_info, get_login_status, start_login

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
    schedule_enabled: bool | None = None
    auto_threshold_enabled: bool | None = None
    danmaku_sentiment_enabled: bool | None = None
    # V0.1.6 P2: 房间配置。
    room_config: dict | None = None


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


class ScheduleRequest(BaseModel):
    """录制预约请求。"""

    room_id: int
    scheduled_at: str  # ISO 格式时间,如 "2026-07-03T20:00:00"
    recurrent: str = ""  # 空=一次性, daily=每日


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


class MergeTopicsRequest(BaseModel):
    """合井主题请求。"""
    source_id: int
    target_id: int


class TopicUpdateRequest(BaseModel):
    """更新主题请求(仅允许白名单字段)。"""
    title: str | None = None
    summary: str | None = None
    keywords_json: str | None = None
    status: str | None = None
    is_collection: bool | None = None


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


# ----------------------------- V0.1.2 新增:录制预约 ----------------------------- #
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


# ----------------------------- V0.1.2 新增:进度追踪 ----------------------------- #
@router.get("/progress")
def get_progress(session_id: int | None = None) -> dict[str, Any]:
    """返回录制→转写→评分的进度统计。"""
    return service.pipeline_progress(session_id=session_id)


# ----------------------------- V0.1.2 新增:阈值自学习 ----------------------------- #
@router.get("/rooms/{db_id}/threshold-learning")
def threshold_learning(db_id: int) -> dict[str, Any]:
    """返回某房间的阈值自学习摘要。"""
    return service.threshold_learning_status(db_id)


# ----------------------------- 媒体预览 ----------------------------- #
@router.get("/clips/{clip_id}/video")
def clip_video(clip_id: int) -> FileResponse:
    """返回成品 MP4 以便页面内预览。"""
    from app.core.paths import clips_dir as _clips_dir

    clips = {c["id"]: c for c in service.list_clips(limit=1000)}
    clip = clips.get(clip_id)
    if not clip or not clip["file_path"] or not Path(clip["file_path"]).exists():
        raise HTTPException(status_code=404, detail="视频不存在")
    # 路径遍历保护:确保文件在 clips 目录内。
    file_path = Path(clip["file_path"]).resolve()
    clips_root = _clips_dir().resolve()
    if not str(file_path).startswith(str(clips_root)):
        raise HTTPException(status_code=403, detail="禁止访问")
    return FileResponse(str(file_path), media_type="video/mp4")


@router.get("/clips/{clip_id}/cover")
def clip_cover(clip_id: int) -> FileResponse:
    """返回成品封面图。"""
    from app.core.paths import clips_dir as _clips_dir

    clips = {c["id"]: c for c in service.list_clips(limit=1000)}
    clip = clips.get(clip_id)
    if not clip or not clip["cover_path"] or not Path(clip["cover_path"]).exists():
        raise HTTPException(status_code=404, detail="封面不存在")
    # 路径遍历保护:确保文件在 clips 目录内。
    file_path = Path(clip["cover_path"]).resolve()
    clips_root = _clips_dir().resolve()
    if not str(file_path).startswith(str(clips_root)):
        raise HTTPException(status_code=403, detail="禁止访问")
    return FileResponse(str(file_path), media_type="image/jpeg")


# ----------------------------- 账号登录 / Cookie 管理 ----------------------------- #
@router.get("/cookie-status")
def cookie_status() -> dict[str, Any]:
    """返回当前 Bilibili Cookie 存续状态。"""
    return get_cookie_info()


@router.post("/login")
def login_start() -> dict[str, Any]:
    """启动一次浏览器登录流程（Playwright）,返回任务 ID 供前端轮询。"""
    try:
        return start_login()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/login/status")
def login_status(task_id: int) -> dict[str, Any]:
    """查询登录任务当前状态。

    - ``starting``: 正在启动浏览器
    - ``waiting``: 等待用户在浏览器中完成登录
    - ``done``: 登录成功,Cookie 已保存
    - 含 ``error`` 时表示登录失败
    """
    return get_login_status(task_id)


@router.post("/login/clear")
def login_clear() -> dict[str, str]:
    """清除已保存的 Bilibili Cookie。"""
    from app.core import settings_store

    settings_store.set_setting("bilibili_cookie", "")
    return {"status": "cleared"}


# ----------------------------- V0.1.6 任务队列 ----------------------------- #
@router.get("/tasks")
def get_tasks(limit: int = 50, stage: str | None = None) -> dict[str, Any]:
    """返回任务队列列表及各阶段统计。"""
    from app.pipeline.task_worker import list_tasks as _list, task_worker

    tasks = _list(limit=limit, stage=stage)
    return {"tasks": tasks, "stats": task_worker.stats()}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int) -> dict[str, Any]:
    """手动重试一个失败/取消的任务。"""
    from app.pipeline.task_worker import retry_task as _retry

    ok = _retry(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或状态不允许重试")
    return {"status": "retried"}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int) -> dict[str, Any]:
    """取消一个未完成的任务。"""
    from app.pipeline.task_worker import cancel_task as _cancel

    ok = _cancel(task_id)
    if not ok:
        raise HTTPException(status_code=400, detail="任务不存在或已完成/失败/取消")
    return {"status": "cancelled"}


# ----------------------------- V0.1.6 P1 主题管理 ----------------------------- #
@router.get("/topics")
def list_topics(session_id: int | None = None) -> dict[str, Any]:
    """获取主题列表。"""
    from app.analysis.topic_cluster import list_topics as _lt

    return {"topics": _lt(session_id=session_id)}


@router.get("/topics/{topic_id}")
def get_topic(topic_id: int) -> dict[str, Any]:
    """获取单个主题详情。"""
    from app.analysis.topic_cluster import get_topic as _gt

    t = _gt(topic_id)
    if t is None:
        raise HTTPException(status_code=404, detail="主题不存在")
    return t


@router.patch("/topics/{topic_id}")
def update_topic(topic_id: int, body: TopicUpdateRequest) -> dict[str, str]:
    """更新主题属性(title/summary/keywords/status/is_collection)。"""
    from app.analysis.topic_cluster import update_topic as _ut

    ok = _ut(topic_id, **body.model_dump(exclude_none=True))
    if not ok:
        raise HTTPException(status_code=404, detail="主题不存在")
    return {"status": "updated"}


@router.post("/topics/{topic_id}/events/{event_id}")
def add_event_to_topic(topic_id: int, event_id: int) -> dict[str, str]:
    """将事件加入主题。"""
    from app.analysis.topic_cluster import add_event_to_topic as _ae

    _ae(event_id, topic_id)
    return {"status": "added"}


@router.delete("/topics/{topic_id}/events/{event_id}")
def remove_event_from_topic(topic_id: int, event_id: int) -> dict[str, str]:
    """从主题移除事件。"""
    from app.analysis.topic_cluster import remove_event_from_topic as _re

    ok = _re(event_id, topic_id)
    if not ok:
        raise HTTPException(status_code=404, detail="关联不存在")
    return {"status": "removed"}


@router.post("/topics/merge")
def merge_topics(req: MergeTopicsRequest) -> dict[str, str]:
    """合并两个主题。"""
    from app.analysis.topic_cluster import merge_topics as _mt

    ok = _mt(req.source_id, req.target_id)
    if not ok:
        raise HTTPException(status_code=400, detail="合并失败")
    return {"status": "merged"}


@router.post("/topics/{topic_id}/split")
def split_topic(topic_id: int, event_ids: list[int]) -> dict[str, Any]:
    """拆分主题:将指定事件移出并创建新主题。"""
    from app.analysis.topic_cluster import split_topic as _st

    new_id = _st(topic_id, event_ids)
    if new_id is None:
        raise HTTPException(status_code=400, detail="拆分失败")
    return {"status": "split", "new_topic_id": new_id}


@router.post("/topics/{topic_id}/reorder")
def reorder_topic_events(topic_id: int, event_ids: list[int]) -> dict[str, str]:
    """重排主题内事件顺序。"""
    from app.analysis.topic_cluster import reorder_topic_events as _ro

    _ro(topic_id, event_ids)
    return {"status": "reordered"}


@router.post("/sessions/{session_id}/cluster")
def cluster_session_candidates(session_id: int) -> dict[str, Any]:
    """对一场直播的候选进行主题聚类。"""
    from app.analysis.topic_cluster import cluster_candidates

    topics = cluster_candidates(session_id)
    return {"status": "clustered", "topics": topics}


# ----------------------------- V0.1.6 P1 ClipVariant ----------------------------- #
@router.get("/events/{event_id}/variants")
def list_variants(event_id: int) -> list[dict[str, Any]]:
    """列出某事件的所有成品版本。"""
    from app.db.models import ClipVariant
    from app.db.session import get_session

    with get_session() as db:
        from sqlmodel import select as _sel

        variants = db.exec(
            _sel(ClipVariant).where(ClipVariant.event_id == event_id).order_by(
                ClipVariant.created_at.desc()
            )
        ).all()
    return [
        {
            "id": v.id, "variant_type": v.variant_type,
            "has_subtitles": v.has_subtitles, "resolution": v.resolution,
            "file_path": v.file_path, "file_hash": v.file_hash,
            "render_status": v.render_status, "version_number": v.version_number,
            "duration_s": v.duration_s, "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in variants
    ]
