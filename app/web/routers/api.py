"""控制台 REST API 路由。

所有写操作的业务逻辑都委托给 :mod:`app.web.service`,本层只负责:

* 请求体校验(pydantic 模型);
* 把领域异常(``ValueError``)转换为 HTTP 400;
* 返回 JSON 或媒体文件。
"""

from __future__ import annotations

import time as _login_time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

from app.db.models import CandidateStatus
from app.web import service
from app.web.login_handler import get_cookie_info, get_login_status, start_login

router = APIRouter(prefix="/api")

# V0.1.9.1: 参数上界保护,防止客户端传超大 limit/days/resolution 导致 OOM。
_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365
_MAX_RESOLUTION = 4096


def _clamp(v: int, lo: int, hi: int) -> int:
    """夹紧整数到 [lo, hi] 区间。"""
    return max(lo, min(v, hi))


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


class BatchRequest(BaseModel):
    """批量操作请求(V0.1.8 P0)。"""

    candidate_ids: list[int]
    action: Literal["approve", "reject", "publish", "delete"]

    @field_validator("candidate_ids")
    @classmethod
    def _non_empty(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("candidate_ids 不能为空")
        if len(v) > 200:
            raise ValueError("单次批量操作不超过 200 项")
        return v


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


class SplitTopicRequest(BaseModel):
    """拆分主题请求。"""

    event_ids: list[int]

    @field_validator("event_ids")
    @classmethod
    def _non_empty_ids(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("event_ids 不能为空")
        return v


class ReorderTopicRequest(BaseModel):
    """重排主题事件请求。"""

    event_ids: list[int]


class ClusterSessionRequest(BaseModel):
    """聚类请求(空体, session_id 从路径获取,未来可能扩展聚类参数)。"""


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


# ----------------------------- 候选审核 ----------------------------- #
@router.get("/candidates")
def get_candidates(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """返回高光候选列表。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
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


@router.post("/candidates/batch")
async def batch_candidates(request: BatchRequest) -> dict[str, Any]:
    """批量审核/发布/删除候选(V0.1.8 P0)。

    :param request: 包含 candidate_ids 和 action。
    :returns: 各候选操作结果。
    """
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for cid in request.candidate_ids:
        try:
            if request.action == "approve":
                clip_id = await service.approve_candidate(cid)
                results.append({"candidate_id": cid, "status": "approved", "clip_id": clip_id})
            elif request.action == "reject":
                service.set_candidate_status(cid, CandidateStatus.REJECTED)
                results.append({"candidate_id": cid, "status": "rejected"})
            elif request.action == "publish":
                result = service.publish_clip(cid)
                results.append({"candidate_id": cid, "status": "ready", **result})
            elif request.action == "delete":
                service.delete_candidate(cid)
                results.append({"candidate_id": cid, "status": "deleted"})
            else:
                raise HTTPException(status_code=400, detail=f"未知操作: {request.action}")
        except (ValueError, HTTPException) as exc:
            failures.append({"candidate_id": cid, "error": str(exc)})
    return {"success": results, "failed": failures}


# ----------------------------- 成品切片 ----------------------------- #
@router.get("/clips")
def get_clips(limit: int = 50) -> list[dict[str, Any]]:
    """返回成品切片列表。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.list_clips(limit=limit)


@router.post("/clips/{clip_id}/publish")
def publish_clip(clip_id: int) -> dict[str, Any]:
    """人工发布:置 ready 并导出待上传清单;上传模块开启时入队上传。"""
    try:
        result = service.publish_clip(clip_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ready", **result}


@router.post("/clips/{clip_id}/confirm-manual-upload")
def confirm_manual_upload(
    clip_id: int,
    platform: str | None = None,
    submission_id: str | None = None,
    published_url: str | None = None,
) -> dict[str, Any]:
    """V0.1.12.7: 确认手动上传完成, 将 FinalClip 标记为 PUBLISHED。

    ManualUploader 导出清单后, FinalClip 不会自动标记为 PUBLISHED。
    用户需在前端确认已完成手动投稿, 然后调用此接口完成状态更新。

    :param clip_id: FinalClip.id。
    :param platform: 投稿平台 (如 bilibili)。
    :param submission_id: 稿件号 (如 BV 号)。
    :param published_url: 已发布链接。
    :returns: 操作结果。
    """
    import json as _json

    from loguru import logger as _log

    from app.db.models import ClipStatus, FinalClip, SegmentTask, SystemLog
    from app.db.models import TaskStatus as _Ts
    from app.db.session import get_session

    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is None:
            raise HTTPException(status_code=404, detail="切片不存在")
        clip.status = ClipStatus.PUBLISHED
        db.add(clip)

        # 如果有关联的 SegmentTask, 推进到 COMPLETED
        from sqlmodel import select as _sel

        task = db.exec(
            _sel(SegmentTask)
            .where(
                SegmentTask.clip_id == clip_id,
            )
            .order_by(SegmentTask.created_at.desc())
        ).first()
        if task and task.stage in (
            _Ts.AWAITING_PUBLISH_CONFIRMATION,
            _Ts.PUBLISHING,
            _Ts.QUEUED_FOR_PUBLISH,
            _Ts.RENDERED,
        ):
            task.stage = _Ts.COMPLETED
            from datetime import UTC
            from datetime import datetime as _dt_now

            task.completed_at = _dt_now(UTC)
            db.add(task)

        # 日志记录
        db.add(
            SystemLog(
                level="INFO",
                module="web",
                event="manual_upload_confirmed",
                message=f"clip={clip_id} 手动上传已确认",
                context_json=_json.dumps(
                    {
                        "clip_id": clip_id,
                        "platform": platform,
                        "submission_id": submission_id,
                        "published_url": published_url,
                    }
                )
                if (platform or submission_id or published_url)
                else None,
            )
        )

    _log.info("manual_upload_confirmed: clip={} platform={} submission_id={}", clip_id, platform, submission_id)
    return {"status": "published", "clip_id": clip_id}


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
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    days = _clamp(days, 1, _MAX_QUERY_DAYS)
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
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
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


# ── 登录失败限流 ────────────────────────────────────────────────────────────
_LOGIN_FAILURES: dict[str, list[float]] = {}
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_S = 300  # 5 分钟窗口


def _check_login_rate(ip: str) -> bool:
    """检查指定 IP 的登录失败次数是否在限流窗口内超限。

    :param ip: 客户端 IP 地址。
    :returns: ``True`` 表示允许继续尝试,``False`` 表示已触发限流。
    """
    now = _login_time.time()
    timestamps = _LOGIN_FAILURES.get(ip, [])
    _LOGIN_FAILURES[ip] = [t for t in timestamps if now - t <= _LOGIN_WINDOW_S]
    return len(_LOGIN_FAILURES[ip]) < _MAX_LOGIN_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    """记录一次登录失败的时间戳。

    :param ip: 客户端 IP 地址。
    """
    now = _login_time.time()
    if ip not in _LOGIN_FAILURES:
        _LOGIN_FAILURES[ip] = []
    _LOGIN_FAILURES[ip].append(now)


# ----------------------------- 账号登录 / Cookie 管理 ----------------------------- #
@router.get("/cookie-status")
def cookie_status() -> dict[str, Any]:
    """返回当前 Bilibili Cookie 存续状态。"""
    return get_cookie_info()


@router.post("/login")
def login_start(request: Request) -> dict[str, Any]:
    """启动一次浏览器登录流程（Playwright）,返回任务 ID 供前端轮询。"""
    ip = request.client.host if request.client else "unknown"
    if not _check_login_rate(ip):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁,请稍后再试")
    try:
        return start_login()
    except Exception as exc:
        _record_login_failure(ip)
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
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    from app.pipeline.task_worker import list_tasks as _list
    from app.pipeline.task_worker import task_worker

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
def split_topic(topic_id: int, req: SplitTopicRequest) -> dict[str, Any]:
    """拆分主题:将指定事件移出并创建新主题。"""
    from app.analysis.topic_cluster import split_topic as _st

    new_id = _st(topic_id, req.event_ids)
    if new_id is None:
        raise HTTPException(status_code=400, detail="拆分失败")
    return {"status": "split", "new_topic_id": new_id}


@router.post("/topics/{topic_id}/reorder")
def reorder_topic_events(topic_id: int, req: ReorderTopicRequest) -> dict[str, str]:
    """重排主题内事件顺序。"""
    from app.analysis.topic_cluster import reorder_topic_events as _ro

    _ro(topic_id, req.event_ids)
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
            _sel(ClipVariant).where(ClipVariant.event_id == event_id).order_by(ClipVariant.created_at.desc())
        ).all()
    return [
        {
            "id": v.id,
            "variant_type": v.variant_type,
            "has_subtitles": v.has_subtitles,
            "resolution": v.resolution,
            "file_path": v.file_path,
            "file_hash": v.file_hash,
            "render_status": v.render_status,
            "version_number": v.version_number,
            "duration_s": v.duration_s,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in variants
    ]


# ----------------------------- V0.1.8 P2 统计分析 ----------------------------- #
@router.get("/analytics")
def get_analytics() -> dict[str, Any]:
    """返回 Dashboard 统计分析数据。

    包含:
    - 录制趋势(近 30 天每日录制会话数/时长)
    - 切片统计(总数/已发布/总时长)
    - 候选分布(分数区间分布/状态分布)
    - 直播间排行(按切片数)
    """
    from datetime import UTC, datetime, timedelta

    from sqlmodel import func
    from sqlmodel import select as _sel

    from app.db.models import (
        ClipStatus,
        FinalClip,
        HighlightCandidate,
        LiveRoom,
        RawSegment,
        RecordingSession,
        TaskStatus,
    )
    from app.db.session import get_session

    now = datetime.now(UTC)
    days_30 = now - timedelta(days=30)

    with get_session() as db:
        # --- 切片统计 ---
        total_clips = db.exec(_sel(func.count()).select_from(FinalClip)).one()
        published_clips = db.exec(
            _sel(func.count()).select_from(FinalClip).where(FinalClip.status == ClipStatus.PUBLISHED)
        ).one()
        total_duration = (
            db.exec(_sel(func.coalesce(func.sum(FinalClip.duration_s), 0)).select_from(FinalClip)).one() or 0.0
        )

        # --- 候选统计 ---
        total_candidates = db.exec(_sel(func.count()).select_from(HighlightCandidate)).one()
        approved_candidates = db.exec(
            _sel(func.count()).select_from(HighlightCandidate).where(HighlightCandidate.status == "approved")
        ).one()
        avg_score = (
            db.exec(
                _sel(func.coalesce(func.avg(HighlightCandidate.highlight_score), 0.0)).select_from(HighlightCandidate)
            ).one()
            or 0.0
        )

        # 分数区间分布
        score_buckets = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
        all_scores = db.exec(_sel(HighlightCandidate.highlight_score).select_from(HighlightCandidate)).all()
        for s in all_scores:
            s = s or 0
            if s < 0.3:
                score_buckets["0.0-0.3"] += 1
            elif s < 0.5:
                score_buckets["0.3-0.5"] += 1
            elif s < 0.7:
                score_buckets["0.5-0.7"] += 1
            elif s < 0.85:
                score_buckets["0.7-0.85"] += 1
            else:
                score_buckets["0.85-1.0"] += 1

        # --- 录制统计 ---
        total_sessions = db.exec(_sel(func.count()).select_from(RecordingSession)).one()
        finished_sessions = db.exec(
            _sel(func.count()).select_from(RecordingSession).where(RecordingSession.ended_at is not None)
        ).one()
        total_reconnects = (
            db.exec(
                _sel(func.coalesce(func.sum(RecordingSession.reconnect_count), 0)).select_from(RecordingSession)
            ).one()
            or 0
        )

        # 原始数据量
        total_raw_gb = (
            db.exec(_sel(func.coalesce(func.sum(RawSegment.size_bytes), 0.0)).select_from(RawSegment)).one() or 0.0
        )
        total_raw_gb = round(total_raw_gb / (1024**3), 2)  # size_bytes → GB

        # --- 任务统计 ---
        from app.db.models import SegmentTask

        task_failed = db.exec(
            _sel(func.count()).select_from(SegmentTask).where(SegmentTask.stage == TaskStatus.FAILED)
        ).one()

        # --- 每日趋势(近 30 天) ---
        daily_record: list[dict[str, Any]] = []
        for i in range(30):
            day = days_30 + timedelta(days=i)
            day_end = day + timedelta(days=1)
            sessions_count = db.exec(
                _sel(func.count())
                .select_from(RecordingSession)
                .where(
                    RecordingSession.started_at >= day,
                    RecordingSession.started_at < day_end,
                )
            ).one()
            clips_count = db.exec(
                _sel(func.count())
                .select_from(FinalClip)
                .where(
                    FinalClip.created_at >= day,
                    FinalClip.created_at < day_end,
                )
            ).one()
            candidates_count = db.exec(
                _sel(func.count())
                .select_from(HighlightCandidate)
                .where(
                    HighlightCandidate.created_at >= day,
                    HighlightCandidate.created_at < day_end,
                )
            ).one()
            daily_record.append(
                {
                    "date": day.strftime("%m-%d"),
                    "sessions": sessions_count,
                    "clips": clips_count,
                    "candidates": candidates_count,
                }
            )

        # --- 直播间排行(按切片数 TOP 10) ---
        room_ranks: list[dict[str, Any]] = []
        rows = db.exec(
            _sel(
                LiveRoom.room_id,
                LiveRoom.uploader_name,
                func.count(FinalClip.id).label("cnt"),
                func.coalesce(func.sum(FinalClip.duration_s), 0.0).label("dur"),
            )
            .select_from(LiveRoom)
            .join(RecordingSession, RecordingSession.room_id == LiveRoom.id, isouter=True)
            .join(HighlightCandidate, HighlightCandidate.session_id == RecordingSession.id, isouter=True)
            .join(FinalClip, FinalClip.candidate_id == HighlightCandidate.id, isouter=True)
            .where(LiveRoom.uploader_name is not None)
            .group_by(LiveRoom.id)
            .order_by(func.count(FinalClip.id).desc())
            .limit(10)
        ).all()
        for r in rows:
            room_ranks.append(
                {
                    "name": r.uploader_name or f"房间{r.room_id}",
                    "clips": r.cnt,
                    "duration_h": round((r.dur or 0) / 3600, 1),
                }
            )

    return {
        "overview": {
            "total_clips": total_clips,
            "published_clips": published_clips,
            "total_duration_h": round(total_duration / 3600, 1),
            "total_candidates": total_candidates,
            "approved_candidates": approved_candidates,
            "avg_highlight_score": round(avg_score, 3),
            "total_sessions": total_sessions,
            "finished_sessions": finished_sessions,
            "total_reconnects": total_reconnects,
            "total_raw_gb": round(total_raw_gb, 1),
            "task_failed": task_failed,
        },
        "score_distribution": score_buckets,
        "daily_trend": daily_record,
        "room_ranking": room_ranks,
    }


@router.get("/metrics")
def get_metrics() -> dict[str, Any]:
    """返回实时运行指标 (V0.1.12.9)。

    轻量级监控端点, 包含:
    - 任务计数
    - Worker/录制状态
    - 平均性能耗时
    - 磁盘使用
    - 历史趋势 (最近 60 点)
    """
    from app.core.metrics import get_history, snapshot

    snap = snapshot()
    history = get_history(limit=60)

    return {
        "current": {
            "tasks": {
                "queued": snap.tasks_queued,
                "processing": snap.tasks_processing,
                "completed": snap.tasks_completed,
                "failed": snap.tasks_failed,
            },
            "workers": snap.active_workers,
            "recordings": snap.active_recordings,
            "recording_hours": snap.total_recording_hours,
            "avg_times": {
                "asr_ms": snap.asr_avg_ms,
                "render_ms": snap.render_avg_ms,
                "upload_ms": snap.upload_avg_ms,
            },
            "disk": {
                "free_gb": snap.disk_free_gb,
                "raw_gb": snap.disk_raw_gb,
                "clips_gb": snap.disk_clips_gb,
            },
            "db_lock_wait_avg_ms": snap.db_lock_wait_avg_ms,
        },
        "history": history,
    }
