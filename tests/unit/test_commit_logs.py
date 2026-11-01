from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial

import pytest
from loguru import logger
from sqlalchemy.exc import IntegrityError

from app.db.entities import AppSetting, HotspotEvent, LiveRoom, RecordingSession
from app.db.hotspot_store import get_or_create_hotspot_event
from app.db.notifications import log_after_commit
from app.db.session import get_session


def test_success_log_waits_for_outer_commit_and_discards_rolled_back_savepoint(temp_db: None) -> None:
    messages: list[str] = []
    with get_session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        db.add(AppSetting(key="outer", value="real row"))
        log_after_commit(db, partial(messages.append, "outer"))
        with db.begin_nested():
            db.add(AppSetting(key="nested-commit", value="real row"))
            log_after_commit(db, partial(messages.append, "nested-commit"))
        with pytest.raises(ValueError):
            with db.begin_nested():
                db.add(AppSetting(key="nested-rollback", value="must disappear"))
                log_after_commit(db, partial(messages.append, "nested-rollback"))
                raise ValueError("rollback nested write")
        assert messages == []
    assert messages == ["outer", "nested-commit"]
    with get_session() as db:
        assert db.get(AppSetting, "nested-commit") is not None
        assert db.get(AppSetting, "nested-rollback") is None


def test_failed_actual_database_commit_does_not_emit_success_log(temp_db: None) -> None:
    with get_session() as db:
        db.add(AppSetting(key="unique-row", value="original"))
    messages: list[str] = []
    with pytest.raises(IntegrityError):
        with get_session() as db:
            db.add(AppSetting(key="unique-row", value="duplicate must fail"))
            log_after_commit(db, partial(messages.append, "must not appear"))
    assert messages == []
    with get_session() as db:
        assert db.get(AppSetting, "unique-row").value == "original"


@pytest.mark.parametrize("rollback", [False, True])
def test_hotspot_created_log_matches_persisted_outer_transaction(temp_db: None, rollback: bool) -> None:
    with get_session() as db:
        db.add(LiveRoom(id=1, input_url="commit-log"))
        db.add(RecordingSession(id=1, room_id=1))
    messages: list[str] = []
    handler = logger.add(
        lambda message: messages.append(str(message)), filter=lambda record: "hotspot_created:" in record["message"]
    )
    start = datetime(2026, 10, 30)
    identity: int | None = None
    try:

        def create() -> None:
            nonlocal identity
            with get_session() as db:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                row, created = get_or_create_hotspot_event(
                    db,
                    event_key="commit-log",
                    session_id=1,
                    start_ts=start,
                    peak_ts=start + timedelta(seconds=2),
                    end_ts=start + timedelta(seconds=5),
                )
                assert created
                identity = row.id
                assert messages == []
                if rollback:
                    raise ValueError("rollback actual hotspot")

        if rollback:
            with pytest.raises(ValueError, match="rollback actual hotspot"):
                create()
        else:
            create()
    finally:
        logger.remove(handler)
    assert len(messages) == (0 if rollback else 1)
    with get_session() as db:
        assert (db.get(HotspotEvent, identity) is None) is rollback
