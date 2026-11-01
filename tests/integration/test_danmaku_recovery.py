from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, TextIO

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlmodel import select

from app.analysis.room_config import merge_room_config
from app.analysis.source_policy import session_has_danmaku
from app.db.entities import Danmaku, LiveRoom, RecordingSession
from app.db.session import get_session
from app.plugins.live_source import DanmakuEvent, DanmakuSink, DanmakuStateSink, DanmakuStatus, SourceRoom
from app.recording import danmaku as capture_module
from app.recording.danmaku import CaptureInterval, DanmakuCapture, DanmakuEvidence, read_evidence
from app.recording.danmaku_archive import archive_path
from app.recording.danmaku_journal import DanmakuJournal, drain_journal, journal_path
from app.sources.bilibili.danmaku import OP_AUTH_REPLY, OP_MESSAGE, encode_packet
from app.sources.bilibili.source import BilibiliSource
from tests.integration.test_bilibili_client import _WBI_RESPONSE
from tests.integration.test_network_recovery import http_boundary
from tests.unit.test_danmaku_batching import Feed, stored_count


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


class QueueFeed(Feed):
    def __init__(self) -> None:
        super().__init__(0)
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def collect_danmaku(self, room: SourceRoom, emit: DanmakuSink, state: DanmakuStateSink) -> None:
        await state(DanmakuStatus.AVAILABLE)
        while True:
            content = await self.queue.get()
            await emit(DanmakuEvent(occurred_at=datetime.now(UTC), content=content))


class Socket:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.queue.put_nowait(encode_packet(OP_AUTH_REPLY, b'{"code":0}'))
        self.closed = False
        self.connections = 0

    async def __aenter__(self) -> Socket:
        """记录外部连接建立，消费使用真实协议帧。"""
        self.connections += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        """记录外部连接已经释放。"""
        self.closed = True

    async def send(self, payload: bytes) -> None:
        pass

    async def recv(self) -> bytes:
        return await self.queue.get()

    def message(self, content: str) -> None:
        payload = {"cmd": "DANMU_MSG", "info": [[], content, [1, "观众"]]}
        self.queue.put_nowait(encode_packet(OP_MESSAGE, json.dumps(payload).encode()))


def bili_socket(monkeypatch: pytest.MonkeyPatch) -> Socket:
    socket = Socket()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json=_WBI_RESPONSE)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "token": "test-token",
                    "host_list": [{"host": "fixture.invalid", "wss_port": 443}],
                },
            },
        )

    http_boundary(monkeypatch, respond)
    monkeypatch.setattr("websockets.connect", lambda *args, **kwargs: socket)
    with get_session() as db:
        room = LiveRoom(input_url="https://live.bilibili.com/123", platform="bilibili", room_id=123)
        room.room_config_json = json.dumps(
            merge_room_config(
                room,
                {"danmaku_sampling": {"danmaku_rate": 1.0, "high_density_enabled": False}},
            )
        )
        db.add(room)
        db.flush()
        db.add(RecordingSession(room_id=room.id, status="recording"))
    return socket


@pytest.mark.parametrize("provider", ["public", "bilibili"])
@pytest.mark.parametrize("failure", ["sqlite_busy", "pool_timeout"])
async def test_collection_continues_through_database_outage_without_resampling(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    failure: str,
) -> None:
    from app.db.session import engine

    monkeypatch.setattr(capture_module, "_BATCH_POLL_S", 0.01)
    monkeypatch.setattr(capture_module, "_COVERAGE_POLL_S", 0.02)
    socket = bili_socket(monkeypatch) if provider == "bilibili" else None
    feed = QueueFeed()
    room = SourceRoom(platform=provider, source_id="123", canonical_url="https://fixture.invalid/123")
    capture = DanmakuCapture(BilibiliSource() if socket else feed, room, 1, 1)
    send = socket.message if socket else feed.queue.put_nowait
    blocked = False
    faults = 0

    def checkout(connection: sqlite3.Connection, record: object, proxy: object) -> None:
        nonlocal faults
        if blocked and failure == "pool_timeout":
            faults += 1
            raise PoolTimeout("controlled connection pool timeout")
        connection.execute("PRAGMA busy_timeout=20")

    event.listen(engine, "checkout", checkout)
    lock: sqlite3.Connection | None = None
    try:
        await capture.start()
        send("before")
        await until(lambda: stored_count() == 1)
        with get_session() as db:
            baseline = read_evidence(db, 1)
        if failure == "sqlite_busy":
            lock = sqlite3.connect(engine.url.database, isolation_level=None)
            lock.execute("BEGIN IMMEDIATE")
        blocked = True
        send("same-message")
        send("same-message")
        await until(lambda: archive_path(1).read_text(encoding="utf-8").count("same-message") == 2)
        await until(lambda: capture._database_failure)
        assert capture._task is not None and not capture._task.done()
        # 原始档案先于补做文件写入；等接收线程返回后核查完整持久记录。
        async with asyncio.timeout(5):
            async with capture._state_lock:
                assert journal_path(1).read_text(encoding="utf-8").count("same-message") == 2
        if failure == "sqlite_busy":
            with get_session() as db:
                assert read_evidence(db, 1).confirmed_until == baseline.confirmed_until
                assert len(db.exec(select(Danmaku)).all()) == 1
            lock.rollback()
            lock.close()
            lock = None
        else:
            assert faults > 0
        blocked = False
        send("after")
        await until(lambda: stored_count() == 4)
        assert not capture._task.done()
        if socket:
            assert socket.connections == 1 and not socket.closed
    finally:
        blocked = False
        if lock is not None:
            lock.rollback()
            lock.close()
        await capture.stop()
        event.remove(engine, "checkout", checkout)
    with get_session() as db:
        assert [row.content for row in db.exec(select(Danmaku).order_by(Danmaku.id)).all()] == [
            "before",
            "same-message",
            "same-message",
            "after",
        ]
        evidence = read_evidence(db, 1)
        assert evidence.ended_at is not None and not evidence.interrupted
    assert drain_journal(1) and drain_journal(1)
    assert stored_count() == 4


@pytest.mark.parametrize("provider", ["public", "bilibili"])
@pytest.mark.parametrize("failure", ["open", "partial_write"])
async def test_journal_write_failure_preserves_prefix_and_does_not_confirm_missing_window(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    failure: str,
) -> None:
    monkeypatch.setattr(capture_module, "_BATCH_POLL_S", 0.01)
    monkeypatch.setattr(capture_module, "_COVERAGE_POLL_S", 600)
    socket = bili_socket(monkeypatch) if provider == "bilibili" else None
    feed = QueueFeed()
    room = SourceRoom(platform=provider, source_id="123", canonical_url="https://fixture.invalid/123")
    capture = DanmakuCapture(BilibiliSource() if socket else feed, room, 1, 1)
    send = socket.message if socket else feed.queue.put_nowait
    await capture.start()
    send("before")
    await until(lambda: stored_count() == 1)
    await capture._save()
    await capture._flush_pending()
    with get_session() as db:
        confirmed = read_evidence(db, 1).confirmed_until
    original = Path.open
    injected = False

    class PartialWrite:
        def __init__(self, stream: BinaryIO) -> None:
            self.stream = stream

        def __enter__(self) -> PartialWrite:
            return self

        def __exit__(self, *args: object) -> None:
            self.stream.close()

        def write(self, data: bytes) -> int:
            nonlocal injected
            self.stream.write(data[: len(data) // 2])
            self.stream.flush()
            injected = True
            raise OSError("controlled partial file write")

    def fault(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> BinaryIO | TextIO | PartialWrite:
        nonlocal injected
        if path == journal_path(1) and mode == "ab" and not injected:
            if failure == "open":
                injected = True
                raise OSError("controlled file open failure")
            return PartialWrite(original(path, mode))
        return original(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", fault)
    failed_at = datetime.now(UTC)
    send("missing-window")
    try:
        await until(lambda: capture._task.done())
        assert injected and capture.evidence.confirmed_until == confirmed
        original_bytes = journal_path(1).read_bytes()
    finally:
        with pytest.raises(OSError, match="停止追加"):
            await capture.stop()
    assert journal_path(1).read_bytes() == original_bytes
    assert drain_journal(1, close=True) and drain_journal(1, close=True)
    assert journal_path(1).read_bytes() == original_bytes
    with get_session() as db:
        assert [row.content for row in db.exec(select(Danmaku)).all()] == ["before"]
        evidence = read_evidence(db, 1)
        assert evidence.interrupted and evidence.confirmed_until == confirmed
        assert evidence.intervals[-1].end == confirmed
    assert not session_has_danmaku(1, failed_at, datetime.now(UTC))
    assert "missing-window" in archive_path(1).read_text(encoding="utf-8")


def test_replay_is_bounded_atomic_and_preserves_duplicate_events(temp_db: None) -> None:
    journal = DanmakuJournal(1)
    now = datetime.now(UTC)
    journal.append_observation(DanmakuEvidence(status=DanmakuStatus.AVAILABLE, intervals=[CaptureInterval(start=now)]))
    rows = [
        Danmaku(session_id=1, room_id=123, ts=now, content="同文同时刻", value=30, msg_type="superchat")
        for _ in range(205)
    ]
    journal.append_messages(rows)
    journal.append_observation(
        DanmakuEvidence(
            status=DanmakuStatus.AVAILABLE,
            intervals=[CaptureInterval(start=now, end=now + timedelta(seconds=1))],
            confirmed_until=now + timedelta(seconds=1),
            ended_at=now + timedelta(seconds=1),
        )
    )
    assert not drain_journal(1)
    assert stored_count() == 100
    with get_session() as db:
        assert read_evidence(db, 1).confirmed_until is None
    assert not drain_journal(1)
    assert stored_count() == 200
    assert drain_journal(1)
    assert stored_count() == 205
    with get_session() as db:
        stored = db.exec(select(Danmaku)).all()
        assert all(
            row.ts == now.replace(tzinfo=None) and row.value == 30 and row.msg_type == "superchat" for row in stored
        )
        assert read_evidence(db, 1).confirmed_until == now + timedelta(seconds=1)
    assert drain_journal(1, close=True)
    assert stored_count() == 205


async def test_stop_during_outage_leaves_durable_tail_for_recovery(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.session import engine

    monkeypatch.setattr(capture_module, "_BATCH_POLL_S", 0.01)
    feed = QueueFeed()
    capture = DanmakuCapture(
        feed, SourceRoom(platform="public", source_id="1", canonical_url="https://fixture.invalid"), 1, 1
    )
    blocked = False

    def checkout(connection: sqlite3.Connection, record: object, proxy: object) -> None:
        if blocked:
            raise PoolTimeout("controlled outage through stop")

    event.listen(engine, "checkout", checkout)
    try:
        await capture.start()
        blocked = True
        feed.queue.put_nowait("durable-tail")
        await until(lambda: journal_path(1).exists() and "durable-tail" in journal_path(1).read_text(encoding="utf-8"))
        await asyncio.wait_for(capture.stop(), 2)
    finally:
        blocked = False
        event.remove(engine, "checkout", checkout)
    assert stored_count() == 0
    assert drain_journal(1, close=True)
    with get_session() as db:
        assert [row.content for row in db.exec(select(Danmaku)).all()] == ["durable-tail"]
        assert read_evidence(db, 1).ended_at is not None
        assert not read_evidence(db, 1).interrupted


@pytest.mark.parametrize("damage", ["missing", "replaced", "truncated", "invalid_record"])
def test_journal_damage_is_explicit_and_never_resets_consumption(temp_db: None, damage: str) -> None:
    journal = DanmakuJournal(1)
    journal.append_messages([Danmaku(session_id=1, room_id=123, content="preserved")])
    assert drain_journal(1)
    path = journal_path(1)
    original = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "replaced":
        header, tail = original.split(b"\n", 1)
        payload = json.loads(header)
        payload["identity"] = "replacement"
        path.write_bytes(json.dumps(payload).encode() + b"\n" + tail)
    elif damage == "truncated":
        path.write_bytes(original.split(b"\n", 1)[0] + b"\n")
    else:
        with path.open("ab") as stream:
            stream.write(b'{"kind":\n')
    with pytest.raises(ValueError):
        drain_journal(1, close=True)
    assert stored_count() == 1


def test_torn_final_line_recovers_complete_prefix_and_retains_uncertainty(temp_db: None) -> None:
    journal = DanmakuJournal(1)
    now = datetime.now(UTC)
    journal.append_observation(
        DanmakuEvidence(status="available", intervals=[CaptureInterval(start=now)], confirmed_until=now)
    )
    journal.append_messages([Danmaku(session_id=1, room_id=123, content="complete")])
    path = journal_path(1)
    with path.open("ab") as stream:
        stream.write(b'{"kind":"message","content":"incomplete')
    original = path.read_bytes()
    assert drain_journal(1, close=True)
    assert stored_count() == 1
    with get_session() as db:
        evidence = read_evidence(db, 1)
        assert evidence.confirmed_until == evidence.ended_at == now
        assert evidence.interrupted and "末行写入中断" in evidence.archive_error
    assert drain_journal(1, close=True)
    assert path.read_bytes() == original and stored_count() == 1
    with path.open("ab") as stream:
        stream.write(b'"}\n')
    with pytest.raises(ValueError, match="不完整尾行发生变化"):
        drain_journal(1, close=True)


@pytest.mark.parametrize("mode", ["unconsumed", "committed", "torn"])
def test_real_process_exit_replays_only_uncommitted_records(temp_db: None, mode: str) -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("danmaku_journal_actor.py")), mode],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 31, result.stderr.decode(errors="replace")
    original = journal_path(1).read_bytes()
    assert drain_journal(1, close=True)
    assert drain_journal(1, close=True)
    with get_session() as db:
        assert [row.content for row in db.exec(select(Danmaku)).all()] == ["same-event", "same-event"]
        evidence = read_evidence(db, 1)
        assert evidence.interrupted and evidence.ended_at == evidence.confirmed_until
        assert (evidence.archive_error is not None) == (mode == "torn")
    assert journal_path(1).read_bytes() == original
