from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.paths import session_raw_dir
from app.db.entities import LiveRoom, RecordingSession
from app.db.session import get_session
from app.pipeline import live_monitor
from app.plugins.live_source import SourceTemporaryError
from app.recording import intents
from app.recording.recovery import recover_pending_recordings
from app.recording.writer_ownership import SessionWriter
from app.sources.registry import SourceRegistry, source_registry
from app.sources.rooms import register_room, room_source
from app.web.services import rooms
from tests.integration.test_source_runtime import RuntimeSource


@pytest.fixture
def runtime(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> RuntimeSource:
    monkeypatch.setattr(source_registry, "_entries", SourceRegistry(timeout_s=5)._entries)
    monkeypatch.setattr(source_registry, "timeout_s", 5)
    monkeypatch.setattr(source_registry, "retry_delay_s", 0)
    monkeypatch.setattr(settings, "collect_danmaku", False)
    monkeypatch.setattr(rooms, "open_path", lambda _path: False)
    source = RuntimeSource()
    source_registry.register_many("recovery-settings", [source])
    return source


def assert_current_settings(manager: rooms.RecorderManager, room_id: int, expected: tuple[bool, bool]) -> None:
    with get_session() as db:
        current = db.get(LiveRoom, room_id)
        intent = intents.load_intent(db, room_id)
        assert current is not None and intent is not None
        assert (current.auto_analyze, current.auto_render) == expected
        assert (intent.pipeline, intent.produce) == expected
    assert (manager._recorders[room_id].on_segment is not None) is expected[0]


@pytest.mark.parametrize("expected", [(False, False), (True, True), (False, True), (True, False)])
@pytest.mark.parametrize("during_metadata", [False, True])
async def test_cooldown_recovery_uses_latest_independent_settings(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    expected: tuple[bool, bool],
    during_metadata: bool,
) -> None:
    room = await register_room("123", True, "external")
    original = (not expected[0], not expected[1])
    rooms.update_room(room.id, {"auto_record": True, "auto_analyze": original[0], "auto_render": original[1]})
    now = [100.0]
    monkeypatch.setattr(rooms, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 1)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 0)
    runtime.stream_failure = SourceTemporaryError("controlled stream outage")
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    monitor = live_monitor.LiveMonitor()
    starting: asyncio.Task[bool] | None = None
    try:
        assert await manager.start(room.id, pipeline=original[0], produce=original[1])
        await asyncio.wait_for(manager.recording_token(room.id), 5)
        recovery = manager.recovery(room.id)
        assert recovery is not None
        now[0] = recovery.ready_at
        runtime.stream_gate = asyncio.Event()
        if during_metadata:
            runtime.requested.clear()
            runtime.info_gate = asyncio.Event()
            starting = asyncio.create_task(manager.start(room.id, automatic=True, recovery=recovery))
            await asyncio.wait_for(runtime.requested.wait(), 3)
        rooms.update_room(room.id, {"auto_analyze": expected[0], "auto_render": expected[1]})
        if starting is not None:
            runtime.info_gate.set()
            assert await asyncio.wait_for(starting, 5)
        else:
            with get_session() as db:
                current = db.get(LiveRoom, room.id)
            await monitor._check_room(current)
        assert_current_settings(manager, room.id, expected)
        with get_session() as db:
            current_intent = intents.load_intent(db, room.id)
            assert current_intent.generation != recovery.generation
            assert current_intent.origin == "manual"
    finally:
        if runtime.info_gate is not None:
            runtime.info_gate.set()
        if starting is not None:
            await asyncio.gather(starting, return_exceptions=True)
        if runtime.stream_gate is not None:
            runtime.stream_gate.set()
        await manager.stop_all()
        await monitor.stop()


@pytest.mark.parametrize("expected", [(False, False), (True, True), (False, True), (True, False)])
async def test_process_recovery_of_manual_intent_uses_latest_settings_without_auto_record(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, expected: tuple[bool, bool]
) -> None:
    room = await register_room("123", True, "external")
    with get_session() as db:
        intent = intents.accept(
            db, room.id, room_source(room), pipeline=not expected[0], produce=not expected[1], origin="manual"
        )
        session = RecordingSession(room_id=room.id, status="recording")
        db.add(session)
        db.flush()
        with SessionWriter.create(session_raw_dir(session.id)) as writer:
            intents.bind_session(db, session.id, room.id, intent.generation, writer.identity)
        old_generation = intent.generation
    rooms.update_room(room.id, {"auto_record": False, "auto_analyze": expected[0], "auto_render": expected[1]})
    manager = rooms.RecorderManager()
    monitor = live_monitor.LiveMonitor()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    monkeypatch.setattr(live_monitor, "live_monitor", monitor)
    runtime.stream_gate = asyncio.Event()
    runtime.stream_failure = SourceTemporaryError("controlled stream outage")
    try:
        assert recover_pending_recordings(rooms.finalize_recording_requests) == [session.id]
        assert await rooms.auto_recover_interrupted_sessions() == [room.id]
        assert_current_settings(manager, room.id, expected)
        with get_session() as db:
            current = db.get(LiveRoom, room.id)
            accepted = intents.load_intent(db, room.id)
            assert not current.auto_record
            assert accepted.generation != old_generation
            assert accepted.origin == "manual" and accepted.crashes == 1
    finally:
        runtime.stream_gate.set()
        await manager.stop_all()
        await monitor.stop()
