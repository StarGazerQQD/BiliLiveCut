from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from loguru import logger
from sqlmodel import select

from app.core.logging import _db_sink, _filter_db_sink
from app.db import telemetry
from app.db.entities import AppSetting, SystemLog
from app.db.session import get_session

if TYPE_CHECKING:
    from loguru import Message
    from pytest import MonkeyPatch


@pytest.mark.parametrize("rollback", [False, True])
def test_slow_transaction_reports_actual_outcome_without_recursive_database_log(
    temp_db: None,
    monkeypatch: MonkeyPatch,
    rollback: bool,
) -> None:
    from app.db.session import engine

    clock = [0.0]
    monkeypatch.setattr(telemetry, "monotonic", lambda: clock[0])
    messages: list[Message] = []
    capture = logger.add(messages.append, filter=lambda record: "db_tx_slow" in record["message"])
    database = logger.add(_db_sink, filter=_filter_db_sink, level="WARNING")
    before = telemetry.database_telemetry_snapshot(engine)
    try:

        def write() -> None:
            with get_session(operation="concurrency-test", task_id=17) as db:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                db.add(AppSetting(key="telemetry-test", value="committed"))
                db.flush()
                clock[0] = 1.25
                if rollback:
                    raise ValueError("rollback the actual transaction")

        if rollback:
            with pytest.raises(ValueError, match="rollback the actual"):
                write()
        else:
            write()
    finally:
        logger.remove(capture)
        logger.remove(database)
    assert len(messages) == 1
    assert f"outcome={'rolled_back' if rollback else 'committed'}" in str(messages[0])
    assert "duration_ms=1250.00" in str(messages[0])
    assert "write_lock_wait_ms=0.0" in str(messages[0])
    assert "operation=concurrency-test" in str(messages[0]) and "task_id=17" in str(messages[0])
    assert "transaction_p95_ms=" in str(messages[0])
    assert messages[0].record["extra"]["operation"] == "concurrency-test"
    assert messages[0].record["extra"]["task_id"] == 17
    assert messages[0].record["extra"]["_skip_db"] is True
    after = telemetry.database_telemetry_snapshot(engine)
    assert after["transactions"] == before["transactions"] + 1
    assert after["pool_checkedout"] == 0
    with get_session() as db:
        assert (db.get(AppSetting, "telemetry-test") is None) is rollback
        assert not db.exec(select(SystemLog).where(SystemLog.message.contains("db_tx_slow"))).all()


def test_savepoint_commit_does_not_report_outer_rollback_as_committed(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(telemetry, "monotonic", lambda: clock[0])
    messages: list[Message] = []
    capture = logger.add(messages.append, filter=lambda record: "db_tx_slow" in record["message"])
    try:
        with pytest.raises(ValueError, match="outer rollback"):
            with get_session(operation="savepoint-test") as db:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                with db.begin_nested():
                    db.add(AppSetting(key="nested-telemetry", value="must roll back"))
                clock[0] = 2.5
                raise ValueError("outer rollback")
    finally:
        logger.remove(capture)
    assert len(messages) == 1
    assert "outcome=rolled_back" in str(messages[0])
    assert messages[0].record["level"].name == "ERROR"
    with get_session() as db:
        assert db.get(AppSetting, "nested-telemetry") is None


def test_transaction_statistics_keep_a_bounded_recent_window(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.db.session import engine

    clock = [0.0]
    monkeypatch.setattr(telemetry, "monotonic", lambda: clock[0])
    before = telemetry.database_telemetry_snapshot(engine)
    for index in range(513):
        with get_session(operation="bounded-metrics") as db:
            db.exec(select(AppSetting).limit(1)).first()
            clock[0] += 10.0 if index == 0 else 0.001
    result = telemetry.database_telemetry_snapshot(engine)
    assert result["transactions"] == before["transactions"] + 513
    assert result["transaction_max_ms"] < 2.0
    assert result["transaction_p95_ms"] < 2.0
    assert result["checkouts"] == result["checkins"]
