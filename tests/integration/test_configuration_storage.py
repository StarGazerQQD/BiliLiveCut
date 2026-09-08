"""存储策略通过实际数据库引用保护活动和已发布文件。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.core.config import settings
from app.core.configuration import ConfigurationChange, save_configuration

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize(
    "protection", ["recording", "pending_task", "review", "shared_clip", "web_job", "reanalysis", "none"]
)
def test_raw_cleanup_preserves_live_references_and_unknown_files(temp_db: None, protection: str) -> None:
    from app.core.paths import raw_dir
    from app.db.entities import (
        AppSetting,
        FinalClip,
        HighlightCandidate,
        LiveRoom,
        RawSegment,
        RecordingSession,
        SegmentTask,
    )
    from app.db.session import get_session
    from app.pipeline.storage_lifecycle import cleanup_old_raw_files

    old = datetime.now(UTC) - timedelta(days=30)
    media = raw_dir() / "known.ts"
    media.write_bytes(b"recorded video")
    unknown = raw_dir() / "unregistered.ts"
    unknown.write_bytes(b"user owned")
    with get_session() as db:
        room = LiveRoom(input_url="retention", room_id=12)
        db.add(room)
        db.flush()
        recording = RecordingSession(
            room_id=room.id,
            started_at=old,
            ended_at=None if protection == "recording" else old,
            status="recording" if protection == "recording" else "stopped",
        )
        db.add(recording)
        db.flush()
        segment = RawSegment(
            session_id=recording.id, seq=0, file_path=str(media), start_ts=old, end_ts=old + timedelta(seconds=10)
        )
        db.add(segment)
        db.flush()
        if protection == "pending_task":
            db.add(
                SegmentTask(
                    segment_id=segment.id,
                    session_id=recording.id,
                    stage="queued_for_transcription",
                    pipeline_key=f"keep:{segment.id}",
                )
            )
        if protection in {"review", "shared_clip"}:
            candidate = HighlightCandidate(
                session_id=recording.id,
                peak_ts=old,
                start_ts=old,
                end_ts=old + timedelta(seconds=10),
                highlight_score=0.8,
                dedup_hash=f"retention-{protection}",
                status="pending" if protection == "review" else "clipped",
            )
            db.add(candidate)
            db.flush()
            if protection == "shared_clip":
                db.add(FinalClip(candidate_id=candidate.id, file_path=str(media), status="published"))
        if protection == "web_job":
            db.add(AppSetting(key="web_job:active", value='{"status":"running"}'))
        if protection == "reanalysis":
            db.add(AppSetting(key=f"session_reanalysis:{recording.id}", value="{}"))
    assert cleanup_old_raw_files() == (1 if protection == "none" else 0)
    assert media.exists() is (protection != "none")
    assert unknown.read_bytes() == b"user owned"


def test_disk_settings_reach_running_recording_guard(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.core.runtime_settings import settings_scope
    from app.pipeline import storage_lifecycle

    monkeypatch.setattr(storage_lifecycle, "get_disk_usage", lambda: {"free_gb": 7.0})
    with settings_scope():
        assert storage_lifecycle.should_stop_recording() is False
        save_configuration(ConfigurationChange(values={"critical_disk_threshold_gb": 8}))
        assert settings.critical_disk_threshold_gb == 5
        assert storage_lifecycle.should_stop_recording() is True


@pytest.mark.parametrize("output_state", ["usable", "missing", "rejected", "partial"])
def test_completed_clip_delay_changes_raw_retention(temp_db: None, tmp_path: Path, output_state: str) -> None:
    from app.core.paths import raw_dir
    from app.db.entities import FinalClip, HighlightCandidate, LiveRoom, RawSegment, RecordingSession, SegmentTask
    from app.db.session import get_session
    from app.pipeline.storage_lifecycle import cleanup_old_raw_files

    old = datetime.now(UTC) - timedelta(hours=30)
    media = raw_dir() / "completed.ts"
    media.write_bytes(b"video")
    with get_session() as db:
        room = LiveRoom(input_url="delay", room_id=13)
        db.add(room)
        db.flush()
        recording = RecordingSession(room_id=room.id, started_at=old, ended_at=old, status="stopped")
        db.add(recording)
        db.flush()
        segment = RawSegment(
            session_id=recording.id, seq=0, file_path=str(media), start_ts=old, end_ts=old + timedelta(seconds=10)
        )
        candidate = HighlightCandidate(
            session_id=recording.id,
            start_ts=old,
            end_ts=old + timedelta(seconds=10),
            peak_ts=old,
            highlight_score=0.9,
            dedup_hash="completed-retention",
            status="clipped",
        )
        db.add(segment)
        if output_state == "partial":
            candidate.end_ts = old + timedelta(seconds=5)
        db.add(candidate)
        db.flush()
        db.add(
            SegmentTask(
                segment_id=segment.id,
                session_id=recording.id,
                stage="completed",
                completed_at=old,
                pipeline_key=f"finished:{segment.id}",
            )
        )
        clip_path = tmp_path / "final.mp4"
        clip_path.write_bytes(b"finished")
        db.add(
            FinalClip(
                candidate_id=candidate.id,
                file_path=str(clip_path),
                status="rejected" if output_state == "rejected" else "published",
                created_at=old,
            )
        )
    if output_state == "missing":
        clip_path.unlink()
    save_configuration(ConfigurationChange(values={"clip_cleanup_delay_hours": 48}))
    assert cleanup_old_raw_files() == 0
    save_configuration(ConfigurationChange(values={"clip_cleanup_delay_hours": 24}))
    assert cleanup_old_raw_files() == (1 if output_state == "usable" else 0)
    if output_state != "missing":
        assert clip_path.read_bytes() == b"finished"


def test_media_use_lock_blocks_cleanup_and_releases_after_failure(temp_db: None) -> None:
    from app.core.media_usage import media_in_use, media_operation
    from app.db.session import get_session
    from app.pipeline.storage_lifecycle import cleanup_old_raw_files

    with pytest.raises(RuntimeError, match="failed operation"):
        with media_operation():
            with get_session() as db:
                assert media_in_use(db) is True
            assert cleanup_old_raw_files() == 0
            raise RuntimeError("failed operation")
    with get_session() as db:
        assert media_in_use(db) is False


def test_alert_threshold_controls_notification_independently(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.notify import webhook
    from app.pipeline import storage_lifecycle
    from app.web.routers import monitor_router

    notifications: list[tuple[object, ...]] = []
    monkeypatch.setattr(storage_lifecycle, "get_disk_usage", lambda: {"free_gb": 15.0})
    monkeypatch.setattr(storage_lifecycle, "check_disk_safe", lambda: (True, "safe"))
    monkeypatch.setattr(storage_lifecycle, "get_directory_size", lambda _path: 0)
    monkeypatch.setattr(webhook, "notify_disk_alert", lambda *args: notifications.append(args))
    monkeypatch.setattr(monitor_router, "_last_disk_alert", 0)
    save_configuration(ConfigurationChange(values={"disk_alert_threshold_gb": 20}))
    monitor_router.get_monitor_data()
    assert len(notifications) == 1
    assert notifications[0][:2] == (15.0, 20)
