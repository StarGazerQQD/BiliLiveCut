"""区分导入媒体与直播的时间轴、弹幕证据语义。"""

from __future__ import annotations

import json

from sqlmodel import Session

from app.core.config import settings
from app.db.entities import AppSetting, LiveRoom, RecordingSession
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
    """导入文件已按媒体时间对齐；直播保持已配置的接收延迟补偿。"""
    return 0.0 if is_local_session(session_id) else settings.danmaku_event_lag_s


def session_has_danmaku(session_id: int) -> bool:
    """区分未提供弹幕与提供了空弹幕文件，保持证据可用性语义。"""
    with get_session() as db:
        if not is_local_session(session_id, db):
            return bool(settings.collect_danmaku)
        row = db.get(AppSetting, f"local_source:{session_id}")
        if row is None:
            return False
        payload = json.loads(row.value)
        return payload.get("has_comments") is True
