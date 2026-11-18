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
from app.recording.session_storage import LOCATION_PREFIX


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
