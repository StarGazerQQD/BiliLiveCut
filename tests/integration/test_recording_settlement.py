from __future__ import annotations

import asyncio
import csv
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.analysis.reanalysis import process_pending_session_reanalyses, queue_session_reanalysis
from app.analysis.session_summary import _session_ready
from app.core.config import settings
from app.core.paths import resolve_storage_path, session_raw_dir
from app.db.entities import AppSetting, Danmaku, LiveRoom, RawSegment, RecordingSession
from app.db.session import get_session
from app.plugins.live_source import SourceUnavailable
from app.recording import intents, settlement as settlement_module
from app.recording.danmaku import CaptureInterval, DanmakuEvidence, read_evidence
from app.recording.danmaku_journal import DanmakuJournal
from app.recording.recorder import Recorder
from app.recording.settlement import RecordingSettlement
from app.recording.writer_ownership import SessionWriter
from app.sources.registry import source_registry
from app.sources.rooms import room_source
from app.web.services import rooms
from tests.integration.test_recording_persistence import seed_execution
from tests.integration.test_recording_progress import video as video
from tests.integration.test_source_runtime import RuntimeSource
from tests.integration.test_source_runtime import runtime as runtime


def mark_finalized(session_id: int, *, error: str = "original recording fault") -> None:
    with get_session() as db:
        execution = intents.load_execution(db, session_id)
        execution.finalized = True
        intents.save(db, execution)
        recording = db.get(RecordingSession, session_id)
        recording.status = "error"
        recording.error_message = error
        recording.ended_at = datetime.now(UTC)
        db.add(recording)


async def test_failed_end_transaction_is_retried_without_restart_or_changing_new_generation(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.session import engine

    monkeypatch.setattr(settlement_module, "_POLL_S", 0.01)
    opened: list[str] = []
    monkeypatch.setattr(rooms, "open_path", lambda path: opened.append(path))
    service = RecordingSettlement(rooms.finalize_recording_requests)
    await service.start()
    room_id, session_id, _, writer = await seed_execution()
    later_writer: SessionWriter | None = None
    faults = 0

    def fail_once(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal faults
        if context.isinsert and f"session_reanalysis:{session_id}" in str(parameters) and faults == 0:
            faults += 1
            raise OperationalError(statement, parameters, sqlite3.OperationalError("database is locked"))

    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        room.auto_analyze = True
        db.add(room)
        db.add(RawSegment(session_id=session_id, seq=0, file_path="isolated.ts", duration_s=10))
        identity = room_source(room)
    recorder = Recorder(identity, room_id, on_end=rooms._on_session_end)
    recorder._session_id = session_id
    recorder._recording_error = "original recording fault"
    event.listen(engine, "before_cursor_execute", fail_once)
    try:
        await recorder._finalize_session()
        assert faults == 1
        with get_session() as db:
            assert not intents.load_execution(db, session_id).settled
            assert db.get(AppSetting, f"session_reanalysis:{session_id}") is None
            assert db.get(AppSetting, f"session_timeline_summary_request:{session_id}") is None
            current = intents.accept(db, room_id, identity, pipeline=False, produce=False, origin="manual")
            later = RecordingSession(room_id=room_id, status="recording")
            db.add(later)
            db.flush()
            later_writer = SessionWriter.create(session_raw_dir(later.id))
            intents.bind_session(db, later.id, room_id, current.generation, later_writer.identity)
            room = db.get(LiveRoom, room_id)
            room.enabled = True
            db.add(room)
            generation = current.generation
        writer.close()
        async with asyncio.timeout(5):
            while True:
                with get_session() as db:
                    if intents.load_execution(db, session_id).settled:
                        break
                await asyncio.sleep(0.01)
        await service.stop()
        with get_session() as db:
            recording = db.get(RecordingSession, session_id)
            assert recording.status == "error" and recording.error_message == "original recording fault"
            assert db.get(AppSetting, f"session_reanalysis:{session_id}") is not None
            assert db.get(LiveRoom, room_id).enabled
            assert intents.load_intent(db, room_id).generation == generation
            snapshot = {row.key: row.value for row in db.exec(select(AppSetting)).all()}
        assert RecordingSettlement(rooms.finalize_recording_requests).scan() == []
        with get_session() as db:
            assert {row.key: row.value for row in db.exec(select(AppSetting)).all()} == snapshot
        assert opened == []
    finally:
        event.remove(engine, "before_cursor_execute", fail_once)
        writer.close()
        if later_writer is not None:
            later_writer.close()
        await service.stop()


async def test_periodic_pages_skip_live_and_broken_writer_without_starving_later_session(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settlement_module, "_PAGE_SIZE", 2)
    records = [await seed_execution() for _ in range(3)]
    for _, session_id, _, _ in records:
        mark_finalized(session_id)
    records[1][3].close()
    resolve_storage_path(records[1][3].identity.path).unlink()
    records[2][3].close()
    service = RecordingSettlement(rooms.finalize_recording_requests)
    try:
        assert service.scan() == []
        assert service.scan() == [records[2][1]]
        with get_session() as db:
            assert not intents.load_execution(db, records[0][1]).settled
            assert not intents.load_execution(db, records[1][1]).settled
            assert intents.load_execution(db, records[2][1]).settled
    finally:
        for *_, writer in records:
            writer.close()


async def test_final_analysis_waits_for_all_journal_batches_and_preserves_normal_end(runtime: RuntimeSource) -> None:
    room_id, session_id, _, writer = await seed_execution()
    writer.close()
    mark_finalized(session_id)
    now = datetime.now(UTC)
    journal = DanmakuJournal(session_id)
    journal.append_observation(DanmakuEvidence(status="available", intervals=[CaptureInterval(start=now)]))
    journal.append_messages(
        [Danmaku(session_id=session_id, room_id=room_id, ts=now, content=str(i)) for i in range(205)]
    )
    journal.append_observation(
        DanmakuEvidence(
            status="available",
            intervals=[CaptureInterval(start=now, end=now)],
            confirmed_until=now,
            ended_at=now,
        )
    )
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        room.auto_analyze = True
        db.add(room)
        db.add(RawSegment(session_id=session_id, seq=0, file_path="isolated.ts", duration_s=10))
    service = RecordingSettlement(rooms.finalize_recording_requests)
    for expected in (100, 200):
        assert service.scan() == []
        with get_session() as db:
            assert len(db.exec(select(Danmaku)).all()) == expected
            assert not intents.load_execution(db, session_id).settled
            assert not _session_ready(db, session_id)
            assert db.get(AppSetting, f"session_reanalysis:{session_id}") is None
        assert process_pending_session_reanalyses() == []
        assert queue_session_reanalysis(session_id, reason="explicit-test").skipped_active == 1
    assert service.scan() == [session_id]
    with get_session() as db:
        assert len(db.exec(select(Danmaku)).all()) == 205
        assert intents.load_execution(db, session_id).settled
        assert db.get(AppSetting, f"session_reanalysis:{session_id}") is not None
        assert read_evidence(db, session_id).ended_at == now
        assert not read_evidence(db, session_id).interrupted


async def test_periodic_settlement_respects_persisted_no_pipeline_policy(runtime: RuntimeSource) -> None:
    room_id, session_id, _, writer = await seed_execution()
    writer.close()
    mark_finalized(session_id)
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        room.auto_analyze = True
        db.add(room)
        record = intents.load_execution(db, session_id)
        record.final_analysis = False
        intents.save(db, record)
        db.add(RawSegment(session_id=session_id, seq=0, file_path="isolated.ts", duration_s=10))
    assert RecordingSettlement(rooms.finalize_recording_requests).scan() == [session_id]
    with get_session() as db:
        assert intents.load_execution(db, session_id).settled
        assert db.get(AppSetting, f"session_reanalysis:{session_id}") is None
        assert db.get(AppSetting, f"session_timeline_summary_request:{session_id}") is None


async def test_terminal_database_outage_is_recovered_after_writer_exit(runtime: RuntimeSource) -> None:
    from app.db.session import engine

    room_id, session_id, _, writer = await seed_execution()
    with get_session() as db:
        identity = room_source(db.get(LiveRoom, room_id))
    recorder = Recorder(identity, room_id, on_end=rooms._on_session_end)
    recorder._session_id = session_id

    def fail_terminal(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if context.isupdate and "recording_sessions" in statement:
            raise OperationalError(statement, parameters, sqlite3.OperationalError("database is locked"))

    event.listen(engine, "before_cursor_execute", fail_terminal)
    try:
        with pytest.raises(OperationalError):
            await recorder._finalize_session()
    finally:
        event.remove(engine, "before_cursor_execute", fail_terminal)
        writer.close()
    with get_session() as db:
        assert not intents.load_execution(db, session_id).finalized
        assert intents.load_intent(db, room_id).state == "running"
    assert RecordingSettlement(rooms.finalize_recording_requests).scan() == [session_id]
    with get_session() as db:
        assert intents.load_execution(db, session_id).settled
        assert db.get(RecordingSession, session_id).status == "interrupted"
        assert intents.load_intent(db, room_id).state == "blocked"
        assert intents.prepare_restarts(db)[0].crashes == 0
        room = db.get(LiveRoom, room_id)
        room.auto_record = True
        db.add(room)
    manager = rooms.RecorderManager()
    assert not await manager.start(room_id, automatic=True)
    assert not manager.is_running(room_id)
    runtime.stream_failure = SourceUnavailable("测试外部来源停止提供媒体")
    assert await manager.start(room_id, automatic=False, pipeline=False)
    await manager.stop(room_id)
    with get_session() as db:
        assert intents.load_intent(db, room_id).state != "blocked"


@pytest.mark.parametrize("finalized", [False, True])
async def test_tail_registration_is_bounded_and_required_even_after_terminal_status(
    runtime: RuntimeSource,
    video: Path,
    finalized: bool,
) -> None:
    _, session_id, _, writer = await seed_execution()
    directory = session_raw_dir(session_id)
    prefix = "part000_0123456789ab_"
    listing = directory / f"{prefix}segments.csv"
    media = video.read_bytes()
    with listing.open("w", encoding="utf-8", newline="") as stream:
        rows = csv.writer(stream)
        for index in range(21):
            path = directory / f"{prefix}{index:05d}.ts"
            path.write_bytes(media)
            rows.writerow([path.name, index, index + 1])
    with get_session() as db:
        intents.adopt_attempt(db, session_id, directory, prefix, listing)
    writer.close()
    if finalized:
        mark_finalized(session_id)
    service = RecordingSettlement(rooms.finalize_recording_requests)
    assert service.scan() == []
    with get_session() as db:
        assert len(db.exec(select(RawSegment)).all()) == 20
        assert not intents.load_execution(db, session_id).settled
        assert intents.load_execution(db, session_id).recovery_after.endswith("00019.ts")
    assert service.scan() == [session_id]
    with get_session() as db:
        assert len(db.exec(select(RawSegment)).all()) == 21
        assert intents.load_execution(db, session_id).settled
        assert intents.load_execution(db, session_id).recovery_after == ""


def finished_media_boundary(
    monkeypatch: pytest.MonkeyPatch,
    runtime: RuntimeSource,
    video: Path,
    room_id: int,
    old_session_id: int | None = None,
) -> list[asyncio.subprocess.Process]:
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def launch(*args: str, **options: object) -> asyncio.subprocess.Process:
        with get_session() as db:
            assert db.get(LiveRoom, room_id).enabled
            if old_session_id is not None:
                assert intents.load_execution(db, old_session_id).settled
        command = [
            sys.executable,
            "-c",
            "import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2].replace('%05d','00000'))",
            str(video),
            args[-1],
        ]
        if os.name != "nt":
            command = [*args[:7], sys.executable, *command]
        process = await original(*command, **options)
        processes.append(process)
        runtime.stream_failure = SourceUnavailable("测试外部来源已结束")
        return process

    monkeypatch.setattr(settings, "recording_stream_race_parallel", 1)
    monkeypatch.setattr(settings, "reconnect_max_backoff_s", 0.01)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    return processes


def test_cli_restores_old_session_before_activating_new_recording(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    video: Path,
) -> None:
    from click.testing import CliRunner

    from app import cli
    from app.plugins.manager import PluginManager

    room_id, session_id, _, writer = asyncio.run(seed_execution())
    writer.close()
    manager = PluginManager(tmp_path / "plugins", registry=source_registry)
    monkeypatch.setattr("app.plugins.runtime.plugin_manager", manager)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr("app.core.osutil.open_path", lambda path: None)
    processes = finished_media_boundary(monkeypatch, runtime, video, room_id, session_id)
    result = CliRunner().invoke(cli.app, ["record", str(room_id), "--no-pipeline"])
    assert result.exit_code == 0, result.output
    assert len(processes) == 1 and all(process.returncode is not None for process in processes)
    with get_session() as db:
        sessions = db.exec(select(RecordingSession).order_by(RecordingSession.id)).all()
        assert len(sessions) == 2
        current = sessions[-1]
        assert intents.load_execution(db, current.id).settled
        assert not intents.load_execution(db, current.id).final_analysis
        assert len(db.exec(select(RawSegment).where(RawSegment.session_id == current.id)).all()) == 1
        assert db.get(AppSetting, f"session_reanalysis:{current.id}") is None
        assert not db.get(LiveRoom, room_id).enabled


async def test_orphan_registration_sql_failure_defers_final_analysis_until_recovered(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    video: Path,
) -> None:
    from app.db.session import engine
    from app.sources.rooms import register_room

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.enabled = saved.auto_analyze = True
        db.add(saved)
    processes = finished_media_boundary(monkeypatch, runtime, video, room.id)

    def fail_segment(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if context.isinsert and "raw_segments" in statement:
            raise OperationalError(statement, parameters, sqlite3.OperationalError("database is locked"))

    recorder = Recorder(room_source(room), room.id, on_end=rooms._on_session_end)
    event.listen(engine, "before_cursor_execute", fail_segment)
    try:
        with pytest.raises(OperationalError):
            await asyncio.wait_for(recorder.run(), 15)
    finally:
        event.remove(engine, "before_cursor_execute", fail_segment)
    assert len(processes) == 1 and all(process.returncode is not None for process in processes)
    with get_session() as db:
        assert recorder._tail_registration_pending
        assert intents.load_execution(db, recorder.session_id).finalized
        assert not intents.load_execution(db, recorder.session_id).settled
        assert not db.exec(select(RawSegment)).all()
        assert db.get(AppSetting, f"session_reanalysis:{recorder.session_id}") is None
        before = db.get(RecordingSession, recorder.session_id).model_dump()
    assert RecordingSettlement(rooms.finalize_recording_requests).scan() == [recorder.session_id]
    with get_session() as db:
        assert intents.load_execution(db, recorder.session_id).settled
        assert len(db.exec(select(RawSegment)).all()) == 1
        assert db.get(AppSetting, f"session_reanalysis:{recorder.session_id}") is not None
        current = db.get(RecordingSession, recorder.session_id)
        assert current.status == before["status"] and current.error_message == before["error_message"]
