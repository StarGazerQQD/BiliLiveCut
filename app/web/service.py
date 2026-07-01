"""Web 后台的业务服务层。

包含两部分:

* :class:`RecorderManager` —— 在 FastAPI 事件循环内以 asyncio 任务形式管理多个
  直播间的录制(启动/停止/状态),录制回调复用阶段2/3 的分析流水线;
* 一组查询与动作函数 —— 供 API 路由调用(列表、审核、发布、删除、调阈值等),
  与 CLI 共享同一套数据库与编排逻辑。

设计为薄服务层:不直接处理 HTTP,便于单测与复用。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

from loguru import logger
from sqlmodel import select

from app.core import settings_store
from app.core.config import settings
from app.core.osutil import open_path
from app.core.paths import clips_dir, ready_to_upload_dir
from app.db.models import (
    CandidateStatus,
    ClipStatus,
    Danmaku,
    FinalClip,
    HighlightCandidate,
    LiveRoom,
    RawSegment,
    RecordingSession,
    RoomMode,
    SessionStatus,
    SystemLog,
    Transcript,
    UploadTask,
)
from app.db.session import get_session
from app.recording.recorder import Recorder
from app.sources.bilibili.client import BilibiliLiveClient

# 前端可轮询的通知缓冲(最近若干条)。用于"上传关闭时直播结束弹目录"等提示。
_NOTIFICATIONS: deque[dict[str, Any]] = deque(maxlen=50)
_notify_seq = 0


def push_notification(message: str, kind: str = "info", data: dict[str, Any] | None = None) -> None:
    """向前端通知缓冲推送一条消息。

    :param message: 提示文本。
    :param kind: 类型(info/success/warning)。
    :param data: 附加数据(如目录路径)。
    """
    global _notify_seq
    _notify_seq += 1
    _NOTIFICATIONS.append(
        {
            "id": _notify_seq,
            "ts": time.time(),
            "kind": kind,
            "message": message,
            "data": data or {},
        }
    )


def get_notifications(since_id: int = 0) -> list[dict[str, Any]]:
    """获取比 ``since_id`` 更新的通知。

    :param since_id: 客户端已见的最大通知 id。
    :returns: 新通知列表。
    """
    return [n for n in _NOTIFICATIONS if n["id"] > since_id]


async def _on_session_end(session_id: int) -> None:
    """录制会话结束时的处理:上传模块关闭则弹出切片目录。

    :param session_id: 结束的会话 id。
    """
    clips_path = str(clips_dir())
    if settings_store.upload_active():
        push_notification(
            f"会话 #{session_id} 已结束。上传模块开启,成品将自动进入上传队列。",
            kind="success",
        )
        return
    # 上传模块关闭:弹出(在本机文件管理器打开)切片所在目录,并通知前端。
    open_path(clips_path)
    push_notification(
        f"本场直播(会话 #{session_id})已结束,上传模块未开启。切片已保存到:{clips_path}",
        kind="success",
        data={"clips_dir": clips_path, "ready_dir": str(ready_to_upload_dir())},
    )
    logger.info("会话 {} 结束,上传关闭,已弹出切片目录: {}", session_id, clips_path)


class RecorderManager:
    """管理多个直播间的并发录制任务(asyncio)。

    每个直播间对应一个 :class:`~app.recording.recorder.Recorder` 与一个 asyncio 任务。
    必须在事件循环内使用(由 FastAPI/uvicorn 提供)。
    """

    def __init__(self) -> None:
        self._recorders: dict[int, Recorder] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def is_running(self, db_id: int) -> bool:
        """指定直播间是否正在录制。

        :param db_id: ``live_rooms`` 主键。
        :returns: 正在录制返回 ``True``。
        """
        task = self._tasks.get(db_id)
        return task is not None and not task.done()

    def running_ids(self) -> list[int]:
        """返回当前正在录制的直播间 db_id 列表。"""
        return [rid for rid in self._tasks if self.is_running(rid)]

    async def start(self, db_id: int, pipeline: bool = True, produce: bool = False) -> None:
        """启动某直播间的录制(幂等:已在录制则忽略)。

        :param db_id: ``live_rooms`` 主键。
        :param pipeline: 是否启用实时转写+高光分析。
        :param produce: 是否在产生候选后自动切片+文案。
        :raises ValueError: 房间不存在、未授权或缺少 room_id 时。
        """
        if self.is_running(db_id):
            logger.info("房间 {} 已在录制,忽略重复启动。", db_id)
            return

        with get_session() as db:
            room = db.get(LiveRoom, db_id)
            if room is None:
                raise ValueError(f"房间不存在: db_id={db_id}")
            if settings.require_authorization and not room.authorized:
                raise ValueError("该直播间未确认授权,拒绝录制。")
            if room.room_id is None:
                raise ValueError("该直播间缺少 room_id。")
            room.enabled = True
            db.add(room)
            room_id = room.room_id

        on_segment = None
        if pipeline:
            from app.pipeline.orchestrator import make_pipeline_callback

            on_segment = make_pipeline_callback(produce=produce)

        recorder = Recorder(
            room_id=room_id,
            db_room_id=db_id,
            on_segment=on_segment,
            on_end=_on_session_end,
        )
        self._recorders[db_id] = recorder
        self._tasks[db_id] = asyncio.create_task(recorder.run())
        # 录制/分析开始 -> 立即暂停网感定时采集。
        from app.trends.scheduler import trend_scheduler

        trend_scheduler.pause_for_recording()
        logger.info("已启动录制任务 db_id={} pipeline={} produce={}", db_id, pipeline, produce)

    async def stop(self, db_id: int) -> None:
        """停止某直播间的录制并等待任务收尾。

        :param db_id: ``live_rooms`` 主键。
        """
        recorder = self._recorders.get(db_id)
        task = self._tasks.get(db_id)
        if recorder is not None:
            recorder.stop()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=30)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._recorders.pop(db_id, None)
        self._tasks.pop(db_id, None)

        with get_session() as db:
            room = db.get(LiveRoom, db_id)
            if room is not None:
                room.enabled = False
                db.add(room)
        # 若已无任何录制在跑,恢复网感定时采集。
        if not self.running_ids():
            from app.trends.scheduler import trend_scheduler

            trend_scheduler.resume_after_recording()
        logger.info("已停止录制任务 db_id={}", db_id)

    async def stop_all(self) -> None:
        """停止所有录制任务(应用关闭时调用)。"""
        for db_id in list(self._tasks):
            await self.stop(db_id)


# 模块级单例:整个 Web 进程共享一个录制管理器。
recorder_manager = RecorderManager()


# --------------------------------------------------------------------------- #
# 动作(写)
# --------------------------------------------------------------------------- #
async def add_room(url: str, authorized: bool) -> LiveRoom:
    """解析并登记直播间(与 CLI ``add-room`` 等价)。

    :param url: 直播间 URL 或房间号。
    :param authorized: 是否确认拥有录制授权。
    :returns: 登记/更新后的 :class:`LiveRoom`。
    :raises ValueError: 未授权时(在要求授权的配置下)。
    """
    if settings.require_authorization and not authorized:
        raise ValueError("需要确认授权才能添加直播间。")

    async with BilibiliLiveClient(cookie=settings.bilibili_cookie) as client:
        info = await client.get_room_info(url)

    with get_session() as db:
        existing = db.exec(select(LiveRoom).where(LiveRoom.room_id == info.room_id)).first()
        if existing:
            existing.input_url = url
            existing.authorized = authorized
            db.add(existing)
            return existing
        room = LiveRoom(
            input_url=url,
            room_id=info.room_id,
            authorized=authorized,
            highlight_threshold=settings.highlight_threshold,
            auto_publish_threshold=settings.auto_publish_threshold,
        )
        db.add(room)
        db.flush()
        db.refresh(room)
        return room


def update_room(db_id: int, fields: dict[str, Any]) -> LiveRoom:
    """更新直播间的可调参数(阈值、模式、授权等)。

    仅允许更新白名单字段,避免越权写入。

    :param db_id: ``live_rooms`` 主键。
    :param fields: 待更新字段。
    :returns: 更新后的 :class:`LiveRoom`。
    :raises ValueError: 房间不存在时。
    """
    allowed = {
        "mode",
        "highlight_threshold",
        "auto_publish_threshold",
        "authorized",
        "title",
        "uploader_name",
    }
    with get_session() as db:
        room = db.get(LiveRoom, db_id)
        if room is None:
            raise ValueError(f"房间不存在: db_id={db_id}")
        for key, value in fields.items():
            if key in allowed and value is not None:
                setattr(room, key, value)
        db.add(room)
        return room


def set_candidate_status(candidate_id: int, status: str) -> None:
    """设置候选状态(审核:批准/拒绝)。

    :param candidate_id: 候选 id。
    :param status: 新状态。
    :raises ValueError: 候选不存在时。
    """
    with get_session() as db:
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is None:
            raise ValueError(f"候选不存在: id={candidate_id}")
        cand.status = status
        db.add(cand)


async def approve_candidate(candidate_id: int) -> int | None:
    """批准候选并出片(切片+文案);在线程池执行 CPU 密集流程。

    :param candidate_id: 候选 id。
    :returns: 生成的 clip_id;失败返回 ``None``。
    """
    set_candidate_status(candidate_id, CandidateStatus.APPROVED)
    from app.pipeline.orchestrator import produce_clip

    clip = await asyncio.to_thread(produce_clip, candidate_id)
    return clip.id if clip else None


def delete_candidate(candidate_id: int) -> None:
    """删除候选。

    :param candidate_id: 候选 id。
    """
    with get_session() as db:
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is not None:
            db.delete(cand)


def publish_clip(clip_id: int) -> dict[str, Any]:
    """人工发布:把成品置为 ready 并导出清单;上传模块开启时入队上传。

    :param clip_id: 成品切片 id。
    :returns: 结果摘要(是否进入上传)。
    :raises ValueError: 切片不存在时。
    """
    from app.publishing.copywriter import export_manifest

    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is None:
            raise ValueError(f"切片不存在: id={clip_id}")
        clip.status = ClipStatus.READY
        db.add(clip)
    export_manifest(clip_id)

    if settings_store.upload_active():
        from app.publishing.uploader import enqueue_and_upload

        task = enqueue_and_upload(clip_id)
        return {"uploaded": True, "task_status": task.status}
    return {"uploaded": False, "note": "上传模块未开启,已导出待上传清单。"}


# --------------------------------------------------------------------------- #
# 设置开关 / 上传队列 / 目录
# --------------------------------------------------------------------------- #
def get_settings_view() -> dict[str, Any]:
    """返回可在后台切换的运行时开关及只读上传配置。

    :returns: 设置视图字典。
    """
    return {
        "biliup_enabled": settings_store.biliup_enabled(),
        "auto_upload": settings_store.auto_upload_enabled(),
        "upload_active": settings_store.upload_active(),
        "biliup_cmd_configured": bool(settings.biliup_upload_cmd.strip()),
        "default_uploader": settings.uploader,
        "clips_dir": str(clips_dir()),
        "ready_dir": str(ready_to_upload_dir()),
    }


def update_settings(fields: dict[str, Any]) -> dict[str, Any]:
    """更新运行时开关(biliup_enabled / auto_upload)。

    :param fields: 待更新开关。
    :returns: 更新后的设置视图。
    """
    if "biliup_enabled" in fields and fields["biliup_enabled"] is not None:
        settings_store.set_bool("biliup_enabled", bool(fields["biliup_enabled"]))
        logger.warning(
            "biliup 上传开关被设置为 {}(合规风险自负)。", bool(fields["biliup_enabled"])
        )
    if "auto_upload" in fields and fields["auto_upload"] is not None:
        settings_store.set_bool("auto_upload", bool(fields["auto_upload"]))
    _update_trend_schedule(fields)
    return get_settings_view()


def _valid_hhmm(value: str) -> bool:
    """校验 ``HH:MM`` 时间字符串是否合法。

    :param value: 时间字符串。
    :returns: 合法返回 ``True``。
    """
    try:
        h, m = value.strip().split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60
    except (ValueError, AttributeError):
        return False


def _update_trend_schedule(fields: dict[str, Any]) -> None:
    """更新网感定时采集的相关设置(开关/窗口/间隔)。

    :param fields: 待更新字段。
    :raises ValueError: 时间格式或间隔非法时。
    """
    if fields.get("trend_schedule_enabled") is not None:
        settings_store.set_bool("trend_schedule_enabled", bool(fields["trend_schedule_enabled"]))
    for key in ("trend_schedule_start", "trend_schedule_end"):
        if fields.get(key) is not None:
            if not _valid_hhmm(str(fields[key])):
                raise ValueError(f"时间格式应为 HH:MM: {fields[key]}")
            settings_store.set_setting(key, str(fields[key]).strip())
    if fields.get("trend_schedule_interval_min") is not None:
        interval = int(fields["trend_schedule_interval_min"])
        if interval < 1:
            raise ValueError("采集间隔需 >= 1 分钟。")
        settings_store.set_setting("trend_schedule_interval_min", str(interval))


def list_uploads(limit: int = 50) -> list[dict[str, Any]]:
    """列出上传任务(按更新时间降序)。

    :param limit: 数量上限。
    :returns: 上传任务字典列表。
    """
    with get_session() as db:
        rows = db.exec(
            select(UploadTask).order_by(UploadTask.updated_at.desc())  # type: ignore[attr-defined]
        ).all()[:limit]
    return [
        {
            "id": t.id,
            "clip_id": t.clip_id,
            "uploader": t.uploader,
            "status": t.status,
            "attempts": t.attempts,
            "remote_id": t.remote_id,
            "last_error": t.last_error,
            "precheck": json.loads(t.precheck_json) if t.precheck_json else None,
        }
        for t in rows
    ]


async def enqueue_clip_upload(clip_id: int) -> dict[str, Any]:
    """手动把某成品加入上传队列并执行(线程池运行)。

    :param clip_id: 成品切片 id。
    :returns: 任务状态摘要。
    """
    from app.publishing.uploader import enqueue_and_upload

    task = await asyncio.to_thread(enqueue_and_upload, clip_id)
    return {"task_id": task.id, "status": task.status, "error": task.last_error}


async def retry_upload(task_id: int) -> dict[str, Any]:
    """重试一个上传任务(线程池运行)。

    :param task_id: 上传任务 id。
    :returns: 任务状态摘要。
    """
    from app.publishing.uploader import process_upload_task

    task = await asyncio.to_thread(process_upload_task, task_id)
    return {"task_id": task.id, "status": task.status, "error": task.last_error}


def open_clips_directory() -> str:
    """在本机文件管理器打开切片目录(供"打开目录"按钮使用)。

    :returns: 切片目录路径。
    """
    path = str(clips_dir())
    open_path(path)
    return path


def reject_clip(clip_id: int) -> None:
    """拒绝成品切片。

    :param clip_id: 成品切片 id。
    """
    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        if clip is not None:
            clip.status = ClipStatus.REJECTED
            db.add(clip)


# --------------------------------------------------------------------------- #
# 查询(读)
# --------------------------------------------------------------------------- #
def dashboard_state() -> dict[str, Any]:
    """汇总仪表盘所需的概览数据。

    :returns: 含房间、运行状态、计数的字典。
    """
    with get_session() as db:
        rooms = db.exec(select(LiveRoom)).all()
        n_candidates = len(db.exec(select(HighlightCandidate)).all())
        n_clips = len(db.exec(select(FinalClip)).all())
        sessions = db.exec(
            select(RecordingSession).where(
                RecordingSession.status.in_(  # type: ignore[attr-defined]
                    [SessionStatus.RECORDING, SessionStatus.RECONNECTING, SessionStatus.STARTING]
                )
            )
        ).all()

    running = set(recorder_manager.running_ids())
    return {
        "rooms": [_room_dict(r, r.id in running) for r in rooms],
        "counts": {"candidates": n_candidates, "clips": n_clips, "active_sessions": len(sessions)},
        "modes": [RoomMode.MANUAL, RoomMode.SEMI, RoomMode.AUTO],
    }


def _room_dict(room: LiveRoom, running: bool) -> dict[str, Any]:
    """把房间转为可序列化字典并附带运行状态。"""
    return {
        "id": room.id,
        "room_id": room.room_id,
        "input_url": room.input_url,
        "title": room.title,
        "uploader_name": room.uploader_name,
        "mode": room.mode,
        "highlight_threshold": room.highlight_threshold,
        "auto_publish_threshold": room.auto_publish_threshold,
        "authorized": room.authorized,
        "enabled": room.enabled,
        "running": running,
    }


def list_candidates(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """列出高光候选(按分数降序)。

    :param limit: 数量上限。
    :param status: 可选状态过滤。
    :returns: 候选字典列表。
    """
    with get_session() as db:
        stmt = select(HighlightCandidate).order_by(
            HighlightCandidate.highlight_score.desc()  # type: ignore[attr-defined]
        )
        if status:
            stmt = stmt.where(HighlightCandidate.status == status)
        rows = db.exec(stmt).all()[:limit]
    return [
        {
            "id": c.id,
            "session_id": c.session_id,
            "highlight_score": round(c.highlight_score, 3),
            "rule_score": round(c.rule_score, 3),
            "llm_score": round(c.llm_score, 3),
            "status": c.status,
            "reason": c.reason,
            "peak_ts": c.peak_ts.isoformat() if c.peak_ts else None,
            "features": json.loads(c.features_json) if c.features_json else {},
        }
        for c in rows
    ]


def list_clips(limit: int = 50) -> list[dict[str, Any]]:
    """列出成品切片(按创建时间降序)。

    :param limit: 数量上限。
    :returns: 成品字典列表。
    """
    with get_session() as db:
        rows = db.exec(
            select(FinalClip).order_by(FinalClip.created_at.desc())  # type: ignore[attr-defined]
        ).all()[:limit]
    return [
        {
            "id": c.id,
            "candidate_id": c.candidate_id,
            "title": c.title,
            "description": c.description,
            "tags": json.loads(c.tags_json) if c.tags_json else [],
            "duration_s": c.duration_s,
            "status": c.status,
            "file_path": c.file_path,
            "cover_path": c.cover_path,
            "publish_suggestion": c.publish_suggestion,
        }
        for c in rows
    ]


def list_transcripts(limit: int = 30) -> list[dict[str, Any]]:
    """列出最近的转写文本(用于"实时转写"视图)。

    :param limit: 数量上限。
    :returns: 转写字典列表(按时间降序)。
    """
    with get_session() as db:
        rows = db.exec(
            select(Transcript).order_by(Transcript.created_at.desc())  # type: ignore[attr-defined]
        ).all()[:limit]
    return [
        {
            "id": t.id,
            "segment_id": t.segment_id,
            "language": t.language,
            "text": t.text,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in rows
    ]


def danmaku_overview(limit: int = 50, session_id: int | None = None) -> dict[str, Any]:
    """返回最近弹幕与按会话聚合的弹幕热度统计。

    :param limit: 返回的最近弹幕条数。
    :param session_id: 仅查询指定会话(可选)。
    :returns: ``{"available", "recent": [...], "sessions": [...]}``。
    """
    with get_session() as db:
        stmt = select(Danmaku).order_by(Danmaku.ts.desc())  # type: ignore[attr-defined]
        if session_id is not None:
            stmt = stmt.where(Danmaku.session_id == session_id)
        recent_rows = db.exec(stmt).all()[:limit]

        # 按会话聚合数量(简单热度指标)。
        agg_stmt = select(Danmaku.session_id, Danmaku.value)
        if session_id is not None:
            agg_stmt = agg_stmt.where(Danmaku.session_id == session_id)
        all_rows = db.exec(agg_stmt).all()

    counts: dict[int, dict[str, float]] = {}
    for sid, value in all_rows:
        bucket = counts.setdefault(sid, {"count": 0.0, "intensity": 0.0})
        bucket["count"] += 1
        bucket["intensity"] += float(value)

    sessions = [
        {"session_id": sid, "count": int(v["count"]), "intensity": round(v["intensity"], 2)}
        for sid, v in sorted(counts.items(), reverse=True)
    ]
    recent = [
        {
            "session_id": d.session_id,
            "ts": d.ts.isoformat() if d.ts else None,
            "type": d.msg_type,
            "user": d.user,
            "content": d.content,
        }
        for d in recent_rows
    ]
    return {"available": True, "total": len(all_rows), "recent": recent, "sessions": sessions}


def list_llm_providers() -> dict[str, Any]:
    """返回多大模型配置(对外视图,key 掩码)。

    :returns: ``{"providers": [...], "active_count": N}``。
    """
    from app.analysis import llm_providers as provs

    return {
        "providers": provs.public_view(),
        "active_count": len(provs.active_providers()),
    }


def save_llm_providers(items: list[dict[str, Any]]) -> dict[str, Any]:
    """保存多大模型配置(未提供新 key 的条目沿用旧 key)。

    :param items: 前端提交的 provider 字典列表。
    :returns: 保存后的对外视图。
    """
    from app.analysis import llm_providers as provs

    provs.merge_and_save(items)
    return list_llm_providers()


async def test_llm_providers() -> dict[str, Any]:
    """逐个测试已启用 provider 的连通性(各发一次极小请求)。

    :returns: ``{"results": [{"id","name","ok","detail"}, ...]}``。
    """
    from app.analysis import llm as llm_mod
    from app.analysis import llm_providers as provs

    def _probe(p: provs.LLMProvider) -> dict[str, Any]:
        try:
            text = llm_mod._complete(p, "ping", max_tokens=1)
            return {"id": p.id, "name": p.name, "ok": True, "detail": (text or "")[:40]}
        except Exception as exc:  # noqa: BLE001 — 汇总每个 provider 的错误
            return {"id": p.id, "name": p.name, "ok": False, "detail": str(exc)[:200]}

    providers = provs.active_providers()
    results = await asyncio.to_thread(lambda: [_probe(p) for p in providers])
    return {"results": results}


def trends_overview(limit: int = 30, days: int = 7) -> dict[str, Any]:
    """返回网感资料库概览:近期热门条目 + 热词排行 + 是否启用。

    :param limit: 条目数量上限。
    :param days: 近期窗口(天)。
    :returns: ``{"enabled", "days", "items": [...], "keywords": [...]}``。
    """
    from app.trends import store as trend_store
    from app.trends.scheduler import trend_scheduler

    items = trend_store.recent_trends(limit=limit, days=days)
    keywords = trend_store.keyword_heat(days=days, top=24)
    return {
        "enabled": settings.trend_enabled,
        "web_search": settings.trend_web_search,
        "days": days,
        "scheduler": trend_scheduler.status(),
        "items": [
            {
                "id": it.id,
                "source": it.source,
                "category": it.category,
                "title": it.title,
                "summary": it.summary,
                "url": it.url,
                "tags": json.loads(it.tags_json or "[]"),
                "heat": round(it.heat, 1),
                "seen_count": it.seen_count,
                "collected_at": it.collected_at.isoformat() if it.collected_at else None,
            }
            for it in items
        ],
        "keywords": keywords,
    }


async def collect_trends_now(topic: str = "") -> dict[str, Any]:
    """立即触发一次网感采集(在线程池中执行,避免阻塞事件循环)。

    :param topic: 采集主题提示。
    :returns: ``{"enabled", "saved"}``。
    """
    if not settings.trend_enabled:
        return {"enabled": False, "saved": 0, "note": "网感资料库未启用(TREND_ENABLED=false)。"}
    from app.trends.collector import collect_and_save

    saved = await asyncio.to_thread(collect_and_save, topic)
    push_notification(f"网感采集完成,新增/更新 {saved} 条。")
    return {"enabled": True, "saved": saved}


def list_logs(limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
    """列出系统日志(WARNING 及以上写入了 system_logs)。

    :param limit: 数量上限。
    :param level: 可选级别过滤。
    :returns: 日志字典列表(按时间降序)。
    """
    with get_session() as db:
        stmt = select(SystemLog).order_by(SystemLog.created_at.desc())  # type: ignore[attr-defined]
        if level:
            stmt = stmt.where(SystemLog.level == level)
        rows = db.exec(stmt).all()[:limit]
    return [
        {
            "id": x.id,
            "level": x.level,
            "module": x.module,
            "event": x.event,
            "message": x.message,
            "created_at": x.created_at.isoformat() if x.created_at else None,
        }
        for x in rows
    ]


def recording_status() -> list[dict[str, Any]]:
    """返回各活跃录制会话的状态(含片段计数)。

    :returns: 会话状态字典列表。
    """
    with get_session() as db:
        sessions = db.exec(
            select(RecordingSession).order_by(
                RecordingSession.started_at.desc()  # type: ignore[attr-defined]
            )
        ).all()[:20]
        result = []
        for s in sessions:
            n_seg = len(
                db.exec(select(RawSegment).where(RawSegment.session_id == s.id)).all()
            )
            result.append(
                {
                    "id": s.id,
                    "room_id": s.room_id,
                    "status": s.status,
                    "stream_format": s.stream_format,
                    "quality": s.quality,
                    "reconnect_count": s.reconnect_count,
                    "segments": n_seg,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "error_message": s.error_message,
                }
            )
    return result
