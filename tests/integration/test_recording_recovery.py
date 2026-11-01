from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.analysis.room_config import load_room_config
from app.core.config import settings
from app.db.entities import LiveRoom, RecordingSession
from app.db.session import get_session
from app.pipeline.live_monitor import LiveMonitor
from app.plugins.live_source import LiveStatus, SourceAuthenticationError, SourceTemporaryError
from app.recording.recorder import Recorder
from app.sources.rooms import register_room, room_source
from app.web.services import rooms
from tests.integration.test_source_runtime import RuntimeSource
from tests.integration.test_source_runtime import runtime as runtime


async def _failed_recording(
    source: RuntimeSource, monkeypatch: pytest.MonkeyPatch
) -> tuple[LiveRoom, rooms.RecorderManager, rooms.RecordingRecovery, list[float]]:
    room = await register_room("123", True, "external")
    with get_session() as db:
        stored = db.get(LiveRoom, room.id)
        assert stored is not None
        stored.auto_record = True
        stored.auto_analyze = False
        db.add(stored)
    now = [100.0]
    monkeypatch.setattr(rooms, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 1)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 0)
    monkeypatch.setattr(rooms, "open_path", lambda _path: False)
    source.stream_failure = SourceTemporaryError("controlled stream outage")
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    await manager.start(room.id, pipeline=False)
    token = manager.recording_token(room.id)
    assert isinstance(token, asyncio.Task)
    await asyncio.wait_for(token, 5)
    recovery = manager.recovery(room.id)
    assert recovery is not None and recovery.remaining() > 0
    with get_session() as db:
        session = db.get(RecordingSession, recovery.session_id)
        assert session is not None and session.status == "error" and session.ended_at is not None
        assert not load_room_config(db.get(LiveRoom, room.id))["recording_wait_for_next_live"]
    return room, manager, recovery, now


async def test_first_stream_lookup_uses_remaining_episode_deadline(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    room = await register_room("123", True, "external")
    runtime.stream_gate = asyncio.Event()
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 0.1)
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    await asyncio.wait_for(recorder.run(), 3)
    assert runtime.stream_calls == 1
    assert recorder.retry_budget_exhausted and not recorder.recording_action_required
    assert "取流超过连续重连剩余期限" in recorder.recording_error
    with get_session() as db:
        saved = db.get(RecordingSession, recorder.session_id)
        assert saved is not None and saved.status == "error"


@pytest.mark.parametrize("failure", ["unknown", "temporary", "authentication"])
async def test_polling_cooldown_distinguishes_transient_and_permanent_failures(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    room, manager, recovery, now = await _failed_recording(runtime, monkeypatch)
    monitor = LiveMonitor()
    try:
        before = runtime.info_calls
        await monitor._check_room(room)
        assert runtime.info_calls == before and not manager.is_running(room.id)
        now[0] = recovery.ready_at
        if failure == "unknown":
            runtime.status = LiveStatus.UNKNOWN
        else:
            runtime.failure = (
                SourceTemporaryError("temporary info failure")
                if failure == "temporary"
                else SourceAuthenticationError("credential-secret")
            )
        await monitor._check_room(room)
        assert not manager.is_running(room.id)
        if failure == "authentication":
            assert manager.recovery(room.id) is None
            with get_session() as db:
                saved = db.get(LiveRoom, room.id)
                assert load_room_config(saved)["recording_auto_restart_suppressed"]
                assert monitor.room_status(saved, running=False, runtime_state="error")["state"] == "action_required"
            assert "secret" not in manager.status(room.id)["message"]
            return
        assert manager.recovery(room.id) is recovery and recovery.remaining() > 0
        now[0] = recovery.ready_at
        runtime.failure = None
        runtime.status = LiveStatus.LIVE
        monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 1000)
        await monitor._check_room(room)
        assert manager.is_running(room.id) and manager.recovery(room.id) is None
        with get_session() as db:
            previous = db.get(RecordingSession, recovery.session_id)
            assert previous is not None and previous.status == "error"
    finally:
        await manager.stop_all()
        await monitor.stop()


@pytest.mark.parametrize("paused", [False, True])
async def test_stopping_cooldown_preserves_completed_failure(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, paused: bool
) -> None:
    room, manager, recovery, now = await _failed_recording(runtime, monkeypatch)
    with get_session() as db:
        previous = db.get(RecordingSession, recovery.session_id)
        assert previous is not None
        ended_at, message = previous.ended_at, previous.error_message
    await manager.stop(room.id, pause_auto_restart=True, mark_paused=paused)
    assert manager.recovery(room.id) is None
    now[0] = recovery.ready_at + 60
    monitor = LiveMonitor()
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        previous = db.get(RecordingSession, recovery.session_id)
        assert previous is not None and previous.status == "error"
        assert previous.ended_at == ended_at and previous.error_message == message
        assert load_room_config(saved)["recording_auto_restart_suppressed"]
        assert monitor.room_status(saved, running=False, runtime_state=manager.status(room.id)["state"])["state"] == (
            "manual_paused" if paused else "stopped"
        )
    await monitor._check_room(saved)
    assert not manager.is_running(room.id)


async def test_recovery_cancelled_during_metadata_does_not_start_new_session(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    room, manager, recovery, now = await _failed_recording(runtime, monkeypatch)
    now[0] = recovery.ready_at
    runtime.info_gate = asyncio.Event()
    runtime.requested.clear()
    starting = asyncio.create_task(manager.start(room.id, pipeline=False, automatic=True, recovery=recovery))
    try:
        await asyncio.wait_for(runtime.requested.wait(), 2)
        manager.clear_recovery(room.id, recovery)
        runtime.info_gate.set()
        await asyncio.wait_for(starting, 2)
        assert not manager.is_running(room.id)
        with get_session() as db:
            sessions = db.exec(select(RecordingSession).where(RecordingSession.room_id == room.id)).all()
            assert len(sessions) == 1 and sessions[0].status == "error"
    finally:
        runtime.info_gate.set()
        await asyncio.gather(starting, return_exceptions=True)
        await manager.stop_all()
