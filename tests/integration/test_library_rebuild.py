from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import URL
from sqlmodel import Session, create_engine

from app.cli import app
from app.core.recording_storage import RecordingStorage
from app.db.entities import Danmaku, LiveRoom, RawSegment, RecordingSession, Transcript
from app.db.schema import create_schema
from app.recording.library_rebuild import LibraryRebuild, LibrarySource, rebuild_library
from app.recording.session_storage import LEGACY_FILE_PREFIX, LOCATION_PREFIX


def _source(storage: Path, version: int, name: str) -> LibrarySource:
    media = storage / "raw" / "session_1" / "part.ts"
    media.parent.mkdir(parents=True)
    media.write_bytes(name.encode())
    database = storage / "blc.db"
    engine = create_engine(URL.create("sqlite", database=str(database)))
    try:
        with Session(engine) as db:
            create_schema(db)
            db.add(LiveRoom(id=1, input_url=name, platform="local", uploader_name=name))
            db.add(RecordingSession(id=1, room_id=1))
            db.add(RawSegment(id=1, session_id=1, seq=0, file_path="raw/session_1/part.ts"))
            db.add(Transcript(id=1, segment_id=1, final_text=name))
            db.add(Danmaku(id=1, session_id=1, room_id=1, content=name))
            db.commit()
    finally:
        engine.dispose()
    if version == 5:
        with closing(sqlite3.connect(database)) as conn, conn:
            old_root = storage.parent / "original-before-move" / "storage"
            conn.execute("UPDATE raw_segments SET file_path=?", (str(old_root / "raw/session_1/part.ts"),))
            conn.execute("ALTER TABLE recording_sessions ADD COLUMN stream_url VARCHAR")
            conn.execute("ALTER TABLE recording_sessions ADD COLUMN quality INTEGER")
            conn.execute("ALTER TABLE clip_variants ADD COLUMN backup_path VARCHAR")
            conn.execute("UPDATE schema_meta SET schema_version=5")
    return LibrarySource(database=database, storage=storage, original_storage=old_root if version == 5 else None)


def test_rebuild_maps_overlapping_ids_and_keeps_original_media_and_databases(tmp_path: Path) -> None:
    sources = [_source(tmp_path / name, ver, name) for name, ver in (("new", 6), ("old", 5))]
    original = {p: hashlib.sha256(p.read_bytes()).digest() for p in tmp_path.rglob("*") if p.is_file()}
    result = rebuild_library(LibraryRebuild(sources=sources), tmp_path / "result")
    assert (result.sessions, result.segments, result.messages, result.missing_files) == (2, 2, 2, 0)
    with closing(sqlite3.connect(result.database)) as db:
        rows = db.execute(
            "SELECT s.id,r.uploader_name,d.content,d.room_id,r.id,g.file_path,t.final_text "
            "FROM recording_sessions s JOIN live_rooms r ON s.room_id=r.id "
            "JOIN danmaku d ON d.session_id=s.id JOIN raw_segments g ON g.session_id=s.id "
            "JOIN transcripts t ON t.segment_id=g.id ORDER BY s.id"
        ).fetchall()
        assert [row[1] for row in rows] == ["new", "old"]
        for sid, name, message, public, room, path, transcript in rows:
            assert name == message == transcript and public == room
            layout = RecordingStorage.model_validate_json(
                db.execute("SELECT value FROM app_settings WHERE key=?", (f"{LOCATION_PREFIX}{sid}",)).fetchone()[0]
            )
            assert layout.session_dir(sid, tmp_path) == Path(path).parent
            assert Path(path).read_text() == name
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone() == (6,)
    assert all(hashlib.sha256(p.read_bytes()).digest() == digest for p, digest in original.items())


def test_files_only_cli_preserves_archive_and_uses_manifest_timing(tmp_path: Path) -> None:
    storage = tmp_path / "original"
    directory = storage / "raw" / "session_7"
    directory.mkdir(parents=True)
    (directory / "part.ts").write_bytes(b"sample")
    (directory / "segments.csv").write_text("part.ts,0,3.5\n", encoding="utf-8")
    identity = {"session_id": 7, "received_at": "2026-01-01T00:00:00Z", "platform": "fixture", "source_id": "example"}
    event = {"occurred_at": "2026-01-01T00:00:00Z", "content": "fixture message"}
    archive = directory / "danmaku.jsonl"
    lines = [{**identity, "kind": "session"}, {**identity, "kind": "normalized", "payload": event}]
    archive.write_bytes(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
    before = archive.read_bytes()
    result = CliRunner().invoke(app, ["storage-rebuild", "--files", str(storage), "--output", str(tmp_path / "new")])
    assert result.exit_code == 0, result.output
    with closing(sqlite3.connect(tmp_path / "new" / "blc.db")) as db:
        assert db.execute("SELECT file_path,duration_s FROM raw_segments").fetchone() == (
            str(directory / "part.ts"),
            3.5,
        )
        assert db.execute("SELECT content FROM danmaku").fetchone() == ("fixture message",)
    assert archive.read_bytes() == before


def test_invalid_source_or_existing_destination_never_publishes_partial_database(tmp_path: Path) -> None:
    source = _source(tmp_path / "source", 6, "source")
    out = tmp_path / "new"
    with pytest.raises(ValueError, match="重复"):
        rebuild_library(LibraryRebuild(sources=[source, source]), out)
    assert source.database is not None
    with closing(sqlite3.connect(source.database)) as db, db:
        db.execute("UPDATE schema_meta SET schema_version=4")
    with pytest.raises(ValueError, match="Schema"):
        rebuild_library(LibraryRebuild(sources=[source]), out)
    assert not out.exists()
    out.mkdir()
    (out / "blc.db").write_bytes(b"original")
    with pytest.raises(FileExistsError):
        rebuild_library(LibraryRebuild(sources=[source]), out)
    assert (out / "blc.db").read_bytes() == b"original"


async def test_web_job_rebuilds_detached_import_through_settings(temp_db: None, tmp_path: Path) -> None:
    from app.web.routers.container import router
    from app.web.routers.jobs import router as jobs
    from app.web.services.background_jobs import web_job_manager

    storage = tmp_path / "library"
    directory = storage / "raw" / "imports" / ("a" * 32) / "segments"
    directory.mkdir(parents=True)
    media = directory / "part_000000.mkv"
    media.write_bytes(b"media")
    service = FastAPI()
    service.include_router(router, prefix="/api")
    service.include_router(jobs, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=service), base_url="http://test") as http:
        try:
            response = await http.post("/api/settings/storage-rebuild", json={"sources": [{"storage": str(storage)}]})
            assert response.status_code == 202
            job = response.json()["job"]
            async with asyncio.timeout(15):
                while job["status"] not in {"succeeded", "failed", "cancelled"}:
                    await asyncio.sleep(0.05)
                    job = (await http.get(f"/api/jobs/{job['id']}")).json()
            assert job["status"] == "succeeded", job.get("error")
            with closing(sqlite3.connect(job["result"]["database"])) as db:
                assert db.execute("SELECT file_path FROM raw_segments").fetchone() == (str(media),)
        finally:
            await web_job_manager.stop()


def test_database_replays_only_uncommitted_journal_suffix(tmp_path: Path) -> None:
    source = _source(tmp_path / "original", 6, "history")
    assert source.database is not None
    journal = source.storage / "raw/session_1/danmaku-pending.jsonl"
    header = {"version": 1, "identity": "prior", "session_id": 1}
    event = {
        "kind": "message",
        "room_id": 1,
        "ts": "2026-01-01T00:00:00Z",
        "msg_type": "danmaku",
        "user": None,
        "content": "history",
        "value": 1,
    }
    prefix = b"".join(json.dumps(record).encode() + b"\n" for record in (header, event))
    journal.write_bytes(prefix + json.dumps({**event, "content": "remaining"}).encode() + b"\n")
    with closing(sqlite3.connect(source.database)) as db, db:
        db.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)",
            ("session_danmaku_cursor:v1:1", json.dumps({"identity": "prior", "offset": len(prefix)})),
        )
    result = rebuild_library(LibraryRebuild(sources=[source]), tmp_path / "result")
    with closing(sqlite3.connect(result.database)) as db:
        assert db.execute("SELECT content FROM danmaku ORDER BY id").fetchall() == [("history",), ("remaining",)]


def test_previous_schema_recovers_local_import_using_original_metadata(tmp_path: Path) -> None:
    source = _source(tmp_path / "local", 5, "local")
    assert source.database is not None and source.original_storage is not None
    import_id = "b" * 32
    relative = Path("raw", "imports", import_id, "segments", "part.ts")
    original = source.storage / relative
    original.parent.mkdir(parents=True)
    (source.storage / "raw/session_1/part.ts").rename(original)
    with closing(sqlite3.connect(source.database)) as db, db:
        db.execute("UPDATE raw_segments SET file_path=?", (str(source.original_storage / relative),))
        db.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES('local_source:1',?,CURRENT_TIMESTAMP)",
            (json.dumps({"version": 1, "import_id": import_id, "has_comments": True}),),
        )
    result = rebuild_library(LibraryRebuild(sources=[source]), tmp_path / "result")
    with closing(sqlite3.connect(result.database)) as db:
        layout = RecordingStorage.model_validate_json(
            db.execute("SELECT value FROM app_settings WHERE key=?", (f"{LOCATION_PREFIX}1",)).fetchone()[0]
        )
        assert layout.media_root(1, tmp_path) == original.parent
        assert db.execute("SELECT file_path FROM raw_segments").fetchone() == (str(original),)


def test_rebuilt_flat_raw_reference_is_limited_to_registered_file(tmp_path: Path) -> None:
    source = _source(tmp_path / "source", 6, "flat")
    assert source.database is not None
    media = source.storage / "raw/part.ts"
    (source.storage / "raw/session_1/part.ts").rename(media)
    with closing(sqlite3.connect(source.database)) as db, db:
        db.execute("UPDATE raw_segments SET file_path='raw/part.ts'")
    result = rebuild_library(LibraryRebuild(sources=[source]), tmp_path / "result")
    with closing(sqlite3.connect(result.database)) as db:
        assert db.execute(
            "SELECT key,value FROM app_settings WHERE key GLOB ?", (f"{LEGACY_FILE_PREFIX}*",)
        ).fetchall() == [(f"{LEGACY_FILE_PREFIX}1:1", str(media))]


@pytest.mark.parametrize("with_message", [False, True])
def test_files_with_observations_choose_one_message_source(tmp_path: Path, with_message: bool) -> None:
    storage = tmp_path / "original"
    folder = storage / "raw/session_7"
    folder.mkdir(parents=True)
    stamp = "2026-01-01T01:00:00Z"
    common = {"session_id": 7, "received_at": stamp, "platform": "fixture", "source_id": "room"}
    event = {**common, "kind": "normalized", "payload": {"occurred_at": stamp, "content": "same"}}
    archive = [{**common, "kind": "session"}, event, event]
    journal = [
        {"version": 1, "identity": "old-identity", "session_id": 7},
        {"kind": "observation", "evidence": {"status": "available"}},
    ]
    if with_message:
        journal.append(
            {
                "kind": "message",
                "room_id": 9,
                "ts": stamp,
                "msg_type": "danmaku",
                "user": None,
                "content": "journal",
                "value": 1,
            }
        )
    for name, records in (("danmaku.jsonl", archive), ("danmaku-pending.jsonl", journal)):
        (folder / name).write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records))
    result = rebuild_library(LibraryRebuild(sources=[LibrarySource(storage=storage)]), tmp_path / "result")
    with closing(sqlite3.connect(result.database)) as db:
        assert db.execute("SELECT content FROM danmaku ORDER BY id").fetchall() == (
            [("journal",)] if with_message else [("same",), ("same",)]
        )
        assert (
            json.loads(db.execute("SELECT value FROM app_settings WHERE key='session_danmaku:1'").fetchone()[0])[
                "status"
            ]
            == "available"
        )
        evidence = db.execute("SELECT value FROM app_settings WHERE key='library_rebuild:session:1'").fetchone()[0]
        assert json.loads(evidence)["message_source"] == ("journal" if with_message else "archive")


def test_renumbered_summaries_are_readable_without_resuming_work(
    tmp_path: Path, temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.segment_summary import SummaryStep, segment_summary_views
    from app.analysis.session_summary import session_timeline_summary_view
    from app.db import session

    sources = [_source(tmp_path / name, 6, name) for name in ("first", "second")]
    assert sources[1].database is not None
    with closing(sqlite3.connect(sources[1].database)) as db, db:
        whole = {
            "version": 3,
            "session_id": 1,
            "analysis_basis": "full_session_asr",
            "transcript_signature": "a" * 64,
            "transcript_count": 1,
            "character_count": 6,
            "source": "llm",
            "summary": "saved whole",
            "generated_at": "2026-01-01T00:00:00Z",
            "coverage": {"mode": "degraded", "missing": [{"segment_id": 1}]},
            "summary_mode": "degraded",
        }
        step = SummaryStep(
            session_id=1,
            segment_id=1,
            signature="old",
            generation="g",
            operation_id="op",
            lease="old",
            status="ready",
            summary="saved segment",
        )
        db.executemany(
            "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)",
            [
                ("session_timeline_summary:1", json.dumps(whole)),
                ("segment_summary_step:v1:1:1", step.model_dump_json()),
            ],
        )
    result = rebuild_library(LibraryRebuild(sources=sources), tmp_path / "result")
    engine = create_engine(URL.create("sqlite", database=result.database))
    session.install_database_telemetry(engine)
    try:
        monkeypatch.setattr(session, "engine", engine)
        with Session(engine) as db:
            summary = segment_summary_views(db, 2)[2]
            assert (summary["summary_status"], summary["context_summary"]) == ("archived", "saved segment")
        whole_view = session_timeline_summary_view(2, processing_state="completed", ended=True)
        assert whole_view["status"] == "archived" and whole_view["summary"] == "saved whole"
        assert whole_view["verification"]["status"] == "not_run"
        assert whole_view["coverage"]["missing"] == [{"segment_id": 2}]
        with closing(sqlite3.connect(result.database)) as db:
            assert db.execute(
                "SELECT count(*) FROM app_settings WHERE key GLOB 'segment_summary_queue:*' "
                "OR key GLOB 'session_timeline_summary_request:*'"
            ).fetchone() == (0,)
    finally:
        engine.dispose()
