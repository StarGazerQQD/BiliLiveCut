from __future__ import annotations

import multiprocessing
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlmodel import create_engine, select

from app.clipping.drafts import publication
from app.core import paths
from app.core.media_usage import media_in_use, media_operation
from app.db import session as database
from app.db.entities import (
    AppSetting,
    ClipStatus,
    ClipVariant,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RecordingSession,
)
from app.db.telemetry import install_database_telemetry
from app.pipeline.storage_lifecycle import _clip_is_protected
from app.pipeline.workers.render import _render_file_referenced
from app.publishing.execution import UploadFile
from app.recording.segments import register_segment
from app.recording.writer_ownership import SessionWriter
from app.web.routers import media


def _hold_media_operation(database_url: str, root: str, pipe: Connection) -> None:
    paths.settings.storage_root = root
    database.settings.database_url = database_url
    database.engine = create_engine(database_url, connect_args={"check_same_thread": False})
    install_database_telemetry(database.engine)
    try:
        with media_operation():
            pipe.send("ready")
            pipe.recv()
    finally:
        database.engine.dispose()
        pipe.close()


def test_crashed_media_registration_is_reclaimed_after_storage_relocation(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = paths.storage_root()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_media_operation, args=(str(database.engine.url), str(original), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() == "ready"
        assert media_in_use()
        with database.get_session() as db:
            row = db.exec(select(AppSetting).where(AppSetting.key.startswith("media_usage:"))).one()
            key, reference = row.key, row.value
    finally:
        process.terminate()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
        parent.close()
    assert process.exitcode is not None and process.exitcode != 0
    database_path = Path(database.engine.url.database)
    with closing(sqlite3.connect(database_path)) as source, closing(sqlite3.connect(original / "blc.db")) as target:
        source.backup(target)
    moved = tmp_path / "moved-after-crash"
    assert original.resolve().parent == moved.resolve().parent == tmp_path.resolve()
    original.rename(moved)
    monkeypatch.setattr(paths.settings, "storage_root", str(moved))
    new_url = f"sqlite:///{(moved / 'blc.db').as_posix()}"
    monkeypatch.setattr(database.settings, "database_url", new_url)
    engine = create_engine(new_url, connect_args={"check_same_thread": False})
    install_database_telemetry(engine)
    monkeypatch.setattr(database, "engine", engine)
    try:
        assert not media_in_use()
        assert not Path(reference).is_absolute()
        assert not paths.resolve_storage_path(reference).exists()
        with database.get_session() as db:
            assert db.get(AppSetting, key) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("reference", ["../foreign.lock", "", "foreign/lock"])
def test_invalid_media_registration_keeps_cleanup_blocked(temp_db: None, reference: str) -> None:
    with database.get_session() as db:
        db.add(AppSetting(key="media_usage:invalid", value=reference))
    assert media_in_use()
    with database.get_session() as db:
        assert db.get(AppSetting, "media_usage:invalid").value == reference


def seed_clip() -> int:
    now = datetime.now(UTC)
    with database.get_session() as db:
        room = LiveRoom(input_url="1", room_id=1)
        db.add(room)
        db.flush()
        recording = RecordingSession(room_id=room.id, started_at=now, ended_at=now + timedelta(seconds=2))
        db.add(recording)
        db.flush()
        candidate = HighlightCandidate(
            session_id=recording.id,
            peak_ts=now,
            start_ts=now,
            end_ts=now + timedelta(seconds=2),
            dedup_hash="relocation",
        )
        db.add(candidate)
        db.flush()
        event = HighlightEvent(candidate_id=candidate.id, session_id=recording.id)
        db.add(event)
        clip = FinalClip(
            candidate_id=candidate.id,
            file_path="clips/clip.mp4",
            cover_path="clips/clip.jpg",
            duration_s=2,
            title="标题",
            description="说明",
        )
        db.add(clip)
        db.flush()
        return clip.id


async def test_database_and_media_copy_uses_new_root_without_rewriting_references(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = paths.storage_root()
    clip_id = seed_clip()
    clips = original / "clips"
    clips.mkdir(exist_ok=True)
    (clips / "clip.mp4").write_bytes(b"original video bytes")
    (clips / "clip.jpg").write_bytes(b"original cover bytes")
    database_path = Path(database.engine.url.database)
    with closing(sqlite3.connect(database_path)) as source, closing(sqlite3.connect(original / "blc.db")) as target:
        source.backup(target)
    moved = tmp_path / "moved-storage"
    assert original.resolve().parent == moved.resolve().parent == tmp_path.resolve()
    original.rename(moved)
    monkeypatch.setattr(paths.settings, "storage_root", str(moved))
    new_url = f"sqlite:///{(moved / 'blc.db').as_posix()}"
    monkeypatch.setattr(database.settings, "database_url", new_url)
    engine = create_engine(new_url, connect_args={"check_same_thread": False})
    install_database_telemetry(engine)
    monkeypatch.setattr(database, "engine", engine)
    try:
        database.init_db()
        app = FastAPI()
        app.include_router(media.router, prefix="/api")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            video = await client.get(f"/api/clips/{clip_id}/video")
            cover = await client.get(f"/api/clips/{clip_id}/cover")
        assert video.status_code == cover.status_code == 200
        assert video.content == b"original video bytes"
        assert cover.content == b"original cover bytes"
        with database.get_session() as db:
            clip = db.get(FinalClip, clip_id)
            assert (clip.file_path, clip.cover_path) == ("clips/clip.mp4", "clips/clip.jpg")
            assert UploadFile.inspect(clip.file_path).sha256 == UploadFile.inspect(str(moved / clip.file_path)).sha256
    finally:
        engine.dispose()


def test_relative_and_absolute_raw_reference_register_once(temp_db: None) -> None:
    now = datetime.now(UTC)
    path = paths.session_raw_dir(1) / "segment.ts"
    path.write_bytes(b"recorded bytes")
    with database.get_session() as db:
        row, created = register_segment(db, 1, path, duration=2, ended_at=now, size=path.stat().st_size)
        assert created and row.file_path == "raw/session_1/segment.ts"
        row.file_path = str(path)
        db.add(row)
    with database.get_session() as db:
        again, created = register_segment(db, 1, path, duration=2, ended_at=now, size=path.stat().st_size)
        assert not created and again.id == row.id and again.file_path == str(path)


@pytest.mark.parametrize("column", ["file_path", "cover_path"])
def test_cleanup_keeps_relative_reference_when_artifact_path_is_absolute(temp_db: None, column: str) -> None:
    clip_id = seed_clip()
    target = paths.clips_dir() / "referenced.mp4"
    target.write_bytes(b"must survive")
    with database.get_session() as db:
        clip = db.get(FinalClip, clip_id)
        setattr(clip, column, paths.to_storage_relative(target))
        db.add(clip)
    with pytest.raises(RuntimeError, match="transaction result unknown"), publication((target,)):
        raise RuntimeError("transaction result unknown")
    assert target.read_bytes() == b"must survive"
    with database.get_session() as db:
        assert _render_file_referenced(db, str(target))
        event = db.exec(select(HighlightEvent)).one()
        clip = db.get(FinalClip, clip_id)
        clip.status = ClipStatus.REJECTED
        setattr(clip, column, str(target))
        variant = ClipVariant(event_id=event.id, **{column: paths.to_storage_relative(target)})
        db.add_all((clip, variant))
        db.flush()
        assert _clip_is_protected(db, clip)


def test_writer_copy_does_not_inherit_original_ownership(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = paths.storage_root()
    with SessionWriter.create(paths.session_raw_dir(1)) as writer:
        identity = writer.identity
    assert not Path(identity.path).is_absolute()
    copied = tmp_path / "copied-storage"
    target = copied / identity.path
    target.parent.mkdir(parents=True)
    target.write_bytes((root / identity.path).read_bytes())
    monkeypatch.setattr(paths.settings, "storage_root", str(copied))
    with pytest.raises(ValueError, match="证明文件已被替换"):
        SessionWriter.recover(identity)
