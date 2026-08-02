"""直播来源身份解析。

把录制会话关联到直播间，并为 Web API 提供稳定、统一的主播与房间标签。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict

from sqlmodel import Session, select

from app.db.models import LiveRoom, RecordingSession


class SourceIdentity(TypedDict):
    """一个录制会话对应的直播来源信息。"""

    room_db_id: int | None
    room_id: int | None
    uploader_name: str | None
    room_title: str | None
    source_label: str


def source_identities_for_sessions(
    db: Session,
    session_ids: Iterable[int],
) -> dict[int, SourceIdentity]:
    """批量解析会话来源，避免候选和任务列表逐行查询数据库。

    :param db: 当前数据库会话。
    :param session_ids: 要解析的录制会话 ID。
    :returns: ``session_id -> SourceIdentity`` 映射。
    """
    ids = sorted(set(session_ids))
    if not ids:
        return {}

    sessions = db.exec(select(RecordingSession).where(RecordingSession.id.in_(ids))).all()
    room_db_ids = sorted({session.room_id for session in sessions})
    rooms = db.exec(select(LiveRoom).where(LiveRoom.id.in_(room_db_ids))).all() if room_db_ids else []
    room_by_id = {room.id: room for room in rooms}

    result: dict[int, SourceIdentity] = {}
    for session in sessions:
        if session.id is None:
            continue
        room = room_by_id.get(session.room_id)
        uploader_name = room.uploader_name.strip() if room and room.uploader_name else None
        room_title = room.title.strip() if room and room.title else None
        public_room_id = room.room_id if room else None
        primary = (
            uploader_name or room_title or (f"房间 {public_room_id}" if public_room_id is not None else "未知来源")
        )
        suffix = (
            f" · 房间 {public_room_id}" if public_room_id is not None and primary != f"房间 {public_room_id}" else ""
        )
        result[session.id] = {
            "room_db_id": room.id if room else session.room_id,
            "room_id": public_room_id,
            "uploader_name": uploader_name,
            "room_title": room_title,
            "source_label": f"{primary}{suffix}",
        }
    return result


def unknown_source_identity() -> SourceIdentity:
    """返回关联数据缺失时的显式来源占位。"""
    return {
        "room_db_id": None,
        "room_id": None,
        "uploader_name": None,
        "room_title": None,
        "source_label": "未知来源",
    }
