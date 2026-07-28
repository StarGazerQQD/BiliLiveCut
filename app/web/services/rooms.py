"""Rooms."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlmodel import select

from app.core import settings_store
from app.core.config import settings
from app.core.cookie import get_bilibili_cookie
from app.core.osutil import open_path
from app.core.paths import clips_dir, ready_to_upload_dir
from app.db.models import (
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    SegmentTask,
    SessionStatus,
    TaskStatus,
)
from app.db.session import get_session
from app.recording.recorder import Recorder
from app.sources.bilibili.client import BilibiliLiveClient
from app.web.services.notifications import push_notification


async def _on_session_end(session_id: int) -> None:
    """录制会话结束时的处理:上传模块关闭则弹出切片目录。

    :param session_id: 结束的会话 id。
    """
    _finalize_manual_markers(session_id)
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


@dataclass(slots=True)
class RecordingRuntime:
    """Web 可查询的单房间录制运行状态。"""

    state: str = "idle"
    session_id: int | None = None
    updated_at: float = 0.0
    message: str | None = None

    def as_dict(self, *, running: bool) -> dict[str, Any]:
        """返回可序列化状态。"""
        return {
            "state": self.state,
            "session_id": self.session_id,
            "updated_at": self.updated_at,
            "message": self.message,
            "running": running,
        }


class RecorderManager:
    """管理多个直播间的并发录制任务(asyncio)。

    每个直播间对应一个 :class:`~app.recording.recorder.Recorder` 与一个 asyncio 任务。
    必须在事件循环内使用(由 FastAPI/uvicorn 提供)。
    """

    def __init__(self) -> None:
        self._recorders: dict[int, Recorder] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._runtime: dict[int, RecordingRuntime] = {}

    def _set_state(
        self,
        db_id: int,
        state: str,
        session_id: int | None = None,
        message: str | None = None,
    ) -> None:
        """更新内存中的运行状态快照。"""
        current = self._runtime.setdefault(db_id, RecordingRuntime())
        current.state = state
        if session_id is not None:
            current.session_id = session_id
        current.updated_at = time.time()
        current.message = message

    def status(self, db_id: int) -> dict[str, Any]:
        """返回指定房间的录制运行状态。"""
        runtime = self._runtime.get(db_id, RecordingRuntime(updated_at=time.time()))
        return runtime.as_dict(running=self.is_running(db_id))

    def is_paused(self, db_id: int) -> bool:
        """返回房间是否被人工暂停自动录制。"""
        from app.analysis.room_config import load_room_config

        with get_session() as db:
            room = db.get(LiveRoom, db_id)
            return bool(load_room_config(room).get("recording_paused", False))

    def _set_paused(self, db_id: int, paused: bool) -> None:
        """持久化人工暂停标记。"""
        from app.analysis.room_config import merge_room_config

        with get_session() as db:
            room = db.get(LiveRoom, db_id)
            if room is None:
                return
            room.room_config_json = json.dumps(
                merge_room_config(room, {"recording_paused": paused}),
                ensure_ascii=False,
            )
            db.add(room)

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

        self._set_paused(db_id, False)
        self._set_state(db_id, SessionStatus.STARTING)

        on_segment = None
        if pipeline:
            from app.pipeline.orchestrator import make_pipeline_callback

            on_segment = make_pipeline_callback(produce=produce, room_id=db_id)

        recorder = Recorder(
            room_id=room_id,
            db_room_id=db_id,
            on_segment=on_segment,
            on_end=_on_session_end,
            on_state=lambda state, session_id: self._set_state(db_id, state, session_id),
        )
        self._recorders[db_id] = recorder
        self._tasks[db_id] = asyncio.create_task(self._run_recorder(db_id, recorder))
        # 录制/分析开始 -> 立即暂停网感定时采集。
        from app.trends.scheduler import trend_scheduler

        trend_scheduler.pause_for_recording()
        logger.info("已启动录制任务 db_id={} pipeline={} produce={}", db_id, pipeline, produce)

    async def _run_recorder(self, db_id: int, recorder: Recorder) -> None:
        """运行 Recorder 并保证未捕获异常被记录为可见状态。"""
        try:
            await recorder.run()
        except asyncio.CancelledError:
            recorder.fail("录制任务被强制取消")
            self._set_state(db_id, "force_stopped", recorder.session_id, "录制任务被强制取消")
            raise
        except Exception as exc:  # noqa: BLE001
            recorder.fail(str(exc))
            self._set_state(db_id, SessionStatus.ERROR, recorder.session_id, str(exc))
            logger.exception("录制任务异常 db_id={}: {}", db_id, exc)

    async def stop(
        self,
        db_id: int,
        *,
        mode: str = "graceful",
        pause_auto_restart: bool = False,
        cancel_pending: bool = False,
    ) -> dict[str, Any]:
        """停止某直播间的录制并等待任务收尾。

        :param db_id: ``live_rooms`` 主键。
        :param mode: ``graceful`` 优雅收尾或 ``force`` 立即结束 FFmpeg。
        :param pause_auto_restart: 是否阻止自动录制监控再次拉起。
        :param cancel_pending: 是否取消该会话尚未完成的下游任务。
        :returns: 最终状态和取消任务数。
        """
        if mode not in {"graceful", "force"}:
            raise ValueError("停止模式必须是 graceful 或 force")
        self._set_paused(db_id, pause_auto_restart)
        recorder = self._recorders.get(db_id)
        task = self._tasks.get(db_id)
        session_id = recorder.session_id if recorder is not None else self.status(db_id).get("session_id")
        self._set_state(db_id, SessionStatus.STOPPING, session_id)
        if recorder is not None:
            if mode == "force":
                recorder.force_stop()
            else:
                recorder.stop()
        forced = mode == "force"
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5 if forced else 30)
            except TimeoutError:
                if recorder is not None and not forced:
                    forced = True
                    recorder.force_stop()
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=5)
                    except TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                else:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            except asyncio.CancelledError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._recorders.pop(db_id, None)
        self._tasks.pop(db_id, None)

        with get_session() as db:
            room = db.get(LiveRoom, db_id)
            if room is not None:
                room.enabled = False
                db.add(room)
        cancelled_tasks = _cancel_pending_tasks(int(session_id)) if cancel_pending and session_id is not None else 0
        if forced:
            final_state = "force_stopped"
        else:
            final_state = SessionStatus.PAUSED if pause_auto_restart else SessionStatus.STOPPED
        if pause_auto_restart and session_id is not None:
            _set_session_status(int(session_id), SessionStatus.PAUSED)
        self._set_state(db_id, final_state, session_id)
        # 若已无任何录制在跑,恢复网感定时采集。
        if not self.running_ids():
            from app.trends.scheduler import trend_scheduler

            trend_scheduler.resume_after_recording()
        logger.info("已停止录制任务 db_id={}", db_id)
        return {
            "state": final_state,
            "session_id": session_id,
            "forced": forced,
            "cancelled_tasks": cancelled_tasks,
        }

    async def stop_all(self) -> None:
        """停止所有录制任务(应用关闭时调用)。"""
        for db_id in list(self._tasks):
            await self.stop(db_id)

    def mark_highlight(
        self,
        db_id: int,
        *,
        pre_roll_s: float,
        post_roll_s: float,
        note: str | None = None,
    ) -> dict[str, Any]:
        """在正在录制的会话中创建人工高光候选。"""
        recorder = self._recorders.get(db_id)
        if recorder is None or not self.is_running(db_id):
            raise ValueError("该房间当前未在录制")
        session_id = recorder.session_id
        if session_id is None:
            raise ValueError("录制会话仍在初始化,请稍后再打点")
        candidate, event = _create_manual_marker(
            session_id,
            pre_roll_s=pre_roll_s,
            post_roll_s=post_roll_s,
            note=note,
        )
        push_notification(
            f"已记录人工高光点 #{candidate.id},将保留前 {pre_roll_s:g} 秒和后 {post_roll_s:g} 秒。",
            kind="success",
            data={"candidate_id": candidate.id, "event_id": event.id, "session_id": session_id},
        )
        return {
            "candidate_id": candidate.id,
            "event_id": event.id,
            "session_id": session_id,
            "start_ts": candidate.start_ts.isoformat(),
            "peak_ts": candidate.peak_ts.isoformat(),
            "end_ts": candidate.end_ts.isoformat(),
        }


def _set_session_status(session_id: int, status: str) -> None:
    """更新已结束会话的最终用户可见状态。"""
    with get_session() as db:
        session = db.get(RecordingSession, session_id)
        if session is not None:
            session.status = status
            if session.ended_at is None:
                session.ended_at = datetime.now(UTC)
            db.add(session)


def _cancel_pending_tasks(session_id: int) -> int:
    """取消指定会话所有非终态下游任务。"""
    terminal = {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}
    with get_session() as db:
        task_ids = [
            task.id
            for task in db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).all()
            if task.id is not None and task.stage not in terminal
        ]
    from app.pipeline.task_worker import cancel_task

    return sum(1 for task_id in task_ids if cancel_task(task_id))


def _create_manual_marker(
    session_id: int,
    *,
    pre_roll_s: float,
    post_roll_s: float,
    note: str | None,
) -> tuple[HighlightCandidate, HighlightEvent]:
    """持久化人工打点及其预期剪辑窗口。"""
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_session() as db:
        session = db.get(RecordingSession, session_id)
        if session is None:
            raise ValueError("录制会话不存在")
        session_start = session.started_at
        if session_start.tzinfo is not None:
            session_start = session_start.astimezone(UTC).replace(tzinfo=None)
        start_ts = max(session_start, now - timedelta(seconds=pre_roll_s))
        end_ts = now + timedelta(seconds=post_roll_s)
        marker_meta = {
            "source": "manual_marker",
            "pre_roll_s": pre_roll_s,
            "post_roll_s": post_roll_s,
            "note": note,
            "state": "waiting_for_media",
        }
        candidate = HighlightCandidate(
            session_id=session_id,
            peak_ts=now,
            start_ts=start_ts,
            end_ts=end_ts,
            highlight_score=1.0,
            reason=f"人工打点{f': {note}' if note else ''}",
            features_json=json.dumps(marker_meta, ensure_ascii=False),
            dedup_hash=f"manual:{session_id}:{uuid4().hex}",
        )
        db.add(candidate)
        db.flush()
        event = HighlightEvent(
            candidate_id=candidate.id,
            session_id=session_id,
            raw_start_ts=start_ts,
            raw_end_ts=end_ts,
            adjusted_start_ts=start_ts,
            adjusted_end_ts=end_ts,
            highlight_score=1.0,
            features_json=candidate.features_json,
            reason=candidate.reason,
            review_status=ReviewStatus.PENDING,
            review_by="manual_marker",
        )
        db.add(event)
        db.flush()
        db.refresh(candidate)
        db.refresh(event)
        return candidate, event


def _finalize_manual_markers(session_id: int) -> None:
    """会话结束时把人工打点窗口收敛到真实录像边界。"""
    with get_session() as db:
        segments = db.exec(select(RawSegment).where(RawSegment.session_id == session_id)).all()
        starts = [segment.start_ts for segment in segments if segment.start_ts is not None]
        ends = [segment.end_ts for segment in segments if segment.end_ts is not None]
        candidates = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)).all()
        for candidate in candidates:
            try:
                metadata = json.loads(candidate.features_json or "{}")
            except json.JSONDecodeError:
                continue
            if metadata.get("source") != "manual_marker":
                continue
            event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate.id)).first()
            if not starts or not ends:
                metadata["state"] = "missing_media"
                candidate.features_json = json.dumps(metadata, ensure_ascii=False)
                if event is not None:
                    event.review_status = ReviewStatus.INSUFFICIENT_CONTEXT
                    event.review_reason = "人工打点没有对应的可用录像"
                    event.features_json = candidate.features_json
                    db.add(event)
                db.add(candidate)
                continue
            media_start = min(starts)
            media_end = max(ends)
            candidate.start_ts = max(candidate.start_ts, media_start)
            candidate.end_ts = min(candidate.end_ts, media_end)
            metadata["state"] = "ready" if (candidate.end_ts - candidate.start_ts).total_seconds() >= 2 else "too_short"
            candidate.features_json = json.dumps(metadata, ensure_ascii=False)
            db.add(candidate)
            if event is not None:
                event.raw_start_ts = candidate.start_ts
                event.raw_end_ts = candidate.end_ts
                event.adjusted_start_ts = candidate.start_ts
                event.adjusted_end_ts = candidate.end_ts
                event.features_json = candidate.features_json
                if metadata["state"] == "too_short":
                    event.review_status = ReviewStatus.INSUFFICIENT_CONTEXT
                    event.review_reason = "人工打点对应录像不足 2 秒"
                db.add(event)


# 模块级单例:整个 Web 进程共享一个录制管理器。

recorder_manager = RecorderManager()


async def add_room(url: str, authorized: bool) -> LiveRoom:
    """解析并登记直播间(与 CLI ``add-room`` 等价)。

    :param url: 直播间 URL 或房间号。
    :param authorized: 是否确认拥有录制授权。
    :returns: 登记/更新后的 :class:`LiveRoom`。
    :raises ValueError: 未授权时(在要求授权的配置下)。
    """
    if settings.require_authorization and not authorized:
        raise ValueError("需要确认授权才能添加直播间。")

    async with BilibiliLiveClient(cookie=get_bilibili_cookie()) as client:
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
        "schedule_enabled",
        "auto_threshold_enabled",
        "danmaku_sentiment_enabled",
        # V0.1.6: 独立自动化开关。
        "auto_record",
        "auto_analyze",
        "auto_render",
        "auto_approve",
        "auto_upload",
        "auto_approve_threshold",
        "review_threshold",
        # V0.1.6 P2: 房间配置。
        "room_config",
    }
    with get_session() as db:
        room = db.get(LiveRoom, db_id)
        if room is None:
            raise ValueError(f"房间不存在: db_id={db_id}")
        # 录制中不允许修改功能开关(锁定保护)。
        if recorder_manager.is_running(db_id):
            for key in ("schedule_enabled", "auto_threshold_enabled", "danmaku_sentiment_enabled"):
                if key in fields:
                    raise ValueError(f"直播间正在录制,无法修改「{key}」开关。请先停止录制。")
        for key, value in fields.items():
            if key in allowed and value is not None:
                if key == "room_config" and isinstance(value, dict):
                    from app.analysis.room_config import merge_room_config

                    value = json.dumps(merge_room_config(room, value), ensure_ascii=False)
                    room.room_config_json = value
                    continue
                setattr(room, key, value)
        db.add(room)
        return room


def _mark_session_interrupted(session_id: int) -> None:
    """将会话标记为中断。

    :param session_id: 会话 id。
    """
    with get_session() as db:
        sess = db.get(RecordingSession, session_id)
        if sess is not None:
            sess.status = SessionStatus.INTERRUPTED
            db.add(sess)


async def auto_recover_interrupted_sessions() -> list[int]:
    """启动时扫描中断的录制会话并尝试恢复。

    查找最近 N 小时内状态为 RECORDING/RECONNECTING/STARTING 的会话,
    对归属房间自动重新启动录制任务。

    :returns: 已恢复的房间 db_id 列表。
    """
    from datetime import timedelta

    from app.db.models import utcnow

    cutoff = utcnow() - timedelta(hours=settings.auto_recover_max_age_hours)
    with get_session() as db:
        sessions = db.exec(
            select(RecordingSession).where(
                RecordingSession.status.in_(
                    [
                        SessionStatus.RECORDING,
                        SessionStatus.RECONNECTING,
                        SessionStatus.STARTING,
                        SessionStatus.STOPPING,
                        SessionStatus.FINALIZING,
                    ]
                ),
                RecordingSession.started_at >= cutoff,
            )
        ).all()

    recovered: list[int] = []
    for sess in sessions:
        room_id = sess.room_id
        if recorder_manager.is_running(room_id):
            continue
        try:
            from app.analysis.room_config import load_room_config

            with get_session() as db:
                room = db.get(LiveRoom, room_id)
                if room is None or not room.authorized:
                    continue
                paused = bool(load_room_config(room).get("recording_paused", False))
            if paused:
                _set_session_status(sess.id, SessionStatus.PAUSED)
                continue
            # 标记旧会话为中断。
            _mark_session_interrupted(sess.id)
            await recorder_manager.start(room_id, pipeline=True, produce=False)
            recovered.append(room_id)
            logger.info("自动恢复录制:房间 #{} (会话 {})", room_id, sess.id)
            push_notification(
                f"检测到中断的录制会话(房间 #{room_id}),已自动恢复。",
                kind="warning",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("自动恢复房间 #{} 失败: {}", room_id, exc)
            push_notification(f"自动恢复房间 #{room_id} 失败:{exc}", kind="warning")

    if recovered:
        logger.info("自动恢复完成:共恢复 {} 个房间。", len(recovered))
    return recovered


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
            n_seg = len(db.exec(select(RawSegment).where(RawSegment.session_id == s.id)).all())
            result.append(
                {
                    "id": s.id,
                    "room_id": s.room_id,
                    "status": s.status,
                    "stream_format": s.stream_format,
                    "quality": s.quality,
                    "reconnect_count": s.reconnect_count,
                    "last_reconnected_at": s.last_reconnected_at.isoformat() if s.last_reconnected_at else None,
                    "segments": n_seg,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "error_message": s.error_message,
                }
            )
    return result
