"""0.1.17.x Schema v4 到 0.1.18 Schema v5 的迁移测试。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

LEGACY_APP_VERSION = "0.1.17.4-alpha"
TARGET_APP_VERSION = "0.1.18.0-alpha"


def _seed_legacy_business_data() -> dict[str, int]:
    """写入迁移必须完整保留的 v4 核心业务数据。"""
    from app.db.entities import (
        FinalClip,
        HighlightCandidate,
        HighlightEvent,
        LiveRoom,
        RawSegment,
        RecordingSession,
        ReviewStatus,
        Transcript,
    )
    from app.db.session import get_session

    start = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="legacy-room", room_id=42, uploader_name="旧主播", authorized=True)
        db.add(room)
        db.flush()
        assert room.id is not None
        recording = RecordingSession(room_id=room.id, started_at=start)
        db.add(recording)
        db.flush()
        assert recording.id is not None
        segment = RawSegment(
            session_id=recording.id,
            seq=0,
            file_path="legacy.ts",
            start_ts=start,
            end_ts=start + timedelta(minutes=5),
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        transcript = Transcript(segment_id=segment.id, final_text="旧转写必须保留")
        db.add(transcript)
        candidate = HighlightCandidate(
            session_id=recording.id,
            start_ts=start,
            peak_ts=start + timedelta(seconds=30),
            end_ts=start + timedelta(seconds=90),
            highlight_score=0.9,
            dedup_hash="legacy-candidate",
        )
        db.add(candidate)
        db.flush()
        assert candidate.id is not None
        event = HighlightEvent(
            candidate_id=candidate.id,
            session_id=recording.id,
            raw_start_ts=candidate.start_ts,
            raw_end_ts=candidate.end_ts,
            review_status=ReviewStatus.APPROVED_SOLO,
            review_reason="旧审核结论必须保留",
        )
        db.add(event)
        clip = FinalClip(candidate_id=candidate.id, file_path="legacy.mp4", title="旧成品")
        db.add(clip)
        db.flush()
        assert transcript.id is not None and event.id is not None and clip.id is not None
        return {
            "room": room.id,
            "session": recording.id,
            "segment": segment.id,
            "transcript": transcript.id,
            "candidate": candidate.id,
            "event": event.id,
            "clip": clip.id,
        }


def _downgrade_fixture_to_v4() -> Path:
    """把当前新库精确还原为受支持的 0.1.17.x v4 迁移夹具。"""
    from app.db.entities import HotspotEvent
    from app.db.schema import SchemaMeta, compute_legacy_v4_fingerprint
    from app.db.session import engine

    HotspotEvent.__table__.drop(bind=engine, checkfirst=False)
    with Session(engine) as db:
        meta = db.get(SchemaMeta, 1)
        assert meta is not None
        meta.schema_version = 4
        meta.schema_fingerprint = compute_legacy_v4_fingerprint()
        meta.app_version = LEGACY_APP_VERSION
        db.add(meta)
        db.commit()
    return Path(engine.url.database or "")


def _force_target_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟最终 0.1.18 启动版本。"""
    from app.db import schema

    monkeypatch.setattr(schema, "_app_version_str", lambda: TARGET_APP_VERSION)


def test_existing_v4_database_migrates_and_preserves_business_data(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 v4 夹具升级后保留房间、场次、片段、转写、审核及成品。"""
    from app.db.entities import (
        FinalClip,
        HighlightCandidate,
        HighlightEvent,
        LiveRoom,
        RawSegment,
        RecordingSession,
        Transcript,
    )
    from app.db.migration_v0180 import migration_backup_path
    from app.db.schema import SchemaMeta, assure_schema, validate_schema
    from app.db.session import engine

    ids = _seed_legacy_business_data()
    db_path = _downgrade_fixture_to_v4()
    _force_target_version(monkeypatch)

    assure_schema()
    assure_schema()

    with Session(engine) as db:
        meta = db.get(SchemaMeta, 1)
        assert meta is not None
        assert meta.schema_version == 5
        assert meta.app_version == TARGET_APP_VERSION
        assert db.get(LiveRoom, ids["room"]).uploader_name == "旧主播"  # type: ignore[union-attr]
        assert db.get(RecordingSession, ids["session"]) is not None
        assert db.get(RawSegment, ids["segment"]).file_path == "legacy.ts"  # type: ignore[union-attr]
        assert db.get(Transcript, ids["transcript"]).final_text == "旧转写必须保留"  # type: ignore[union-attr]
        assert db.get(HighlightCandidate, ids["candidate"]).dedup_hash == "legacy-candidate"  # type: ignore[union-attr]
        assert db.get(HighlightEvent, ids["event"]).review_reason == "旧审核结论必须保留"  # type: ignore[union-attr]
        assert db.get(FinalClip, ids["clip"]).title == "旧成品"  # type: ignore[union-attr]
    assert validate_schema() is True

    backup_path = migration_backup_path(db_path)
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as backup:
        backup_meta = backup.execute("SELECT schema_version, app_version FROM schema_meta WHERE id = 1").fetchone()
        hotspot_table = backup.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hotspot_events'"
        ).fetchone()
        preserved = backup.execute("SELECT final_text FROM transcripts WHERE id = ?", (ids["transcript"],)).fetchone()
    assert backup_meta == (4, LEGACY_APP_VERSION)
    assert hotspot_table is None
    assert preserved == ("旧转写必须保留",)


def test_failed_migration_rolls_back_and_retry_reuses_backup(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """建表后故障不留下半迁移结构，重试复用备份并成功。"""
    from app.db.entities import HotspotEvent
    from app.db.migration_v0180 import SchemaMigrationError, migration_backup_path
    from app.db.schema import SchemaMeta, assure_schema
    from app.db.session import engine

    _seed_legacy_business_data()
    db_path = _downgrade_fixture_to_v4()
    _force_target_version(monkeypatch)
    original_create = HotspotEvent.__table__.create

    def create_then_fail(*args: object, **kwargs: object) -> None:
        original_create(*args, **kwargs)
        raise RuntimeError("injected migration failure")

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(HotspotEvent.__table__, "create", create_then_fail)
        with pytest.raises(SchemaMigrationError, match="补偿回滚"):
            assure_schema()

    with engine.connect() as connection:
        hotspot_table = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hotspot_events'"
        ).fetchone()
    with Session(engine) as db:
        meta = db.get(SchemaMeta, 1)
        assert meta is not None
        assert meta.schema_version == 4
        assert db.exec(select(SchemaMeta)).one().app_version == LEGACY_APP_VERSION
    assert hotspot_table is None
    backup_path = migration_backup_path(db_path)
    assert backup_path.is_file()
    backup_mtime = backup_path.stat().st_mtime_ns

    assure_schema()

    assert backup_path.stat().st_mtime_ns == backup_mtime
    with Session(engine) as db:
        meta = db.get(SchemaMeta, 1)
        assert meta is not None and meta.schema_version == 5


def test_migration_refuses_modified_v4_fingerprint(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """伪造或修改过的 v4 不会被自动迁移。"""
    from app.db.schema import SchemaMeta, assure_schema
    from app.db.session import engine

    _downgrade_fixture_to_v4()
    with Session(engine) as db:
        meta = db.get(SchemaMeta, 1)
        assert meta is not None
        meta.schema_fingerprint = "0" * 64
        db.add(meta)
        db.commit()
    _force_target_version(monkeypatch)

    with pytest.raises(RuntimeError, match="指纹不匹配"):
        assure_schema()
