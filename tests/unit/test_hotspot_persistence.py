"""HotspotEvent 模型与持久化原语回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError


def _create_session() -> int:
    """创建一个可供热点外键引用的录制场次。"""
    from app.db.entities import LiveRoom, RecordingSession
    from app.db.session import get_session

    with get_session() as db:
        room = LiveRoom(input_url="100", room_id=100, authorized=True)
        db.add(room)
        db.flush()
        assert room.id is not None
        session = RecordingSession(room_id=room.id)
        db.add(session)
        db.flush()
        assert session.id is not None
        return session.id


def test_fresh_schema_contains_hotspot_constraints(temp_db: None) -> None:
    """新数据库包含热点表、复合索引、可空候选和三条外键。"""
    from sqlmodel import Session

    from app.db.schema import CURRENT_SCHEMA_VERSION, SchemaMeta, _verify_critical_indexes, _verify_foreign_keys
    from app.db.session import engine

    with engine.connect() as connection:
        columns = {row[1]: row for row in connection.exec_driver_sql("PRAGMA table_info('hotspot_events')").fetchall()}
        indexes = connection.exec_driver_sql("PRAGMA index_list('hotspot_events')").fetchall()
        index_columns = {
            tuple(column[2] for column in connection.exec_driver_sql(f"PRAGMA index_info('{row[1]}')").fetchall())
            for row in indexes
        }
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_list('hotspot_events')").fetchall()

    with Session(engine) as db:
        meta = db.get(SchemaMeta, 1)

    assert meta is not None
    assert meta.schema_version == CURRENT_SCHEMA_VERSION == 5
    assert columns["candidate_id"][3] == 0
    assert ("session_id", "status", "peak_ts") in index_columns
    assert {(row[3], row[2], row[4]) for row in foreign_keys} >= {
        ("session_id", "recording_sessions", "id"),
        ("candidate_id", "highlight_candidates", "id"),
        ("merged_into_id", "hotspot_events", "id"),
    }
    assert _verify_critical_indexes() is True
    assert _verify_foreign_keys() is True


def test_get_or_create_is_idempotent_and_keeps_multiple_events(temp_db: None) -> None:
    """相同业务键复用，不同业务键可在同场次独立持久化。"""
    from app.db.hotspot_store import get_or_create_hotspot_event, list_session_hotspot_events
    from app.db.session import get_session

    session_id = _create_session()
    start = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    with get_session() as db:
        first, first_created = get_or_create_hotspot_event(
            db,
            event_key=f"session:{session_id}:signal:1",
            session_id=session_id,
            start_ts=start,
            peak_ts=start + timedelta(seconds=8),
            end_ts=start + timedelta(seconds=20),
            heat_score=0.72,
        )
        repeated, repeated_created = get_or_create_hotspot_event(
            db,
            event_key=f"session:{session_id}:signal:1",
            session_id=session_id,
            start_ts=start,
            peak_ts=start + timedelta(seconds=8),
            end_ts=start + timedelta(seconds=20),
        )
        second, second_created = get_or_create_hotspot_event(
            db,
            event_key=f"session:{session_id}:signal:2",
            session_id=session_id,
            start_ts=start + timedelta(seconds=30),
            peak_ts=start + timedelta(seconds=38),
            end_ts=start + timedelta(seconds=50),
        )
        events = list_session_hotspot_events(db, session_id)

    assert first_created is True
    assert repeated_created is False
    assert second_created is True
    assert first.id == repeated.id
    assert first.id != second.id
    assert [event.event_key for event in events] == [
        f"session:{session_id}:signal:1",
        f"session:{session_id}:signal:2",
    ]
    assert all(event.candidate_id is None for event in events)


def test_hotspot_store_rejects_invalid_time_and_cross_session_key(temp_db: None) -> None:
    """持久化入口拒绝反向时间窗和跨场次复用业务键。"""
    from app.db.hotspot_store import get_or_create_hotspot_event
    from app.db.session import get_session

    first_session = _create_session()
    second_session = _create_session()
    now = datetime.now(UTC)
    with get_session() as db:
        with pytest.raises(ValueError, match="start_ts"):
            get_or_create_hotspot_event(
                db,
                event_key="invalid-time",
                session_id=first_session,
                start_ts=now,
                peak_ts=now - timedelta(seconds=1),
                end_ts=now + timedelta(seconds=1),
            )
        get_or_create_hotspot_event(
            db,
            event_key="stable-key",
            session_id=first_session,
            start_ts=now,
            peak_ts=now,
            end_ts=now,
        )
        with pytest.raises(ValueError, match="另一录制场次"):
            get_or_create_hotspot_event(
                db,
                event_key="stable-key",
                session_id=second_session,
                start_ts=now,
                peak_ts=now,
                end_ts=now,
            )


def test_database_check_constraint_rejects_reverse_time(temp_db: None) -> None:
    """绕过持久化入口时数据库仍拒绝反向时间窗。"""
    from app.db.entities import HotspotEvent
    from app.db.session import get_session

    session_id = _create_session()
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        with get_session() as db:
            db.add(
                HotspotEvent(
                    event_key="invalid-direct",
                    session_id=session_id,
                    start_ts=now,
                    peak_ts=now - timedelta(seconds=1),
                    end_ts=now,
                )
            )
            db.flush()
