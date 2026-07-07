"""Rooms (V0.1.14.1)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger
from sqlmodel import select

from app.core import settings_store
from app.core.config import settings
from app.core.cookie import get_bilibili_cookie
from app.core.osutil import open_path
from app.core.paths import clips_dir, ready_to_upload_dir
from app.db.models import (
    LiveRoom,
    RawSegment,
    RecordingSession,
    SessionStatus,
)
from app.db.session import get_session
from app.recording.recorder import Recorder
from app.sources.bilibili.client import BilibiliLiveClient
from app.web.services.notifications import push_notification


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

            on_segment = make_pipeline_callback(produce=produce, room_id=db_id)

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
                    value = json.dumps(value, ensure_ascii=False)
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
                    [SessionStatus.RECORDING, SessionStatus.RECONNECTING, SessionStatus.STARTING]
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
            with get_session() as db:
                room = db.get(LiveRoom, room_id)
                if room is None or not room.authorized:
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
