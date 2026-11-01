from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest
from sqlmodel import select

from app.core import settings_store
from app.db.entities import (
    AppSetting,
    FinalClip,
    HighlightEvent,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
    UploadAttempt,
    UploadStatus,
    UploadTask,
)
from app.db.session import get_session
from app.pipeline.lease import TaskLease
from app.pipeline.publish_recovery import recover_publish_results
from app.pipeline.stale_recovery import recover_stale_upload_attempts
from app.pipeline.workers.publish import commit_publish_result, execute_remote_upload, prepare_upload_attempt
from app.publishing import journal, uploader
from app.publishing.execution import read_execution
from app.publishing.heartbeat import UploadHeartbeat, heartbeat_key


def prepare(tmp_path: Path, *, pipeline_task: int | None = None) -> tuple[int, str, int]:
    path = tmp_path / "upload.mp4"
    path.write_bytes(b"controlled external upload input")
    lease = None
    with get_session() as db:
        stage_task = db.get(SegmentTask, pipeline_task) if pipeline_task is not None else None
        clip = FinalClip(
            candidate_id=stage_task.candidate_id if stage_task else 1,
            file_path=str(path),
            title="标题",
            description="说明",
        )
        db.add(clip)
        db.flush()
        task = UploadTask(clip_id=clip.id, status="queued", uploader="biliup")
        db.add(task)
        db.flush()
        task_id = task.id
        if stage_task is not None:
            stage_task.clip_id = clip.id
            stage_task.stage = TaskStatus.PUBLISHING
            stage_task.claimed_by = "test-worker"
            stage_task.lease_token = "original-lease"
            db.add(stage_task)
            db.flush()
            lease = TaskLease(stage_task.id, "test-worker", "original-lease", TaskStatus.PUBLISHING)
    assert task_id is not None
    prepared = prepare_upload_attempt(task_id, "test-worker", task_lease=lease)
    assert prepared["ready"]
    return task_id, prepared["attempt_token"], prepared["publish_generation"]


@pytest.mark.parametrize("replay", [False, True])
def test_success_receipt_exists_before_database_commit(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replay: bool
) -> None:
    settings_store.set_bool("biliup_enabled", True)
    monkeypatch.setattr(
        uploader.settings, "biliup_upload_cmd", 'biliup upload "{file}" --title "{title}" --desc "{desc}"'
    )
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    calls: list[list[str]] = []

    def upload(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(args, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.subprocess, "run", upload)
    task_id, token, generation = prepare(tmp_path)
    result = execute_remote_upload(token)
    assert result["outcome"] == "success" and len(calls) == 1
    entries = journal.read_pending_entries()
    assert len(entries) == 1 and entries[0]["attempt_token"] == token
    with get_session() as db:
        assert db.get(UploadTask, task_id).status == UploadStatus.UPLOADING
    if replay:
        assert recover_publish_results() == 1
        assert recover_publish_results() == 0
    else:
        commit_publish_result(token, generation, result)
    with get_session() as db:
        task = db.get(UploadTask, task_id)
        assert task is not None and task.status == UploadStatus.SUCCESS and task.remote_id == "BV12345678901"
    assert not journal.read_pending_entries()
    assert execute_remote_upload(token)["already_completed"] is True
    assert len(calls) == 1


def test_fresh_heartbeat_protects_long_upload_but_expiry_requires_reconciliation(temp_db: None, tmp_path: Path) -> None:
    task_id, token, generation = prepare(tmp_path)
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        attempt.status = "in_progress"
        attempt.started_at = datetime.now(UTC) - timedelta(hours=2)
        db.add(attempt)
        binding = read_execution(db, attempt)
    with UploadHeartbeat(binding):
        assert recover_stale_upload_attempts() == 0
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        row = db.get(AppSetting, heartbeat_key(attempt))
        assert row is not None
        row.updated_at = datetime.now(UTC) - timedelta(hours=1)
        db.add(row)
    assert recover_stale_upload_attempts() == 1
    with get_session() as db:
        assert db.get(UploadTask, task_id).status == UploadStatus.RECONCILIATION_REQUIRED
    assert not prepare_upload_attempt(task_id, "another-worker").get("ready")


def test_old_generation_receipt_cannot_complete_new_upload(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    task_id, token, generation = prepare(tmp_path)
    with get_session() as db:
        task = db.get(UploadTask, task_id)
        assert task is not None
        task.publish_generation += 1
        task.status = "queued"
        db.add(task)
        clip_id = task.clip_id
    assert journal.write_remote_success(token, generation, task_id, clip_id, "BV_old")
    assert recover_publish_results() == 1
    with get_session() as db:
        task = db.get(UploadTask, task_id)
        assert task is not None and task.status == "queued" and task.remote_id is None
        assert db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one().remote_id == "BV_old"


def test_normal_commit_keeps_conflicting_receipt_for_reconciliation(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    task_id, token, generation = prepare(tmp_path)
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        attempt.status = UploadStatus.SUCCESS
        attempt.remote_id = "BV_original"
        db.add(attempt)
        clip_id = attempt.clip_id
    assert journal.write_remote_success(token, generation, task_id, clip_id, "BV_late")
    commit_publish_result(token, generation, {"outcome": "success", "remote_id": "BV_late"})
    assert journal.read_pending_entries()[0]["remote_id"] == "BV_late"
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        assert attempt.remote_id == "BV_original"


@pytest.mark.parametrize("change", ["title", "content", "rejected", "generation", "worker"])
def test_prepared_upload_revalidates_exact_request_before_sending(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    settings_store.set_bool("biliup_enabled", True)
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", 'biliup upload "{file}"')
    task_id, token, _ = prepare(tmp_path)
    with get_session() as db:
        task = db.get(UploadTask, task_id)
        clip = db.get(FinalClip, task.clip_id)
        if change == "title":
            clip.title = "变更后的标题"
        elif change == "content":
            path = Path(clip.file_path)
            path.write_bytes(b"x" * path.stat().st_size)
        elif change == "rejected":
            clip.status = "rejected"
        elif change == "generation":
            task.publish_generation += 1
            task.status = "queued"
        else:
            task.claimed_by = "replacement-worker"
        db.add(clip)
        db.add(task)

    def unexpected_upload(*args: object, **kwargs: object) -> None:
        pytest.fail("失效准备记录不得触发外部命令")

    monkeypatch.setattr(uploader.subprocess, "run", unexpected_upload)
    result = execute_remote_upload(token)
    assert result["outcome"] == "failed_permanent" and not result["request_may_have_been_sent"]
    with get_session() as db:
        task = db.get(UploadTask, task_id)
        assert task.attempts == 0
        if change == "generation":
            assert task.status == "queued" and task.publish_generation == 2
        if change == "worker":
            assert task.claimed_by == "replacement-worker"


@pytest.mark.parametrize("change", ["lease", "review", "missing_credential"])
def test_pipeline_upload_rechecks_source_and_full_lease(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pcm_wav: Path,
    change: str,
) -> None:
    settings_store.set_bool("biliup_enabled", True)
    settings_store.set_bool("auto_upload", True)
    from tests.approval_fixtures import seed_automatic_task

    pipeline_task = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.PUBLISHING)
    task_id, token, _ = prepare(tmp_path, pipeline_task=pipeline_task)
    with get_session() as db:
        task = db.exec(select(SegmentTask)).one()
        lease = TaskLease(task.id, task.claimed_by, task.lease_token, task.stage)
        if change == "lease":
            task.lease_token = "replacement-lease"
            db.add(task)
        elif change == "review":
            event = db.get(HighlightEvent, task.event_id)
            event.review_status = ReviewStatus.REJECTED
            db.add(event)
    result = execute_remote_upload(token, task_lease=None if change == "missing_credential" else lease)
    assert result["outcome"] == "failed_permanent" and not result["request_may_have_been_sent"]
    with get_session() as db:
        assert db.get(UploadTask, task_id).attempts == 0


def test_late_failure_cannot_change_successful_receipt(
    temp_db: None,
    tmp_path: Path,
) -> None:
    task_id, token, generation = prepare(tmp_path)
    finished = datetime.now(UTC) - timedelta(hours=1)
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        attempt.status, attempt.remote_id, attempt.finished_at = "success", "BV_original", finished
        attempt.remote_url = "https://www.bilibili.com/video/BV_original"
        db.add(attempt)
        task = db.get(UploadTask, task_id)
        task.status, task.remote_id, task.updated_at = "success", "BV_original", finished
        db.add(task)
    commit_publish_result(
        token,
        generation,
        {
            "outcome": "failed_retryable",
            "error_type": "timeout",
            "error_message": "late error",
            "remote_id": "BV_wrong",
        },
    )
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        assert attempt.status == "success" and attempt.remote_id == "BV_original"
        assert attempt.finished_at.replace(tzinfo=UTC) == finished
        assert attempt.error_type is None and attempt.error_message is None
        task = db.get(UploadTask, task_id)
        assert task.status == "success" and task.last_error is None
        assert task.updated_at.replace(tzinfo=UTC) == finished


def test_replay_same_id_preserves_known_url_when_journal_lacks_it(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    task_id, token, generation = prepare(tmp_path)
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        attempt.status, attempt.remote_id = "success", "BV_original"
        attempt.remote_url = "https://www.bilibili.com/video/BV_original"
        db.add(attempt)
        clip_id = attempt.clip_id
    assert journal.write_remote_success(token, generation, task_id, clip_id, "BV_original")
    assert recover_publish_results() == 0
    assert journal.read_pending_entries() == []
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        assert attempt.remote_url == "https://www.bilibili.com/video/BV_original"


def test_heartbeat_exit_retains_execution_until_sqlite_writer_returns(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import event

    from app.publishing import heartbeat as heartbeat_module

    _, token, _ = prepare(tmp_path)
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
        attempt.status = "in_progress"
        db.add(attempt)
        binding = read_execution(db, attempt)
        engine = db.get_bind()
    monkeypatch.setattr(heartbeat_module, "_POLL_S", 0.02)
    heartbeat = UploadHeartbeat(binding)
    entered = Event()

    def observe(
        connection: object, cursor: object, statement: str, parameters: object, context: object, many: bool
    ) -> None:
        if current_thread() is heartbeat.thread and statement == "BEGIN IMMEDIATE":
            entered.set()

    event.listen(engine, "before_cursor_execute", observe)
    heartbeat.__enter__()
    closer = Thread(target=heartbeat.__exit__)
    try:
        with get_session() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            assert entered.wait(2)
            closer.start()
            closer.join(timeout=0.1)
            assert closer.is_alive() and heartbeat.thread.is_alive()
        closer.join(timeout=3)
        assert not closer.is_alive() and not heartbeat.thread.is_alive()
    finally:
        heartbeat.__exit__()
        if closer.ident is not None:
            closer.join(timeout=3)
        event.remove(engine, "before_cursor_execute", observe)


def test_actual_process_exit_after_receipt_replays_without_second_command(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_store.set_bool("biliup_enabled", True)
    task_id, token, _ = prepare(tmp_path)
    root = Path(__file__).resolve().parents[3]
    actor = root / "tests" / "integration" / "upload_crash_actor.py"
    count = tmp_path / "command-count.txt"
    command = f'"{sys.executable}" "{actor}" upload "{count}" "{{file}}"'
    directory = tmp_path / "journal"
    monkeypatch.setattr(journal, "_JOURNAL_DIR", directory)
    completed = subprocess.run(
        [sys.executable, str(actor), "execute", token],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root), "BLC_JOURNAL_DIR": str(directory), "BILIUP_UPLOAD_CMD": command},
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 29, completed.stderr.decode(errors="replace")
    with get_session() as db:
        assert db.get(UploadTask, task_id).status == UploadStatus.UPLOADING
    assert len(journal.read_pending_entries()) == 1
    assert recover_publish_results() == 1
    assert recover_publish_results() == 0
    assert execute_remote_upload(token)["already_completed"]
    assert count.read_text(encoding="utf-8").splitlines() == ["sent"]


def test_replay_preserves_original_success_time_for_rate_limit(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    task_id, token, generation = prepare(tmp_path)
    finished = datetime.now(UTC) - timedelta(hours=2)
    with get_session() as db:
        task = db.get(UploadTask, task_id)
        clip_id = task.clip_id
    for index in range(2):
        assert journal.write_remote_success(
            token, generation, task_id, clip_id, "BV_confirmed", finished_at=finished.isoformat()
        )
        assert recover_publish_results() == (1 if index == 0 else 0)
        with get_session() as db:
            attempt = db.exec(select(UploadAttempt).where(UploadAttempt.attempt_token == token)).one()
            task = db.get(UploadTask, task_id)
            assert attempt.finished_at.replace(tzinfo=UTC) == task.updated_at.replace(tzinfo=UTC) == finished
        assert uploader._recent_success_count(hours=1) == 0
