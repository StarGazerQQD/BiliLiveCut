from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, ExecutionContext
from sqlmodel import select

from app.db import session as database
from app.db.entities import SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline import heartbeat


def seed_tasks(count: int) -> list[int]:
    with get_session() as db:
        tasks = [
            SegmentTask(
                segment_id=index + 1,
                session_id=1,
                stage=TaskStatus.ANALYZING,
                claimed_by=heartbeat._WORKER_ID,
                lease_token=f"lease-{index}",
            )
            for index in range(count)
        ]
        db.add_all(tasks)
        db.flush()
        return [task.id for task in tasks if task.id is not None]


def wait_for_worker_exit() -> None:
    with heartbeat._CONDITION:
        assert heartbeat._CONDITION.wait_for(lambda: heartbeat._worker is None, timeout=5)
        assert not heartbeat._REGISTRATIONS


def test_many_leases_share_bounded_batch_updates(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = seed_tasks(130)
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_POLL_S", 3600)
    writes: list[object] = []

    def observe(
        _connection: Connection,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: ExecutionContext,
        _many: bool,
    ) -> None:
        if statement.startswith("UPDATE segment_tasks SET heartbeat_at"):
            writes.append(parameters)

    event.listen(database.engine, "before_cursor_execute", observe)
    stops: list[threading.Event] = []
    try:
        with heartbeat._CONDITION:
            for index, task_id in enumerate(ids):
                stops.append(heartbeat.start_heartbeat_thread(task_id, f"lease-{index}", TaskStatus.ANALYZING))
            time.sleep(0.02)
        deadline = time.monotonic() + 5
        while True:
            with get_session() as db:
                remaining = db.exec(select(SegmentTask.id).where(SegmentTask.heartbeat_at.is_(None))).all()
            if not remaining:
                break
            assert time.monotonic() < deadline
            threading.Event().wait(0.01)
        assert len(writes) == 3
        assert len([worker for worker in threading.enumerate() if worker.name == "lease-heartbeats"]) == 1
        assert all(len(parameters) <= 1 + 64 * 4 for parameters in writes)  # type: ignore[arg-type]
    finally:
        for stop in stops:
            stop.set()
        wait_for_worker_exit()
        event.remove(database.engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("changed", ["lease_token", "claimed_by", "stage"])
def test_batch_preserves_each_full_lease_identity(temp_db: None, monkeypatch: pytest.MonkeyPatch, changed: str) -> None:
    first, second = seed_tasks(2)
    now = datetime(2026, 10, 30, tzinfo=UTC)
    monkeypatch.setattr(heartbeat, "now_utc", lambda: now)
    with get_session() as db:
        task = db.get(SegmentTask, second)
        assert task is not None
        setattr(task, changed, {"lease_token": "new", "claimed_by": "other", "stage": TaskStatus.RENDERING}[changed])
        db.add(task)
    first_stop = heartbeat.start_heartbeat_thread(first, "lease-0", TaskStatus.ANALYZING)
    second_stop = heartbeat.start_heartbeat_thread(second, "lease-1", TaskStatus.ANALYZING)
    try:
        assert second_stop.wait(5)
    finally:
        first_stop.set()
        second_stop.set()
        wait_for_worker_exit()
    with get_session() as db:
        assert db.get(SegmentTask, first).heartbeat_at == now.replace(tzinfo=None)
        assert db.get(SegmentTask, second).heartbeat_at is None


def test_stop_waits_for_inflight_batch_before_heartbeat_clear(temp_db: None) -> None:
    task_id = seed_tasks(1)[0]
    entered, release, stopping, stopped = (threading.Event() for _ in range(4))

    def hold_database_write(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _many: bool,
    ) -> None:
        if statement.startswith("UPDATE segment_tasks SET heartbeat_at") and not release.is_set():
            entered.set()
            assert release.wait(5)

    event.listen(database.engine, "before_cursor_execute", hold_database_write)
    stop = heartbeat.start_heartbeat_thread(task_id, "lease-0", TaskStatus.ANALYZING)

    def finish() -> None:
        stopping.set()
        stop.set()
        heartbeat.clear_heartbeat_if_own(task_id, "lease-0")
        stopped.set()

    try:
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(finish)
            try:
                assert stopping.wait(5)
                assert not stopped.wait(0.05)
            finally:
                release.set()
            future.result(timeout=5)
        wait_for_worker_exit()
        with get_session() as db:
            assert db.get(SegmentTask, task_id).heartbeat_at is None
    finally:
        release.set()
        stop.set()
        event.remove(database.engine, "before_cursor_execute", hold_database_write)


def test_failed_batch_retries_without_abandoning_registration(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = seed_tasks(1)[0]
    monkeypatch.setattr(heartbeat, "_HEARTBEAT_POLL_S", 0.02)
    failed = threading.Event()
    sink = logger.add(lambda message: failed.set() if "heartbeat_failed" in str(message) else None)
    with get_session() as db:
        db.exec(
            text("CREATE TRIGGER deny_heartbeat BEFORE UPDATE ON segment_tasks BEGIN SELECT RAISE(FAIL, 'busy'); END")
        )
    stop = heartbeat.start_heartbeat_thread(task_id, "lease-0", TaskStatus.ANALYZING)
    try:
        assert failed.wait(5)
        assert not stop.is_set()
        with get_session() as db:
            db.exec(text("DROP TRIGGER deny_heartbeat"))
        deadline = time.monotonic() + 5
        while True:
            with get_session() as db:
                task = db.get(SegmentTask, task_id)
                assert task is not None
                if task.heartbeat_at is not None:
                    break
            assert time.monotonic() < deadline
            threading.Event().wait(0.01)
    finally:
        stop.set()
        wait_for_worker_exit()
        logger.remove(sink)
