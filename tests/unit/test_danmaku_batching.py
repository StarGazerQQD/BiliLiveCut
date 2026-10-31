from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from threading import Event
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.db.entities import Danmaku
from app.db.session import get_session
from app.plugins.live_source import DanmakuEvent, DanmakuSink, DanmakuStateSink, DanmakuStatus, SourceRoom
from app.recording import danmaku as capture_module
from app.recording.danmaku import DanmakuCapture
from app.recording.danmaku_archive import archive_path
from tests.unit.test_live_sources import Source

if TYPE_CHECKING:
    from pytest import MonkeyPatch


class Feed(Source):
    def __init__(self, count: int, *, pause: bool = False) -> None:
        super().__init__()
        self.count = count
        self.pause = pause
        self.sent = asyncio.Event()

    async def collect_danmaku(self, room: SourceRoom, emit: DanmakuSink, state: DanmakuStateSink) -> None:
        await state(DanmakuStatus.AVAILABLE)
        for index in range(self.count):
            await emit(DanmakuEvent(occurred_at=datetime.now(UTC), content=f"batch-{index}"))
        self.sent.set()
        if self.pause:
            await asyncio.Event().wait()


def capture_for(feed: Feed) -> DanmakuCapture:
    return DanmakuCapture(
        feed, SourceRoom(platform="sample", source_id="batch", canonical_url="https://sample.invalid/batch"), 1, 1
    )


def stored_count() -> int:
    with get_session() as db:
        return len(db.exec(select(Danmaku)).all())


async def test_normal_collector_end_flushes_full_batches_and_tail(temp_db: None) -> None:
    from app.db.session import engine

    transactions: set[object] = set()

    def observe(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        if context.isinsert and "danmaku" in statement:
            transactions.add(connection.get_transaction())

    feed = Feed(253)
    capture = capture_for(feed)
    event.listen(engine, "before_cursor_execute", observe)
    try:
        await capture.start()
        assert capture._task is not None
        await asyncio.wait_for(asyncio.shield(capture._task), 5)
        await capture.stop()
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert await asyncio.to_thread(stored_count) == 253
    assert len(transactions) == 3
    records = [json.loads(line) for line in archive_path(1).read_text(encoding="utf-8").splitlines()]
    assert len([record for record in records if record["kind"] == "normalized"]) == 253


async def test_periodic_flush_preserves_small_batch_and_coverage(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(capture_module, "_BATCH_POLL_S", 0.01)
    feed = Feed(1, pause=True)
    capture = capture_for(feed)
    try:
        await capture.start()
        await asyncio.wait_for(feed.sent.wait(), 2)
        async with asyncio.timeout(2):
            while await asyncio.to_thread(stored_count) != 1:
                await asyncio.sleep(0.01)
    finally:
        await capture.stop()
    assert capture.evidence.confirmed_until == capture.evidence.ended_at
    assert await asyncio.to_thread(stored_count) == 1


async def test_failed_batch_keeps_pending_and_does_not_advance_coverage(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_module, "_BATCH_POLL_S", 3600)
    feed = Feed(0, pause=True)
    capture = capture_for(feed)
    await capture.start()
    await asyncio.wait_for(feed.sent.wait(), 2)
    message = DanmakuEvent(occurred_at=datetime.now(UTC), content="retry-without-duplicate")
    await capture._emit(message)
    confirmed = capture.evidence.confirmed_until

    def set_trigger(enabled: bool) -> None:
        with get_session() as db:
            db.exec(
                text(
                    "CREATE TRIGGER reject_danmaku BEFORE INSERT ON danmaku BEGIN SELECT RAISE(FAIL, 'batch denied'); END"
                    if enabled
                    else "DROP TRIGGER reject_danmaku"
                )
            )

    await asyncio.to_thread(set_trigger, True)
    try:
        with pytest.raises(IntegrityError):
            async with capture._state_lock:
                await capture._save()
        assert capture.evidence.confirmed_until == confirmed
        assert await asyncio.to_thread(stored_count) == 0
    finally:
        await asyncio.to_thread(set_trigger, False)
        await capture.stop()
    assert await asyncio.to_thread(stored_count) == 1
    records = [json.loads(line) for line in archive_path(1).read_text(encoding="utf-8").splitlines()]
    assert len([record for record in records if record["kind"] == "normalized"]) == 1


async def test_cancelled_inflight_batch_is_not_replayed_after_commit(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.db.session import engine

    monkeypatch.setattr(capture_module, "_DB_BATCH_SIZE", 1)
    started, release = Event(), Event()

    def before_cursor(
        connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        if context.isinsert and "danmaku" in statement:
            connection.info["test_danmaku_write"] = True

    def committing(connection: Connection) -> None:
        if connection.info.pop("test_danmaku_write", False):
            started.set()
            if not release.wait(5):
                raise TimeoutError("controlled commit was not released")

    capture = capture_for(Feed(0))
    event.listen(engine, "before_cursor_execute", before_cursor)
    event.listen(engine, "commit", committing)
    emitting = asyncio.create_task(capture._emit(DanmakuEvent(occurred_at=datetime.now(UTC), content="cancel-safe")))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        emitting.cancel()
        await asyncio.sleep(0)
        assert not emitting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await emitting
        await capture.stop()
    finally:
        release.set()
        await asyncio.gather(emitting, return_exceptions=True)
        event.remove(engine, "before_cursor_execute", before_cursor)
        event.remove(engine, "commit", committing)
    assert await asyncio.to_thread(stored_count) == 1
