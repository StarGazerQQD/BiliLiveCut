from __future__ import annotations

import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from os import stat_result
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import Connection, ExecutionContext
from sqlmodel import select

from app.core import settings_store
from app.core.runtime_settings import settings_scope
from app.db import session as database
from app.db.entities import (
    AppSetting,
    CandidateStatus,
    Danmaku,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    SegmentTask,
    TaskStatus,
    Transcript,
    UploadAttempt,
    UploadTask,
)
from app.db.session import get_session
from app.pipeline.approval import approve_event_and_task, render_approval_error
from app.pipeline.lease import TaskLease
from app.pipeline.scheduler import advance_approved, advance_rendered
from app.pipeline.workers.publish import (
    commit_publish_and_advance,
    commit_publish_result,
    execute_remote_upload,
    prepare_publish_attempt,
    prepare_upload_attempt,
    run_publish,
)
from app.pipeline.workers.render import commit_render
from app.publishing import uploader
from tests.approval_fixtures import seed_automatic_task
from tests.unit.pipeline.test_render_commit import seed_render
from tests.unit.pipeline.test_upload_receipts import prepare


def test_receipt_arriving_during_media_preflight_precedes_revoked_send_authorization(
    temp_db: None, pcm_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.APPROVED)
    settings_store.set_bool("biliup_enabled", True)
    settings_store.set_bool("auto_upload", True)
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", 'biliup upload "{file}"')
    _, token, generation = prepare(tmp_path, pipeline_task=task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        lease = TaskLease(task.id, task.claimed_by, task.lease_token, task.stage)
    calls: list[list[str]] = []

    def send(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.subprocess, "run", send)
    result = execute_remote_upload(token, task_lease=lease)
    assert result["outcome"] == "success"
    entered, release = Event(), Event()
    original_stat = Path.stat

    def media_boundary(path: Path, *, follow_symlinks: bool = True) -> stat_result:
        if path == pcm_wav and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", media_boundary)
    with ThreadPoolExecutor(max_workers=1) as executor:
        preparing = executor.submit(prepare_publish_attempt, lease)
        try:
            assert entered.wait(5)
            commit_publish_result(token, generation, result)
            settings_store.set_bool("auto_upload", False)
        finally:
            release.set()
        prepared = preparing.result(timeout=5)
    assert prepared.get("already_success"), prepared
    assert prepared["attempt_token"] == token and len(calls) == 1


@pytest.mark.parametrize("entry", ["pipeline", "upload"])
@pytest.mark.parametrize(
    "change", ["auto_upload", "biliup_enabled", "room", "threshold", "source", "media", "rejected"]
)
def test_remote_success_recovers_without_new_authorization_or_send(
    temp_db: None, pcm_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, change: str
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.APPROVED)
    settings_store.set_bool("biliup_enabled", True)
    settings_store.set_bool("auto_upload", True)
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", 'biliup upload "{file}"')
    upload_id, token, generation = prepare(tmp_path, pipeline_task=task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        lease = TaskLease(task.id, task.claimed_by, task.lease_token, task.stage)
    calls: list[list[str]] = []

    def send(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.subprocess, "run", send)
    result = execute_remote_upload(token, task_lease=lease)
    assert result["outcome"] == "success" and len(calls) == 1
    commit_publish_result(token, generation, result)
    with get_session() as db:
        receipt = db.exec(select(UploadAttempt)).one().model_dump()
        if change in {"auto_upload", "biliup_enabled"}:
            setting = db.get(AppSetting, change)
            setting.value = "false"
            db.add(setting)
        elif change == "source":
            transcript = db.exec(select(Transcript)).one()
            transcript.final_text, transcript.words_json = "changed", "[]"
            db.add(transcript)
        elif change == "media":
            pcm_wav.unlink()
            (tmp_path / "upload.mp4").unlink()
        elif change == "rejected":
            candidate = db.get(HighlightCandidate, task.candidate_id)
            event = db.get(HighlightEvent, task.event_id)
            candidate.status, event.review_status = CandidateStatus.REJECTED, "rejected"
            db.add_all((candidate, event))
        else:
            room = db.exec(select(LiveRoom)).one()
            if change == "room":
                room.auto_upload = False
            else:
                room.auto_publish_threshold = 1.1
            db.add(room)
    if entry == "upload":
        recovered = prepare_upload_attempt(upload_id, lease.worker_id, task_lease=lease)
        assert recovered["already_success"] and recovered["attempt_token"] == token
    run_publish(lease)
    assert len(calls) == 1
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.COMPLETED and task.lease_token is None
        assert db.exec(select(UploadAttempt)).one().model_dump() == receipt
        assert db.get(UploadTask, upload_id).publish_generation == generation
        assert len(db.exec(select(UploadTask)).all()) == 1


@pytest.mark.parametrize("change", ["lease", "candidate"])
@pytest.mark.parametrize("when", ["before_prepare", "before_commit"])
def test_success_receipt_does_not_complete_unrelated_task(
    temp_db: None, pcm_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, when: str
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.APPROVED)
    settings_store.set_bool("biliup_enabled", True)
    settings_store.set_bool("auto_upload", True)
    upload_id, token, generation = prepare(tmp_path, pipeline_task=task_id)
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt)).one()
        attempt.status, attempt.remote_id = "success", "BV12345678901"
        db.add(attempt)
        task = db.get(SegmentTask, task_id)
        lease = TaskLease(task.id, task.claimed_by, task.lease_token, task.stage)
    if when == "before_commit":
        assert prepare_publish_attempt(lease)["already_success"]
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if change == "lease":
            task.lease_token = "new-owner"
        else:
            task.candidate_id = 99999
        db.add(task)
    if when == "before_prepare":
        assert "error" in prepare_publish_attempt(lease)
        assert "error" in prepare_upload_attempt(upload_id, lease.worker_id, task_lease=lease)
    else:
        commit_publish_and_advance(lease, token, generation, {"outcome": "success"})
    with get_session() as db:
        assert db.get(SegmentTask, task_id).stage == TaskStatus.PUBLISHING
        assert db.exec(select(UploadAttempt)).one().attempt_token == token
        assert db.get(UploadTask, upload_id).publish_generation == generation


def test_publish_receipt_commit_does_not_overwrite_concurrent_cancellation(
    temp_db: None, pcm_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.APPROVED)
    settings_store.set_bool("biliup_enabled", True)
    settings_store.set_bool("auto_upload", True)
    _, token, generation = prepare(tmp_path, pipeline_task=task_id)
    with get_session() as db:
        attempt = db.exec(select(UploadAttempt)).one()
        attempt.status, attempt.remote_id = "success", "BV12345678901"
        db.add(attempt)
        task = db.get(SegmentTask, task_id)
        lease = TaskLease(task.id, task.claimed_by, task.lease_token, task.stage)
    cancellations: list[bool] = []

    def cancel_from_other_connection(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        if cancellations or not statement.startswith("SELECT segment_tasks."):
            return
        with closing(sqlite3.connect(database.engine.url.database, timeout=0)) as other:
            try:
                other.execute(
                    "UPDATE segment_tasks SET stage='cancelled', claimed_by=NULL, lease_token=NULL WHERE id=?",
                    (task_id,),
                )
                other.commit()
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                cancellations.append(False)
            else:
                cancellations.append(True)

    sqlalchemy_event.listen(database.engine, "after_cursor_execute", cancel_from_other_connection)
    try:
        commit_publish_and_advance(lease, token, generation, {"outcome": "success"})
    finally:
        sqlalchemy_event.remove(database.engine, "after_cursor_execute", cancel_from_other_connection)
    assert len(cancellations) == 1
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == (TaskStatus.CANCELLED if cancellations[0] else TaskStatus.COMPLETED)
        assert task.lease_token is None
        assert db.exec(select(UploadAttempt)).one().remote_id == "BV12345678901"


@pytest.mark.parametrize("entry", ["approve", "render", "publish"])
@pytest.mark.parametrize("change", ["missing", "provenance", "revision", "text", "bounds", "policy", "media"])
def test_automatic_entries_revalidate_current_evidence_even_after_approval(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, entry: str, change: str
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.APPROVED)
    settings_store.set_bool("biliup_enabled", True)
    settings_store.set_bool("auto_upload", True)
    with settings_scope():
        with get_session() as db:
            task = db.get(SegmentTask, task_id)
            event = db.get(HighlightEvent, task.event_id)
            candidate = db.get(HighlightCandidate, task.candidate_id)
            event_id = event.id
            if change in {"missing", "provenance", "revision"}:
                features = json.loads(event.features_json)
                if change == "missing":
                    features.pop("editorial_review")
                elif change == "provenance":
                    features["editorial_review"].pop("provenance")
                else:
                    features["editorial_review"]["revision"] = "changed"
                event.features_json = json.dumps(features)
                db.add(event)
            elif change == "text":
                text = db.exec(select(Transcript)).one()
                text.final_text = "来源转写已重新生成"
                text.words_json = "[]"
                db.add(text)
            elif change == "bounds":
                candidate.start_ts = candidate.end_ts - timedelta(seconds=0.01)
                db.add(candidate)
            elif change == "policy":
                row = db.get(AppSetting, "llm_providers")
                providers = json.loads(row.value)
                providers[0]["model"] = "changed-after-task-start"
                row.value = json.dumps(providers)
                db.add(row)
            else:
                pcm_wav.unlink()
            if entry == "publish":
                task.stage = TaskStatus.RENDERED
                db.add(task)
        if entry == "approve":
            assert not approve_event_and_task(task_id=task_id, event_id=event_id)
        elif entry == "render":
            advance_approved()
        else:
            advance_rendered()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == (TaskStatus.AWAITING_PUBLISH_CONFIRMATION if entry == "publish" else TaskStatus.APPROVED)
        if entry == "render":
            assert task.last_error


@pytest.mark.parametrize("field", ["candidate_id", "event_id", "session_id", "segment_id"])
def test_approval_rejects_mismatched_task_without_partial_updates(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        event = db.get(HighlightEvent, task.event_id)
        candidate = db.get(HighlightCandidate, task.candidate_id)
        event_id, candidate_id = event.id, candidate.id
        setattr(task, field, 99999)
        db.add(task)
        before = (task.stage, task.event_id, task.candidate_id, event.review_status, event.review_by, candidate.status)
    assert not approve_event_and_task(task_id=task_id, event_id=event_id, source="human", approved_by="reviewer")
    with get_session() as db:
        task, event, candidate = (
            db.get(SegmentTask, task_id),
            db.get(HighlightEvent, event_id),
            db.get(HighlightCandidate, candidate_id),
        )
        assert (
            task.stage,
            task.event_id,
            task.candidate_id,
            event.review_status,
            event.review_by,
            candidate.status,
        ) == before


def test_human_reapproval_preserves_machine_record_and_active_lease(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.RENDERING)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.lease_token, task.claimed_by = "lease", "worker"
        db.add(task)
        event = db.get(HighlightEvent, task.event_id)
        event_id, machine = event.id, event.features_json
    assert approve_event_and_task(task_id=task_id, event_id=event_id, source="human", approved_by="reviewer")
    with get_session() as db:
        task, event = db.get(SegmentTask, task_id), db.get(HighlightEvent, event_id)
        assert (task.stage, task.lease_token, task.claimed_by) == (TaskStatus.RENDERING, "lease", "worker")
        assert event.review_by == "reviewer" and event.features_json == machine


def test_reapproval_after_real_render_preserves_clip_and_completed_state(temp_db: None, tmp_path: Path) -> None:
    lease, result = seed_render(tmp_path)
    commit_render(lease, result, 1)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        candidate = db.get(HighlightCandidate, task.candidate_id)
        assert candidate.status == CandidateStatus.CLIPPED
        original = (task.stage, task.clip_id, task.completed_at, task.stage_key)
        event_id = task.event_id
        clips = [row.id for row in db.exec(select(FinalClip)).all()]
    for _ in range(2):
        assert approve_event_and_task(task_id=lease.task_id, event_id=event_id, source="human", approved_by="reviewer")
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert (task.stage, task.clip_id, task.completed_at, task.stage_key) == original
        assert db.get(HighlightCandidate, task.candidate_id).status == CandidateStatus.CLIPPED
        assert [row.id for row in db.exec(select(FinalClip)).all()] == clips


@pytest.mark.parametrize("actor", ["auto", ""])
def test_human_fallback_approval_cannot_be_mistaken_for_machine_approval(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, actor: str
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        event = db.get(HighlightEvent, task.event_id)
        event_id = event.id
        features = json.loads(event.features_json)
        features["editorial_review"]["execution"]["result"]["verdict"] = "needs_review"
        event.features_json = json.dumps(features)
        original = event.features_json
        db.add(event)
    assert not approve_event_and_task(task_id=task_id, event_id=event_id)
    assert approve_event_and_task(task_id=task_id, event_id=event_id, source="human", approved_by=actor)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        event = db.get(HighlightEvent, event_id)
        assert render_approval_error(db, task) is None
        assert event.review_by == ("human:auto" if actor == "auto" else "manual")
        assert event.features_json == original


@pytest.mark.parametrize("change", ["auto_upload", "biliup_enabled", "room", "threshold", "source"])
def test_queued_upload_rechecks_authorization_inside_old_task_snapshot(
    temp_db: None, pcm_wav: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    task_id = seed_automatic_task(pcm_wav, monkeypatch, stage=TaskStatus.PUBLISHING)
    settings_store.set_bool("biliup_enabled", True)
    settings_store.set_bool("auto_upload", True)
    upload_id, token, _ = prepare(tmp_path, pipeline_task=task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        lease = TaskLease(task.id, task.claimed_by, task.lease_token, task.stage)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("撤销授权后不能发送外部上传请求")

    monkeypatch.setattr(uploader.subprocess, "run", forbidden)
    with settings_scope():
        with get_session() as db:
            if change in {"auto_upload", "biliup_enabled"}:
                row = db.get(AppSetting, change)
                row.value = "false"
                db.add(row)
            elif change == "source":
                text = db.exec(select(Transcript)).one()
                text.final_text, text.words_json = "changed", "[]"
                db.add(text)
            else:
                room = db.exec(select(LiveRoom)).one()
                if change == "room":
                    room.auto_upload = False
                else:
                    room.auto_publish_threshold = 1.1
                db.add(room)
        result = execute_remote_upload(token, task_lease=lease)
    assert result["outcome"] == "failed_permanent" and result["request_may_have_been_sent"] is False
    with get_session() as db:
        assert db.get(UploadTask, upload_id).attempts == 0


def test_manual_upload_requires_current_biliup_but_not_automatic_switch(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_store.set_bool("biliup_enabled", True)
    settings_store.set_bool("auto_upload", False)
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", 'biliup upload "{file}"')
    _, token, _ = prepare(tmp_path)
    calls: list[list[str]] = []

    def send(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.subprocess, "run", send)
    assert execute_remote_upload(token)["outcome"] == "success"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["none", "text", "danmaku"])
def test_segment_approval_rebuilds_complete_source_and_detects_edits(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from app.pipeline.workers.analyze import score_segment_direct
    from tests.unit.test_editorial_review import START, _seed, _service

    _, segment_id = _seed(pcm_wav)
    with get_session() as db:
        room = db.exec(select(LiveRoom)).one()
        room.auto_approve = False
        db.add(room)
        db.add(Danmaku(session_id=1, room_id=9541, ts=START + timedelta(seconds=4), content="规则讲明白了"))
    calls = _service(monkeypatch, "recommend")
    candidate = score_segment_direct(segment_id)
    assert candidate is not None
    with get_session() as db:
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate.id)).one()
        event_id, machine = event.id, event.features_json
        features = json.loads(machine)
        assert features["editorial_review"]["input"]["target"]["kind"] == "segment"
        assert features["timeline"]["representative_danmaku"]
    requests = len(calls)
    assert approve_event_and_task(task_id=None, event_id=event_id)
    with get_session() as db:
        if change == "text":
            transcript = db.exec(select(Transcript)).one()
            transcript.final_text = "主播开始解释另一场比赛。"
            transcript.words_json = json.dumps([{"w": transcript.final_text, "start": 2.0, "end": 6.0}])
            db.add(transcript)
        elif change == "danmaku":
            comment = db.exec(select(Danmaku)).one()
            comment.content = "弹幕内容已变化"
            db.add(comment)
    assert approve_event_and_task(task_id=None, event_id=event_id) is (change == "none")
    assert len(calls) == requests
    with get_session() as db:
        assert db.get(HighlightEvent, event_id).features_json == machine
        assert len(db.exec(select(HighlightCandidate)).all()) == 1
        assert len(db.exec(select(HighlightEvent)).all()) == 1


def test_manual_upload_obeys_biliup_revocation_inside_old_snapshot(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_store.set_bool("biliup_enabled", True)
    _, token, _ = prepare(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("人工上传也必须遵守当前全局上传授权")

    monkeypatch.setattr(uploader.subprocess, "run", forbidden)
    with settings_scope():
        with get_session() as db:
            row = db.get(AppSetting, "biliup_enabled")
            row.value = "false"
            db.add(row)
        result = execute_remote_upload(token)
    assert result["outcome"] == "failed_permanent" and result["request_may_have_been_sent"] is False
