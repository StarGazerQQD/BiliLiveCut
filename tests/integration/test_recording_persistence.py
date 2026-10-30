from __future__ import annotations

import asyncio
import csv
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from app.core.paths import session_raw_dir
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSchedule, RecordingSession
from app.db.session import get_session
from app.recording import intents
from app.recording.recovery import recover_pending_recordings
from app.recording.writer_ownership import SessionWriter
from app.sources.rooms import register_room, room_source
from app.web.services import rooms
from tests.integration.test_recording_progress import video as video
from tests.integration.test_source_runtime import RuntimeSource
from tests.integration.test_source_runtime import runtime as runtime


async def seed_execution() -> tuple[int, int, str, SessionWriter]:
    room = await register_room("123", True, "external")
    with get_session() as db:
        intent = intents.accept(db, room.id, room_source(room), pipeline=False, produce=False, origin="manual")
        session = RecordingSession(room_id=room.id, status="recording")
        db.add(session)
        db.flush()
        writer = SessionWriter.create(session_raw_dir(session.id))
        intents.bind_session(db, session.id, room.id, intent.generation, writer.identity)
        return room.id, session.id, intent.generation, writer


async def test_settlement_requires_last_writer_exit_and_preserves_only_adopted_media(
    runtime: RuntimeSource,
    video: Path,
) -> None:
    room_id, session_id, generation, writer = await seed_execution()
    directory = session_raw_dir(session_id)
    prefix = "part000_0123456789ab_"
    good, tail = [directory / f"{prefix}{index:05d}.ts" for index in range(2)]
    for path in (good, tail, directory / "part000_abcdef123456_00000.ts"):
        path.write_bytes(video.read_bytes())
    damaged = directory / f"{prefix}00002.ts"
    damaged.write_bytes(b"not valid media")
    listing = directory / f"{prefix}segments.csv"
    with listing.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow([good.name, 0, 1])
        handle.write(f'"{tail.name},0,')
    with get_session() as db:
        intents.adopt_attempt(db, session_id, directory, prefix, listing)
    try:
        with pytest.raises(OSError):
            recover_pending_recordings(rooms.finalize_recording_requests)
        with get_session() as db:
            assert not db.exec(select(RawSegment)).all()
            assert db.get(RecordingSession, session_id).status == "recording"
    finally:
        writer.close()
    assert recover_pending_recordings(rooms.finalize_recording_requests) == [session_id]
    with get_session() as db:
        segments = db.exec(select(RawSegment).order_by(RawSegment.seq)).all()
        assert [segment.file_path for segment in segments] == [str(good), str(tail)]
        assert [segment.seq for segment in segments] == [0, 1]
        assert all(segment.duration_s > 0 for segment in segments)
        assert db.get(RecordingSession, session_id).status == "interrupted"
        execution = intents.load_execution(db, session_id)
        assert execution.settled and len(execution.recovery_errors) == 2
        requests = {row.key: row.value for row in db.exec(select(AppSetting)).all()}
        assert intents.load_intent(db, room_id).generation == generation
    assert recover_pending_recordings(rooms.finalize_recording_requests) == []
    with get_session() as db:
        assert {row.key: row.value for row in db.exec(select(AppSetting)).all()} == requests
        assert len(db.exec(select(RawSegment)).all()) == 2
    assert damaged.is_file()


async def test_recovery_budget_survives_restarts_and_explicit_start_resets_it(runtime: RuntimeSource) -> None:
    room_id, session_id, generation, writer = await seed_execution()
    writer.close()
    recover_pending_recordings(rooms.finalize_recording_requests)
    for attempt in range(1, intents.MAX_CRASH_RECOVERIES + 2):
        with get_session() as db:
            intent = intents.prepare_restarts(db)[0]
            assert intent.crashes == attempt
            assert intents.prepare_restarts(db)[0].crashes == attempt
            if attempt <= intents.MAX_CRASH_RECOVERIES:
                assert intent.state == "cooldown"
                intents.accept(
                    db,
                    room_id,
                    intent.source,
                    pipeline=False,
                    produce=False,
                    origin="manual",
                    previous_generation=intent.generation,
                )
            else:
                assert intent.state == "blocked"
                fresh = intents.accept(db, room_id, intent.source, pipeline=False, produce=False, origin="manual")
                assert fresh.crashes == 0 and fresh.generation != generation


async def test_manual_stop_before_session_creation_revokes_accepted_request(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = await register_room("123", True, "external")
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    await manager.start(room.id, pipeline=False)
    with get_session() as db:
        accepted = intents.load_intent(db, room.id)
        assert accepted.state == "accepted" and accepted.session_id is None
    await manager.stop(room.id)
    with get_session() as db:
        assert intents.load_intent(db, room.id).state == "stopped"
        assert not intents.prepare_restarts(db)
    assert await rooms.auto_recover_interrupted_sessions() == []
    assert runtime.stream_calls == 0


async def test_old_finalizer_cannot_overwrite_later_manual_intent(runtime: RuntimeSource) -> None:
    room_id, session_id, generation, writer = await seed_execution()
    writer.close()
    with get_session() as db:
        old = intents.load_intent(db, room_id)
        intents.stop_intent(db, room_id, "用户停止")
        current = intents.accept(db, room_id, old.source, pipeline=False, produce=False, origin="manual")
        intents.finish(
            db, session_id, reason="old failure", blocked=True, cooldown=False, shutdown=False, cooldown_seconds=1
        )
        assert intents.load_intent(db, room_id) == current
        assert current.generation != generation


async def test_real_recorder_process_crash_leaves_recoverable_ffmpeg_tail(
    runtime: RuntimeSource, tmp_path: Path
) -> None:
    room = await register_room("123", True, "external")
    root = Path(__file__).resolve().parents[2]
    actor = Path(__file__).with_name("recording_crash_actor.py")
    log = tmp_path / "crash.log"
    with log.open("wb") as output:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(actor), str(room.id)],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root)},
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
    assert result.returncode == 31, log.read_text(encoding="utf-8", errors="replace")
    with get_session() as db:
        execution = intents.pending_executions(db)[0]
        assert execution.attempts and not execution.finalized
    # 实际父死亡清理异步完成；未确认 writer 退出时绝不进入清算。
    deadline = time.monotonic() + 10
    while True:
        try:
            with SessionWriter.recover(execution.writer):
                break
        except OSError:
            assert time.monotonic() < deadline, "所属 FFmpeg 未随宿主退出"
            await asyncio.sleep(0.05)
    recover_pending_recordings(rooms.finalize_recording_requests)
    with get_session() as db:
        segments = db.exec(select(RawSegment).where(RawSegment.session_id == execution.session_id)).all()
        assert segments and all(segment.duration_s > 0 and segment.size_bytes > 0 for segment in segments)
        assert intents.load_execution(db, execution.session_id).settled
        assert db.get(RecordingSession, execution.session_id).ended_at is not None
        before = len(segments)
    assert not recover_pending_recordings(rooms.finalize_recording_requests)
    with get_session() as db:
        assert len(db.exec(select(RawSegment)).all()) == before


async def test_accepted_request_is_blocked_when_writer_directory_cannot_be_created(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.paths import storage_root
    from app.pipeline.live_monitor import LiveMonitor

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.auto_record = True
        db.add(saved)
    obstacle = storage_root() / ".recording_writers"
    obstacle.write_bytes(b"controlled filesystem obstacle")
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    assert await manager.start(room.id, pipeline=False)
    task = manager.recording_token(room.id)
    assert isinstance(task, asyncio.Task)
    await task
    with get_session() as db:
        intent = intents.load_intent(db, room.id)
        assert intent.state == "blocked" and intent.session_id is None
        assert "FileExistsError" in intent.reason
        generation = intent.generation
        saved = db.get(LiveRoom, room.id)
    monitor = LiveMonitor()
    try:
        await monitor._check_room(saved)
    finally:
        await monitor.stop()
    with get_session() as db:
        assert intents.load_intent(db, room.id).generation == generation
        assert not db.exec(select(RecordingSession)).all()
    assert manager.status(room.id)["state"] == "error" and runtime.stream_calls == 0
    assert obstacle.read_bytes() == b"controlled filesystem obstacle"


async def test_unexpected_database_failure_has_one_error_terminal_before_end_callback(runtime: RuntimeSource) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    from app.db import session as db_module
    from app.recording.recorder import Recorder

    room = await register_room("123", True, "external")
    failed = False
    observed: list[tuple[str, str]] = []

    def fail_selection(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal failed
        if (
            not failed
            and statement.startswith("INSERT INTO app_settings")
            and isinstance(parameters, tuple)
            and any(isinstance(value, str) and value.startswith("session_stream:") for value in parameters)
        ):
            failed = True
            raise SQLAlchemyError("controlled recording selection write failure")

    async def ended(session_id: int) -> None:
        await rooms._on_session_end(session_id)
        with get_session() as db:
            observed.append((db.get(RecordingSession, session_id).status, intents.load_intent(db, room.id).state))

    recorder = Recorder(room_source(room), room.id, on_end=ended, metadata_prepared=True)
    event.listen(db_module.engine, "before_cursor_execute", fail_selection)
    try:
        with pytest.raises(SQLAlchemyError, match="selection write failure"):
            await recorder.run()
    finally:
        event.remove(db_module.engine, "before_cursor_execute", fail_selection)
    assert failed and observed == [("error", "blocked")]
    with get_session() as db:
        assert intents.load_execution(db, recorder.session_id).settled
        assert intents.load_intent(db, room.id).state == "blocked"


@pytest.mark.parametrize("permanent_error", [False, True])
async def test_service_shutdown_preserves_request_or_confirmed_error(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    permanent_error: bool,
) -> None:
    from app.plugins.live_source import SourceAuthenticationError

    room = await register_room("123", True, "external")
    runtime.stream_gate = asyncio.Event()
    if permanent_error:
        runtime.stream_failure = SourceAuthenticationError("controlled expired credential")
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    assert await manager.start(room.id, pipeline=False)
    while not runtime.stream_calls:
        await asyncio.sleep(0.01)
    closing = asyncio.create_task(manager.stop_all())
    try:
        await asyncio.sleep(0)
        runtime.stream_gate.set()
        await asyncio.wait_for(closing, 5)
    finally:
        runtime.stream_gate.set()
        await manager.stop_all()
    with get_session() as db:
        intent = intents.load_intent(db, room.id)
        assert intent.state == ("blocked" if permanent_error else "cooldown")
        assert intent.resume_requested is not permanent_error
        assert db.get(RecordingSession, intent.session_id).status == ("error" if permanent_error else "stopped")
        assert intent.crashes == 0
    assert manager.status(room.id)["state"] == ("error" if permanent_error else "stopped")


async def test_schedule_acceptance_commits_intent_and_one_successor_together(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.recording.schedules import complete_schedule_occurrence

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.schedule_enabled = True
        db.add(saved)
        schedule = RecordingSchedule(
            room_id=room.id, scheduled_at=datetime.now(UTC) - timedelta(seconds=1), recurrent="daily"
        )
        db.add(schedule)
        db.flush()
        schedule_id = schedule.id
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    try:
        assert await manager.start(room.id, pipeline=False, schedule_id=schedule_id)
        with get_session() as db:
            intent = intents.load_intent(db, room.id)
            assert intent.origin == "schedule" and intent.schedule_id == schedule_id
            assert intent.state == "accepted" and intent.session_id is None
            assert db.get(RecordingSchedule, schedule_id).triggered
            assert len(db.exec(select(RecordingSchedule)).all()) == 2
        complete_schedule_occurrence(schedule_id)
        with get_session() as db:
            assert len(db.exec(select(RecordingSchedule)).all()) == 2
    finally:
        await manager.stop(room.id)
    assert runtime.stream_calls == 0


@pytest.mark.parametrize("deleted", [False, True])
async def test_schedule_revoked_during_metadata_does_not_start_or_notify(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    deleted: bool,
) -> None:
    from app.recording.schedule_runner import ScheduleRunner

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.schedule_enabled = True
        db.add(saved)
        schedule = RecordingSchedule(room_id=room.id, scheduled_at=datetime.now(UTC) - timedelta(seconds=1))
        db.add(schedule)
        db.flush()
        schedule_id = schedule.id
    runtime.requested.clear()
    runtime.info_gate = asyncio.Event()
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    notices: list[str] = []
    runner = ScheduleRunner(manager, notices.append)
    pending = asyncio.create_task(runner.run_due())
    try:
        await asyncio.wait_for(runtime.requested.wait(), 3)
        with get_session() as db:
            schedule = db.get(RecordingSchedule, schedule_id)
            if deleted:
                db.delete(schedule)
            else:
                schedule.enabled = False
                db.add(schedule)
        runtime.info_gate.set()
        await asyncio.wait_for(pending, 5)
        assert not notices and not manager.is_running(room.id)
        with get_session() as db:
            assert intents.load_intent(db, room.id) is None
            if not deleted:
                assert not db.get(RecordingSchedule, schedule_id).triggered
    finally:
        runtime.info_gate.set()
        await manager.stop_all()


async def test_unknown_live_status_keeps_manual_crash_recovery_delay_across_restarts(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.pipeline import live_monitor as module
    from app.plugins.live_source import LiveStatus

    room_id, session_id, generation, writer = await seed_execution()
    writer.close()
    recover_pending_recordings(rooms.finalize_recording_requests)
    runtime.status = LiveStatus.UNKNOWN
    manager = rooms.RecorderManager()
    monitor = module.LiveMonitor()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    monkeypatch.setattr(module, "live_monitor", monitor)
    try:
        assert not await rooms.auto_recover_interrupted_sessions()
        with get_session() as db:
            waiting = intents.load_intent(db, room_id)
            assert waiting.state == "cooldown" and waiting.resume_requested and waiting.crashes == 1
            assert waiting.ready_at > datetime.now(UTC)
        assert not await rooms.auto_recover_interrupted_sessions()
        with get_session() as db:
            assert intents.load_intent(db, room_id) == waiting
    finally:
        await monitor.stop()
        await manager.stop_all()
    assert runtime.stream_calls == 0


async def test_backend_becomes_ready_only_after_old_writer_exits_and_session_is_settled(
    runtime: RuntimeSource,
    tmp_path: Path,
) -> None:
    import socket
    from urllib.error import URLError
    from urllib.request import ProxyHandler, build_opener

    room_id, session_id, _generation, writer = await seed_execution()
    with get_session() as db:
        intents.stop_intent(db, room_id, "用户停止后进程异常中断")
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "PYTHONIOENCODING": "utf-8",
        "ADMIN_PASSWORD": "",
        "DOCKER_AUTH_ENABLED": "false",
        "BILIBILI_COOKIE": "",
        "ASR_NO_MODEL_DOWNLOAD": "1",
        "ASR_PRELOAD_ON_START": "false",
        "TREND_ENABLED": "false",
        "TREND_SCHEDULE_ENABLED": "false",
        "_BLC_PARENT_STDIN": "0",
    }
    command = [sys.executable, "-m", "app.cli", "backend", "--port", str(port)]
    try:
        failed = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert failed.returncode != 0
        assert "Web 后台已启动" not in failed.stdout + failed.stderr
        with get_session() as db:
            assert not intents.load_execution(db, session_id).settled
            assert db.get(RecordingSession, session_id).ended_at is None
    finally:
        writer.close()
    opener = build_opener(ProxyHandler({}))
    with (tmp_path / "recovered-backend.log").open("w+", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            cwd=tmp_path,
            env={**env, "_BLC_PARENT_STDIN": "1"},
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=output,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/auth/status", timeout=1) as response:
                        assert response.status == 200
                        break
                except (OSError, URLError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        output.seek(0)
                        pytest.fail(output.read())
                    await asyncio.sleep(0.1)
            with get_session() as db:
                assert intents.load_execution(db, session_id).settled
                session = db.get(RecordingSession, session_id)
                assert session.status == "stopped" and session.ended_at is not None
            process.stdin.close()
            assert await asyncio.to_thread(process.wait, timeout=20) == 0
        finally:
            if process.poll() is None:
                process.stdin.close()
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=5)
