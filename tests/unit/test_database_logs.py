from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from typing import TYPE_CHECKING

import pytest
from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, ExecutionContext
from sqlmodel import select

from app.core import logging as app_logging
from app.db.entities import SystemLog
from app.db.runtime_logs import RUNTIME_LOG_CONTEXT, prune_runtime_logs
from app.db.session import get_session

if TYPE_CHECKING:
    from loguru import Message
    from pytest import MonkeyPatch


def test_runtime_retention_preserves_legacy_audit_and_failure_records(temp_db: None) -> None:
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
    with get_session() as db:
        db.add_all(
            [
                SystemLog(level="WARNING", event="legacy-warning", created_at=old),
                SystemLog(level="WARNING", event="review.approved", context_json='{"actor":"admin"}', created_at=old),
                SystemLog(level="ERROR", event="failure", context_json=RUNTIME_LOG_CONTEXT, created_at=old),
                SystemLog(level="WARNING", event="new-runtime", context_json=RUNTIME_LOG_CONTEXT, created_at=old),
            ]
        )
    assert prune_runtime_logs() == 1
    with get_session() as db:
        assert {row.event for row in db.exec(select(SystemLog)).all()} == {
            "legacy-warning",
            "review.approved",
            "failure",
        }


def test_runtime_capacity_cleanup_is_bounded_and_keeps_latest_rows(temp_db: None) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with get_session() as db:
        db.add_all(
            [
                SystemLog(
                    level="WARNING",
                    event=str(index),
                    context_json=RUNTIME_LOG_CONTEXT,
                    created_at=now + timedelta(seconds=index),
                )
                for index in range(9)
            ]
        )
    assert prune_runtime_logs(max_rows=5, limit=2) == 2
    assert prune_runtime_logs(max_rows=5, limit=2) == 2
    assert prune_runtime_logs(max_rows=5, limit=2) == 0
    with get_session() as db:
        assert {row.event for row in db.exec(select(SystemLog)).all()} == {"4", "5", "6", "7", "8"}


def test_log_query_indexes_preserve_schema_and_cover_latest_queries(temp_db: None) -> None:
    from app.db.schema import validate_schema

    assert validate_schema()
    with get_session() as db:
        latest = db.exec(
            text("EXPLAIN QUERY PLAN SELECT * FROM system_logs ORDER BY created_at DESC, id DESC LIMIT 50")
        ).all()
        filtered = db.exec(
            text(
                "EXPLAIN QUERY PLAN SELECT * FROM system_logs WHERE level='WARNING' ORDER BY created_at DESC, id DESC LIMIT 50"
            )
        ).all()
    assert any("ix_system_logs_created_at_id" in str(row) for row in latest)
    assert any("ix_system_logs_level_created_at_id" in str(row) for row in filtered)
    assert all("TEMP B-TREE" not in str(row) for row in [*latest, *filtered])


def test_slow_database_bounds_pending_diagnostics_without_blocking_producers(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    from app.db.session import engine

    logger.complete()
    assert app_logging.database_log_queue_snapshot()["pending"] == 0
    monkeypatch.setattr(app_logging, "_DB_CAPACITY", 4)
    insert_started = Event()
    produced = Event()
    transactions: set[object] = set()
    copies: list[Message] = []
    before = app_logging.database_log_queue_snapshot()

    def observe(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        if context.isinsert and "system_logs" in statement:
            transactions.add(connection.get_transaction())
            insert_started.set()

    def produce() -> None:
        for index in range(50):
            logger.warning("pressure-test-{}", index)
        produced.set()

    event.listen(engine, "before_cursor_execute", observe)
    file_copy = logger.add(copies.append, level="WARNING")
    database = None
    if not app_logging._CONFIGURED:
        database = logger.add(app_logging._db_sink, level="WARNING", enqueue=True, filter=app_logging._filter_db_sink)
    producer = Thread(target=produce)
    try:
        with get_session() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            logger.warning("pressure-test-initial")
            assert insert_started.wait(2)
            producer.start()
            assert produced.wait(2), "日志生产者不能等待业务数据库写锁"
            state = app_logging.database_log_queue_snapshot()
            assert state["pending"] <= 4 and state["in_flight"] >= 1
            assert state["dropped"] >= before["dropped"] + 47
    finally:
        producer.join(5) if producer.ident is not None else None
        logger.complete()
        if database is not None:
            logger.remove(database)
        logger.remove(file_copy)
        event.remove(engine, "before_cursor_execute", observe)
    assert not producer.is_alive()
    assert len([message for message in copies if "pressure-test-" in str(message)]) == 51
    assert app_logging.database_log_queue_snapshot()["pending"] == 0
    with get_session() as db:
        stored = db.exec(select(SystemLog).where(SystemLog.message.startswith("pressure-test-"))).all()
    assert len(stored) == 4
    assert len(transactions) <= 2


def test_retention_failure_does_not_count_committed_records_as_dropped(temp_db: None) -> None:
    logger.complete()
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
    with get_session() as db:
        db.add(SystemLog(level="WARNING", event="retention-blocked", context_json=RUNTIME_LOG_CONTEXT, created_at=old))
        db.exec(
            text(
                "CREATE TRIGGER refuse_log_prune BEFORE DELETE ON system_logs "
                "BEGIN SELECT RAISE(FAIL, 'retention test denied'); END"
            )
        )
    before = app_logging.database_log_queue_snapshot()
    handler = None
    if not app_logging._CONFIGURED:
        handler = logger.add(app_logging._db_sink, level="WARNING", enqueue=True, filter=app_logging._filter_db_sink)
    try:
        logger.warning("already-committed-before-retention")
        logger.complete()
    finally:
        if handler is not None:
            logger.remove(handler)
        with get_session() as db:
            db.exec(text("DROP TRIGGER refuse_log_prune"))
    after = app_logging.database_log_queue_snapshot()
    assert after["dropped"] == before["dropped"]
    assert after["retention_failures"] == before["retention_failures"] + 1
    with get_session() as db:
        assert db.exec(select(SystemLog).where(SystemLog.message == "already-committed-before-retention")).one()
        assert db.exec(select(SystemLog).where(SystemLog.event == "retention-blocked")).one()


def test_sink_stop_reports_remaining_rate_limited_failures(
    temp_db: None, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logger.complete()
    app_logging._report_database_log_drops(force=True)
    capsys.readouterr()
    monkeypatch.setattr(app_logging, "_db_last_drop_report", None)
    handler = None
    if not app_logging._CONFIGURED:
        handler = logger.add(
            app_logging._DatabaseLogSink(), level="WARNING", enqueue=True, filter=app_logging._filter_db_sink
        )
    with get_session() as db:
        db.exec(
            text("CREATE TRIGGER deny_diagnostic BEFORE INSERT ON system_logs BEGIN SELECT RAISE(FAIL, 'denied'); END")
        )
    try:
        logger.warning("first-batch-failure")
        logger.complete()
        assert "未保留 1 条副本" in capsys.readouterr().err
        logger.warning("last-batch-failure")
        logger.complete()
        assert "未保留" not in capsys.readouterr().err
        app_logging._DatabaseLogSink().stop()
        assert "未保留 1 条副本" in capsys.readouterr().err
    finally:
        if handler is not None:
            logger.remove(handler)
        with get_session() as db:
            db.exec(text("DROP TRIGGER deny_diagnostic"))
