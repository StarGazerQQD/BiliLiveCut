"""区分导入媒体与直播的时间轴、弹幕证据语义。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.core.config import settings
from app.db.entities import AppSetting, Danmaku, LiveRoom, RecordingSession
from app.db.session import get_session


def is_local_session(session_id: int, db: Session | None = None) -> bool:
    """由持久来源平台判断本地场次，避免依赖可丢失的任务快照。"""
    if db is None:
        with get_session() as connection:
            return is_local_session(session_id, connection)
    session = db.get(RecordingSession, session_id)
    room = db.get(LiveRoom, session.room_id) if session else None
    return room is not None and room.platform == "local"


def session_danmaku_lag_s(session_id: int) -> float:
    """本地及外部 UTC 事件无需 Bili 接收延迟；新场次使用当场固定值。"""
    from app.recording.danmaku import read_evidence

    with get_session() as db:
        evidence = read_evidence(db, session_id)
        if evidence is not None:
            return evidence.lag_s
        recording = db.get(RecordingSession, session_id)
        room = db.get(LiveRoom, recording.room_id) if recording else None
        return settings.danmaku_event_lag_s if room is not None and room.platform == "bilibili" else 0.0


def session_has_danmaku(
    session_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> bool:
    """区分缺失和有效零事件；指定窗口时必须有完整连接覆盖或旧场次的实际事件。"""
    from app.recording.danmaku import read_evidence

    with get_session() as db:
        if is_local_session(session_id, db):
            row = db.get(AppSetting, f"local_source:{session_id}")
            return row is not None and json.loads(row.value).get("has_comments") is True
        evidence = read_evidence(db, session_id)
        if evidence is not None:
            if start is None or end is None:
                return bool(evidence.intervals)
            left = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
            right = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
            return any(
                interval.start <= left and (interval.end or evidence.confirmed_until or interval.start) >= right
                for interval in evidence.intervals
            )
        # 旧场次没有连接历史，只认可确实存下来的事件，不从当前全局开关反推。
        stmt = select(Danmaku.id).where(Danmaku.session_id == session_id)
        if start is not None:
            stmt = stmt.where(Danmaku.ts >= start)
        if end is not None:
            stmt = stmt.where(Danmaku.ts < end)
        return db.exec(stmt.limit(1)).first() is not None


def session_danmaku_view(session_id: int) -> dict[str, object]:
    """用于界面与旧评分插件的证据说明，计数和覆盖状态分别展示。"""
    from app.recording.danmaku import read_evidence

    with get_session() as db:
        evidence = read_evidence(db, session_id)
        if evidence is not None:
            return {
                "state": evidence.status.value,
                "interrupted": evidence.interrupted,
                "available": bool(evidence.intervals),
                "ended_at": evidence.ended_at.isoformat() if evidence.ended_at else None,
            }
        if is_local_session(session_id, db):
            available = session_has_danmaku(session_id)
            return {"state": "available" if available else "unsupported", "available": available, "ended_at": None}
        return {"state": "legacy_unknown", "available": session_has_danmaku(session_id), "ended_at": None}
