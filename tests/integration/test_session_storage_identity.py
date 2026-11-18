from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlmodel import select

from app.cli import app
from app.core.config import settings
from app.core.recording_storage import RecordingStorage
from app.db.entities import Danmaku, RawSegment, RecordingSession
from app.db.session import get_session
from app.plugins.live_source import SourceRoom
from app.recording.danmaku_archive import DanmakuArchive, archive_path, archive_view, open_archive_snapshot
from app.recording.danmaku_journal import DanmakuJournal, drain_journal, journal_path
from app.recording.imports import ImportRequest, create_import, get_import, upload_path
from app.recording.recorder import Recorder
from app.recording.session_storage import JOURNAL_COLLISION, OWNER_FILE, OWNER_PREFIX, bind_new_storage, verify_storage
from app.recording.storage_check import check_storage
from app.recording.writer_ownership import SessionWriter
from app.sources.rooms import register_room, room_source
from tests.integration.test_source_runtime import RuntimeSource
from tests.integration.test_source_runtime import runtime as runtime


async def test_recording_skips_copied_storage_and_replays_only_its_own_journal(runtime: RuntimeSource) -> None:
    root = Path(settings.storage_root)
    room = await register_room("123", True, "external")
    old = DanmakuJournal(1)
    old.append_messages([Danmaku(session_id=1, room_id=123, content="old")])
    original = journal_path(1).read_bytes()
    (root / "clips/session_2").mkdir(parents=True)
    (root / "ready_to_upload/session_3").mkdir(parents=True)
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    with SessionWriter.create(root / ".recording_writers") as writer:
        recorder._writer = writer
        sid = recorder._create_session()
    assert sid == 4
    with get_session() as db:
        owner = verify_storage(db, sid)
        assert owner is not None and db.get(RecordingSession, 1) is None
    journal = DanmakuJournal(sid, storage_identity=owner)
    journal.append_messages([Danmaku(session_id=sid, room_id=123, content="new")])
    saved = journal_path(sid).read_bytes()
    journal_path(sid).write_bytes(original.replace(b'"session_id":1', b'"session_id":4'))
    with pytest.raises(ValueError, match="存储身份"):
        drain_journal(sid, close=True)
    journal_path(sid).write_bytes(saved)
    assert drain_journal(sid, close=True)
    assert drain_journal(sid, close=True)
    with get_session() as db:
        assert [(x.session_id, x.content) for x in db.exec(select(Danmaku)).all()] == [(sid, "new")]
    assert journal_path(1).read_bytes() == original


def test_mismatched_storage_and_known_legacy_conflict_are_preserved(temp_db: None) -> None:
    with get_session() as db:
        row = RecordingSession(room_id=1)
        owner = bind_new_storage(db, row)
        assert row.id == 1
    journal = DanmakuJournal(1, storage_identity=owner)
    journal.append_messages([Danmaku(session_id=1, room_id=4, content="original")])
    path = journal_path(1)
    original = path.read_bytes()
    (path.parent / OWNER_FILE).write_bytes(b"a" * 32 + b"\n")
    with pytest.raises(ValueError, match="身份不一致"):
        drain_journal(1)
    old = DanmakuJournal(2)
    old.append_messages([Danmaku(session_id=2, room_id=4, content="legacy")])
    with get_session() as db:
        db.add(RecordingSession(id=2, room_id=2, error_message=f"录制失败：RuntimeError: {JOURNAL_COLLISION}"))
    with pytest.raises(ValueError, match="旧弹幕档案冲突"):
        drain_journal(2, close=True)
    assert path.read_bytes() == original
    assert not archive_view(2)["available"]
    with get_session() as db:
        assert db.exec(select(Danmaku)).all() == []


def test_storage_check_identifies_history_without_modifying_database_or_files(temp_db: None) -> None:
    from app.db.session import engine

    root = Path(settings.storage_root)
    directory = root / "raw/session_74"
    directory.mkdir(parents=True)
    (directory / "old.ts").write_bytes(b"archived media")
    now = datetime.now(UTC)
    with get_session() as db:
        db.add(RecordingSession(id=74, room_id=2, started_at=now, error_message=JOURNAL_COLLISION))
        db.add(Danmaku(session_id=74, room_id=2, ts=now - timedelta(days=1), content="private fixture"))
        db.add(RawSegment(session_id=74, seq=1, file_path="raw/session_74/missing.ts", duration_s=1))
    database = Path(engine.url.database)
    before = database.read_bytes()
    result = CliRunner().invoke(app, ["storage-check", "--database", str(database), "--storage", str(root)])
    assert result.exit_code == 1
    for msg in ["补做记录冲突", "1 条弹幕早于", "old.ts", "登记文件在副本中缺失"]:
        assert msg in result.output
    assert "private fixture" not in result.output and database.read_bytes() == before
    assert (directory / "old.ts").read_bytes() == b"archived media"


def test_rolled_back_reservation_cannot_be_reused_by_later_session(temp_db: None) -> None:
    with pytest.raises(ValueError, match="模拟"):
        with get_session() as db:
            bind_new_storage(db, RecordingSession(room_id=1))
            raise ValueError("模拟场次事务回滚")
    with get_session() as db:
        row = RecordingSession(room_id=1)
        owner = bind_new_storage(db, row)
        assert row.id == 2 and verify_storage(db, 2) == owner


def test_single_archive_replacement_is_rejected_for_read_and_append(temp_db: None) -> None:
    room = SourceRoom(platform="test", source_id="1", canonical_url="https://example.invalid/1")
    with get_session() as db:
        owner = bind_new_storage(db, RecordingSession(room_id=1))
    archive = DanmakuArchive(1, room, storage_identity=owner)
    archive.start()
    genuine = archive_path(1).read_bytes()
    assert archive_view(1)["available"]
    swapped = genuine.replace(owner.encode(), b"x" * 32)
    archive_path(1).write_bytes(swapped)
    assert not archive_view(1)["available"]
    with pytest.raises(ValueError, match="身份不一致"):
        open_archive_snapshot(1)
    archive.append([{"text": "new"}], event_format="normalized")
    assert archive.error and archive_path(1).read_bytes() == swapped


def test_legacy_first_replay_validates_messages_before_committing_batch(temp_db: None) -> None:
    now = datetime.now(UTC)
    with get_session() as db:
        db.add(RecordingSession(id=1, room_id=3, started_at=now))
    journal = DanmakuJournal(1)
    journal.append_messages(
        [
            Danmaku(session_id=1, room_id=3, ts=now, content="matching"),
            Danmaku(session_id=1, room_id=3, ts=now - timedelta(days=1), content="older"),
        ]
    )
    with pytest.raises(ValueError, match="早于当前场次"):
        drain_journal(1, close=True)
    with get_session() as db:
        assert db.exec(select(Danmaku)).all() == []


async def test_legacy_external_replay_preserves_provider_event_time(runtime: RuntimeSource) -> None:
    room = await register_room("123", True, "external")
    now = datetime.now(UTC)
    with get_session() as db:
        db.add(RecordingSession(id=1, room_id=room.id, started_at=now))
    archive = DanmakuArchive(1, room_source(room))
    archive.start()
    early = now - timedelta(minutes=5)
    journal = DanmakuJournal(1)
    for _ in range(2):
        journal.append_messages([Danmaku(session_id=1, room_id=room.id, ts=early, content="buffered")])
    assert not drain_journal(1, limit=1)
    assert drain_journal(1, close=True)
    with get_session() as db:
        rows = db.exec(select(Danmaku)).all()
        assert len(rows) == 2
        assert all(row.ts.replace(tzinfo=UTC) == early for row in rows)


@pytest.mark.parametrize("uncheckpointed", [False, True])
def test_storage_check_preserves_offline_wal_files(temp_db: None, tmp_path: Path, uncheckpointed: bool) -> None:
    from app.db.session import engine

    source = tmp_path / "original.db"
    copy = tmp_path / "offline"
    copy.mkdir()
    snapshot = copy / source.name
    with closing(sqlite3.connect(engine.url.database)) as original, closing(sqlite3.connect(source)) as writer:
        original.backup(writer)
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
            (f"{OWNER_PREFIX}7", "a" * 32, datetime.now(UTC).isoformat()),
        )
        writer.commit()
        if uncheckpointed:
            shutil.copyfile(source, snapshot)
            wal = source.with_name(source.name + "-wal")
            shutil.copyfile(wal, copy / wal.name)
    if not uncheckpointed:
        shutil.copyfile(source, snapshot)

    def hashes() -> dict[str, str]:
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in copy.iterdir()}

    initial = hashes()
    _, findings = check_storage(snapshot, copy)
    assert "场次 7 已登记存储身份，原始目录缺失" in findings
    assert hashes() == initial


async def test_storage_settings_pin_both_locations_for_live_and_historical_sessions(
    runtime: RuntimeSource, tmp_path: Path
) -> None:
    from app.web.main import app as web_app

    client = TestClient(web_app)
    api = "/api/settings/configuration"
    raw, chat = tmp_path / "video", tmp_path / "messages"
    fields = {field["key"]: field for field in client.get(api).json()["fields"]}
    assert all(
        fields[key]["editable"] and fields[key]["effect"] == "next_recording"
        for key in ("storage_raw_dir", "storage_danmaku_dir")
    )
    assert (
        client.patch(api, json={"values": {"storage_raw_dir": str(raw), "storage_danmaku_dir": str(chat)}}).status_code
        == 200
    )
    room = await register_room("123", True, "external")
    source = room_source(room)
    recorder = Recorder(source, room.id, metadata_prepared=True)
    with SessionWriter.create(Path(settings.storage_root) / ".recording_writers") as writer:
        recorder._writer = writer
        sid = recorder._create_session()
    locations, owner = recorder._locations, recorder._storage_identity
    assert (raw / f"session_{sid}" / OWNER_FILE).read_bytes() == (chat / f"session_{sid}" / OWNER_FILE).read_bytes()
    archive = DanmakuArchive(sid, source, storage_identity=owner, locations=locations)
    journal = DanmakuJournal(sid, storage_identity=owner, locations=locations)
    archive.start()
    assert (
        client.patch(api, json={"values": {"storage_raw_dir": "later", "storage_danmaku_dir": "later"}}).status_code
        == 200
    )
    archive.append([{"text": "sample"}], event_format="normalized")
    journal.append_messages([Danmaku(session_id=sid, room_id=room.id, content="sample")])
    assert drain_journal(sid, close=True)
    assert archive_path(sid).parent == chat / f"session_{sid}"
    assert archive_view(sid)["available"] and not (raw / f"session_{sid}" / "danmaku.jsonl").exists()
    with get_session() as db:
        second = RecordingSession(room_id=room.id)
        bind_new_storage(db, second)
    assert (Path(settings.storage_root) / "later" / f"session_{second.id}" / OWNER_FILE).is_file()
    assert client.patch(api, json={"reset": ["storage_raw_dir", "storage_danmaku_dir"]}).status_code == 200
    assert RecordingStorage.configured() == RecordingStorage()
    stream, size = open_archive_snapshot(sid)
    with stream:
        assert len(stream.read()) == size
    from app.db.session import engine

    raw_copy, chat_copy = tmp_path / "raw-copy", tmp_path / "chat-copy"
    shutil.copytree(raw, raw_copy)
    shutil.copytree(chat, chat_copy)
    database = Path(engine.url.database)
    _, missing = check_storage(database, Path(settings.storage_root))
    assert any("尚未映射" in item for item in missing)
    _, checked = check_storage(database, Path(settings.storage_root), ((str(raw), raw_copy), (str(chat), chat_copy)))
    assert checked == []


def test_incomplete_import_keeps_separate_upload_locations_after_change(temp_db: None, tmp_path: Path) -> None:
    from app.core.configuration import ConfigurationChange, save_configuration

    raw, chat = tmp_path / "video", tmp_path / "comments"
    save_configuration(ConfigurationChange(values={"storage_raw_dir": str(raw), "storage_danmaku_dir": str(chat)}))
    record = create_import(ImportRequest(title="导入", video_name="v.mp4", comments_name="c.json"), "admin")
    save_configuration(ConfigurationChange(reset=["storage_raw_dir", "storage_danmaku_dir"]))
    reopened = get_import(record.id)
    assert upload_path(reopened, "video").parent == raw / "imports" / record.id
    assert upload_path(reopened, "comments").parent == chat / "imports" / record.id
    assert "locations" not in reopened.model_dump()
