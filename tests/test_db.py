"""数据库模型与会话的基础测试。"""

from __future__ import annotations

from app.db.models import LiveRoom, RoomMode


def test_create_and_query_room(temp_db: None) -> None:
    """能写入并查询直播间,默认值符合预期。"""
    from sqlmodel import select

    from app.db.session import get_session

    with get_session() as db:
        room = LiveRoom(input_url="https://live.bilibili.com/123", room_id=123, authorized=True)
        db.add(room)
        db.flush()
        db.refresh(room)
        new_id = room.id

    assert new_id is not None

    with get_session() as db:
        fetched = db.exec(select(LiveRoom).where(LiveRoom.room_id == 123)).first()
        assert fetched is not None
        assert fetched.mode == RoomMode.MANUAL
        assert fetched.enabled is False
        assert fetched.authorized is True
