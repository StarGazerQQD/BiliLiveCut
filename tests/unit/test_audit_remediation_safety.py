"""审计整改回归：真实数据库及入口，外部边界使用可控替身。"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlmodel import select


def test_journal_replay_preserves_success_written_during_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.publishing import journal

    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path)
    assert journal.write_remote_success("first", 1, 1, 1, "BV1")
    original = journal._atomic_write

    def write_during_replay(path: Path, content: str) -> None:
        monkeypatch.setattr(journal, "_atomic_write", original)
        assert journal.write_remote_success("second", 1, 2, 2, "BV2")
        original(path, content)

    monkeypatch.setattr(journal, "_atomic_write", write_during_replay)
    assert journal.mark_replayed("first", 1)
    assert [entry["attempt_token"] for entry in journal.read_pending_entries()] == ["second"]


def test_journal_corrupt_legacy_line_does_not_hide_later_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.publishing import journal

    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path)
    (tmp_path / "publish_journal_20200101.jsonl").write_text(
        'broken\nnull\n{"attempt_token":"valid","publish_generation":1,"remote_id":"BV1"}\n', encoding="utf-8"
    )
    assert [entry["attempt_token"] for entry in journal.read_pending_entries()] == ["valid"]
    assert journal.mark_replayed("valid", 1)
    assert journal.read_pending_entries() == []


def test_journal_failed_replace_preserves_unreplayed_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.publishing import journal

    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path)
    assert journal.write_remote_success("first", 1, 1, 1, "BV1")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(journal.os, "replace", fail_replace)
    assert not journal.mark_replayed("first", 1)
    assert [entry["attempt_token"] for entry in journal.read_pending_entries()] == ["first"]


@pytest.mark.parametrize("timeout", [False, True])
def test_pipeline_after_manual_export_records_remote_result(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: bool,
) -> None:
    from app.core import settings_store
    from app.db.entities import HighlightCandidate, HighlightEvent, SegmentTask, UploadAttempt, UploadTask
    from app.db.session import get_session
    from app.pipeline.lease import TaskLease
    from app.pipeline.workers.publish import run_publish
    from app.publishing import uploader
    from app.web.routers.clips import confirm_manual_upload

    now = datetime.now(UTC)
    with get_session() as db:
        db.add(HighlightCandidate(id=1, session_id=1, start_ts=now, peak_ts=now, end_ts=now, dedup_hash="remote"))
    clip_id = seed_clip(tmp_path)
    with get_session() as db:
        db.add(HighlightEvent(id=1, candidate_id=1, session_id=1, review_status="approved_solo"))
        db.add(
            SegmentTask(
                id=1,
                segment_id=1,
                session_id=1,
                event_id=1,
                clip_id=clip_id,
                stage="publishing",
                claimed_by="audit",
                lease_token="token",
            )
        )
        db.add(UploadTask(clip_id=clip_id, uploader="manual", status="success"))
    settings_store.set_bool("biliup_enabled", True)
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", "biliup upload {file}")
    calls: list[list[str]] = []

    def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if timeout:
            raise subprocess.TimeoutExpired(command, 1800)
        return subprocess.CompletedProcess(command, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.subprocess, "run", execute)
    run_publish(TaskLease(1, "audit", "token", "publishing"))
    with get_session() as db:
        task = db.get(SegmentTask, 1)
        assert task.stage == ("awaiting_publish_confirmation" if timeout else "completed")
        assert task.lease_token is None
        assert db.exec(select(UploadAttempt)).one().status == ("reconciliation_required" if timeout else "success")
    if timeout:
        confirm_manual_upload(clip_id, submission_id="BV12345678901")
        with get_session() as db:
            assert db.exec(select(UploadAttempt)).one().status == "success"
            assert db.get(SegmentTask, 1).stage == "completed"
    assert len(calls) == 1


def test_reanalysis_database_failure_does_not_block_other_sessions(temp_db: None, tmp_path: Path) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    from app.analysis.reanalysis import process_pending_session_reanalyses, request_session_reanalysis
    from app.db import session as database
    from app.db.entities import (
        AppSetting,
        HighlightCandidate,
        HighlightEvent,
        LiveRoom,
        RawSegment,
        RecordingSession,
        SegmentTask,
    )
    from app.db.session import get_session

    now = datetime.now(UTC)
    with get_session() as db:
        db.add(LiveRoom(id=1, input_url="1", auto_analyze=True))
        for key in (1, 2):
            db.add(RecordingSession(id=key, room_id=1, status="completed"))
            db.add(RawSegment(id=key, session_id=key, seq=0, file_path=str(tmp_path / f"{key}.ts")))
            db.add(
                HighlightCandidate(
                    id=key, session_id=key, start_ts=now, peak_ts=now, end_ts=now, dedup_hash=f"failure-{key}"
                )
            )
            db.flush()
            db.add(HighlightEvent(candidate_id=key, segment_id=key, session_id=key))
    for key in (1, 2):
        assert request_session_reanalysis(key, reason="session_finalized")

    def reject_first_delete(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        # DBAPI 边界注入约束故障，保留真实事务、排队和另一个场次的执行。
        if statement.startswith("DELETE FROM highlight_candidates") and parameters == (1,):
            raise IntegrityError(statement, parameters, ValueError("injected constraint failure"))

    event.listen(database.engine, "before_cursor_execute", reject_first_delete)
    try:
        completed = process_pending_session_reanalyses()
    finally:
        event.remove(database.engine, "before_cursor_execute", reject_first_delete)
    assert [result.session_id for result in completed] == [2]
    with get_session() as db:
        assert db.get(AppSetting, "session_reanalysis:1") is not None
        assert db.get(AppSetting, "session_reanalysis_error:1") is not None
        assert db.exec(select(SegmentTask).where(SegmentTask.session_id == 2)).one().stage == "queued_for_transcription"


def test_publish_claim_is_atomic(temp_db: None, tmp_path: Path) -> None:
    from app.db.entities import UploadTask
    from app.db.session import get_session
    from app.pipeline.workers.publish import _atomic_claim_upload_task

    clip_id = seed_clip(tmp_path)
    with get_session() as db:
        upload = UploadTask(clip_id=clip_id, uploader="biliup")
        db.add(upload)
        db.flush()
        assert upload.id is not None
        assert _atomic_claim_upload_task(db, upload.id, "one") == 1
        assert _atomic_claim_upload_task(db, upload.id, "two") is None


@pytest.mark.parametrize(
    "response,expected,count",
    [
        ("timeout", "reconciliation_required", 1),
        ("nonzero", "reconciliation_required", 1),
        ("success", "success", 1),
        ("missing", "failed_permanent", 1),
        ("refused", "failed_retryable", 3),
    ],
)
def test_upload_persists_attempt_and_retries_only_unsent_results(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    expected: str,
    count: int,
) -> None:
    from app.core import settings_store
    from app.db.entities import UploadAttempt
    from app.db.session import get_session
    from app.publishing import uploader

    calls: list[list[str]] = []

    def execute(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if response == "timeout":
            raise subprocess.TimeoutExpired(command, 1800)
        if response == "missing":
            raise FileNotFoundError("missing executable")
        if response == "refused":
            raise ConnectionRefusedError("Connection refused errno 111")
        return subprocess.CompletedProcess(command, 1 if response == "nonzero" else 0, b"BV12345678901", b"")

    settings_store.set_bool("biliup_enabled", True)
    monkeypatch.setattr(uploader.settings, "upload_max_retries", 2)
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", "biliup upload {file}")
    monkeypatch.setattr(uploader.subprocess, "run", execute)
    task = uploader.enqueue_upload(seed_clip(tmp_path))
    assert task.id is not None
    result = uploader.process_upload_task(task.id)
    assert result.status == expected and len(calls) == count
    with get_session() as db:
        attempts = db.exec(select(UploadAttempt)).all()
        assert len(attempts) == count
        assert attempts[-1].status == expected
        assert len({attempt.publish_generation for attempt in attempts}) == count
    if expected != "failed_retryable":
        assert uploader.process_upload_task(task.id).status == expected
        assert len(calls) == count


@pytest.mark.asyncio
async def test_http_retry_executes_on_event_loop(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.web import main
    from app.web.routers import jobs as router
    from app.web.services import background_jobs as jobs

    manager = jobs.WebJobManager()
    manager.register("audit_job", lambda context, payload: {"executed": True})
    monkeypatch.setattr(router, "web_job_manager", manager)
    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "")
    jobs._save_job(stored_job("http-retry"))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post("/api/jobs/http-retry/retry")
        assert response.status_code == 200
        await asyncio.gather(*list(manager._tasks.values()))
        assert jobs.get_job("http-retry")["status"] == "succeeded"
    finally:
        await manager.stop()


def test_failed_cleanup_keeps_candidate_retryable(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.entities import HighlightCandidate
    from app.db.session import get_session
    from app.pipeline import storage_lifecycle

    now = datetime.now(UTC)
    with get_session() as db:
        db.add(
            HighlightCandidate(
                id=1, session_id=1, start_ts=now, peak_ts=now, end_ts=now, dedup_hash="cleanup", status="rejected"
            )
        )
    seed_clip(tmp_path)
    monkeypatch.setattr(storage_lifecycle, "clips_dir", lambda: tmp_path)
    original_unlink = Path.unlink

    def fail_media(path: Path, missing_ok: bool = False) -> None:
        if path.name == "clip.mp4":
            raise PermissionError("file in use")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_media)
    assert storage_lifecycle.cleanup_rejected_candidates() == 0
    with get_session() as db:
        assert db.get(HighlightCandidate, 1).status == "rejected"
    assert (tmp_path / "clip.mp4").exists()


@pytest.mark.asyncio
async def test_disk_guard_stops_active_ffmpeg_and_releases_watchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import storage_lifecycle
    from app.recording import recorder as module
    from app.sources.bilibili.client import StreamInfo

    finished = asyncio.Event()
    checked = asyncio.Event()
    loop = asyncio.get_running_loop()
    critical = False
    terminated: list[bool] = []

    def disk_critical() -> bool:
        loop.call_soon_threadsafe(checked.set)
        return critical

    class MediaProcess:
        returncode: int | None = None
        stderr = None

        async def wait(self) -> int:
            await finished.wait()
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            terminated.append(True)
            finished.set()

        def kill(self) -> None:
            finished.set()

    async def create_process(*args: str, **kwargs: object) -> MediaProcess:
        return MediaProcess()

    monkeypatch.setattr(storage_lifecycle, "should_stop_recording", disk_critical)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create_process)
    recorder = module.Recorder(db_room_id=1, room_id=1)
    operation = asyncio.create_task(
        recorder._record_once(
            StreamInfo(
                url="https://example.invalid/stream", protocol="flv", format_name="flv", codec_name="avc", quality=10000
            ),
            tmp_path,
        )
    )
    try:
        await asyncio.wait_for(checked.wait(), 2)
        critical = True
        await asyncio.wait_for(operation, 3)
        assert terminated == [True] and recorder._active_process is None
    finally:
        finished.set()
        await asyncio.gather(operation, return_exceptions=True)


def seed_clip(tmp_path: Path, *, status: str = "generated") -> int:
    from app.db.entities import FinalClip
    from app.db.session import get_session

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"audit-only-media-placeholder")
    with get_session() as db:
        clip = FinalClip(candidate_id=1, file_path=str(path), title="audit", description="audit", status=status)
        db.add(clip)
        db.flush()
        assert clip.id is not None
        return clip.id


def stored_job(job_id: str, *, status: str = "failed", timestamp: str | None = None) -> dict[str, Any]:
    now = timestamp or datetime.now(UTC).isoformat()
    return dict(
        version=1,
        id=job_id,
        type="audit_job",
        label="audit",
        owner="local-admin",
        payload={},
        dedup_key=job_id,
        cancellable_while_running=True,
        status=status,
        progress=0,
        message="audit",
        result=None,
        error=None,
        attempt=1,
        created_at=now,
        updated_at=now,
        started_at=None,
        finished_at=None,
        recovered=False,
    )


def test_manual_confirmation_commits_clip_and_pipeline(temp_db: None, tmp_path: Path) -> None:
    from app.db.entities import FinalClip, SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.web.routers.clips import confirm_manual_upload

    clip_id = seed_clip(tmp_path)
    with get_session() as db:
        db.add(SegmentTask(segment_id=1, session_id=1, clip_id=clip_id, stage=TaskStatus.AWAITING_PUBLISH_CONFIRMATION))
    confirm_manual_upload(clip_id)
    with get_session() as db:
        assert db.get(FinalClip, clip_id).status == "published"
        task = db.exec(select(SegmentTask)).one()
        assert task.stage == TaskStatus.COMPLETED and task.completed_at is not None


def test_reanalysis_unlinks_hotspot_and_rebuilds_candidate(temp_db: None, tmp_path: Path) -> None:
    from app.analysis.reanalysis import queue_session_reanalysis
    from app.db.entities import HighlightCandidate, HighlightEvent, HotspotEvent, LiveRoom, RawSegment, RecordingSession
    from app.db.session import get_session

    now = datetime.now(UTC)
    with get_session() as db:
        room = LiveRoom(input_url="1", room_id=1, authorized=True)
        db.add(room)
        db.flush()
        recording = RecordingSession(room_id=room.id)
        db.add(recording)
        db.flush()
        segment = RawSegment(session_id=recording.id, seq=0, file_path=str(tmp_path / "raw.ts"))
        candidate = HighlightCandidate(
            session_id=recording.id, start_ts=now, peak_ts=now, end_ts=now, dedup_hash="audit"
        )
        db.add(segment)
        db.add(candidate)
        db.flush()
        db.add(HighlightEvent(candidate_id=candidate.id, segment_id=segment.id, session_id=recording.id))
        db.add(
            HotspotEvent(
                event_key="audit",
                session_id=recording.id,
                start_ts=now,
                peak_ts=now,
                end_ts=now,
                candidate_id=candidate.id,
            )
        )
        session_id = recording.id
    queue_session_reanalysis(session_id, reason="session_finalized")
    with get_session() as db:
        assert db.exec(select(HotspotEvent)).one().candidate_id is None
        assert not db.exec(select(HighlightCandidate)).all()


def test_maintenance_preserves_published_candidate_assets(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.entities import CandidateStatus, ClipStatus, FinalClip, HighlightCandidate
    from app.db.session import get_session
    from app.pipeline import storage_lifecycle

    now = datetime.now(UTC)
    with get_session() as db:
        candidate = HighlightCandidate(
            session_id=1, start_ts=now, peak_ts=now, end_ts=now, dedup_hash="audit", status=CandidateStatus.REJECTED
        )
        db.add(candidate)
    clip_id = seed_clip(tmp_path, status=ClipStatus.PUBLISHED)
    monkeypatch.setattr(storage_lifecycle, "clips_dir", lambda: tmp_path)
    assert (tmp_path / "clip.mp4").exists()
    count = storage_lifecycle.cleanup_rejected_candidates()
    with get_session() as db:
        assert db.get(FinalClip, clip_id).status == ClipStatus.PUBLISHED
    assert count == 0 and (tmp_path / "clip.mp4").exists()


@pytest.mark.asyncio
async def test_worker_start_replays_journal_before_stale_recovery(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.entities import UploadAttempt, UploadTask
    from app.db.session import get_session
    from app.pipeline.task_worker import TaskWorker
    from app.publishing import journal

    clip_id = seed_clip(tmp_path)
    with get_session() as db:
        upload = UploadTask(clip_id=clip_id, publish_generation=1, status="preparing", claimed_by="crashed-worker")
        db.add(upload)
        db.flush()
        upload_id = upload.id
        attempt = UploadAttempt(
            upload_task_id=upload_id,
            clip_id=clip_id,
            attempt_token="audit-journal",
            publish_generation=1,
            status="in_progress",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(attempt)
        db.flush()
        attempt_id = attempt.id
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    assert journal.write_remote_success("audit-journal", 1, upload_id, clip_id, "BV-audit-placeholder")
    worker = TaskWorker()
    await worker.start()
    await asyncio.sleep(0.05)
    await worker.stop()
    with get_session() as db:
        assert db.get(UploadAttempt, attempt_id).status == "success"
        assert db.get(UploadTask, upload_id).status == "success"
        assert db.get(UploadTask, upload_id).claimed_by is None
    assert not journal.read_pending_entries()


def test_existing_successful_upload_completes_segment_task(temp_db: None, tmp_path: Path) -> None:
    from app.db.entities import HighlightCandidate, HighlightEvent, SegmentTask, UploadAttempt, UploadTask
    from app.db.session import get_session
    from app.pipeline.lease import TaskLease
    from app.pipeline.workers.publish import run_publish

    now = datetime.now(UTC)
    with get_session() as db:
        db.add(HighlightCandidate(id=1, session_id=1, start_ts=now, peak_ts=now, end_ts=now, dedup_hash="audit-reuse"))
    clip_id = seed_clip(tmp_path)
    with get_session() as db:
        db.add(HighlightEvent(id=1, candidate_id=1, session_id=1, review_status="approved_solo"))
        db.add(
            SegmentTask(
                id=1,
                segment_id=1,
                session_id=1,
                event_id=1,
                clip_id=clip_id,
                stage="publishing",
                claimed_by="audit",
                lease_token="audit-token",
            )
        )
        db.add(UploadTask(id=1, clip_id=clip_id, status="success", publish_generation=1))
    with get_session() as db:
        db.add(
            UploadAttempt(
                upload_task_id=1,
                clip_id=clip_id,
                attempt_token="audit-success",
                publish_generation=1,
                status="success",
                remote_id="BV-audit-placeholder",
            )
        )
    run_publish(TaskLease(1, "audit", "audit-token", "publishing"))
    with get_session() as db:
        assert db.get(SegmentTask, 1).stage == "completed"
        assert db.get(SegmentTask, 1).lease_token is None


@pytest.mark.asyncio
async def test_recovery_finds_active_job_behind_500_completed_jobs(temp_db: None) -> None:
    from app.db.entities import AppSetting
    from app.db.session import get_session
    from app.web.services import background_jobs as jobs

    with get_session() as db:
        old = stored_job("older-active", status="queued", timestamp="2026-01-01T00:00:00+00:00")
        db.add(AppSetting(key="web_job:older-active", value=json.dumps(old)))
        for index in range(500):
            value = stored_job(f"recent-{index}", status="succeeded")
            db.add(AppSetting(key=f"web_job:recent-{index}", value=json.dumps(value)))
    manager = jobs.WebJobManager()
    manager.register("audit_job", lambda context, payload: {"recovered": True})
    await manager.start()
    try:
        await asyncio.gather(*list(manager._tasks.values()))
        assert jobs.get_job("older-active")["status"] == "succeeded"
    finally:
        await manager.stop()
