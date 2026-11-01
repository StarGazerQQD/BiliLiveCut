from __future__ import annotations

import subprocess
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from sqlmodel import select

from app.core import settings_store
from app.db import job_store
from app.db.entities import AppSetting, FinalClip, UploadAttempt, UploadTask
from app.db.session import get_session
from app.jobs.context import JobContext
from app.jobs.contracts import JobAttempt, JobData, JobSuperseded, now_iso
from app.jobs.manager import JobManager
from app.pipeline.publish_recovery import recover_publish_state
from app.pipeline.workers.publish import execute_remote_upload, prepare_upload_attempt
from app.publishing import journal, uploader
from app.web.services.job_handlers import _upload_clip, register_job_handlers


def job(clip_id: int) -> JobData:
    now = now_iso()
    return {
        "version": 1,
        "id": uuid4().hex,
        "type": "clip_upload",
        "label": "上传恢复测试",
        "owner": "owner",
        "payload": {"clip_id": clip_id},
        "dedup_key": None,
        "cancellable_while_running": False,
        "status": "queued",
        "progress": 0,
        "message": "等待执行",
        "result": None,
        "error": None,
        "attempt": 1,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "recovered": False,
    }


def seed(tmp_path: Path) -> tuple[int, JobContext]:
    media = tmp_path / "input.mp4"
    media.write_bytes(b"controlled uploader input")
    with get_session() as db:
        clip = FinalClip(candidate_id=1, file_path=str(media), title="标题", description="说明")
        db.add(clip)
        db.flush()
        clip_id = clip.id
    assert clip_id is not None
    registered = job_store.register(job(clip_id))
    token = JobAttempt(registered["id"], 1)
    assert job_store.claim(token) is not None
    return clip_id, JobContext(token.job_id, Event(), token.number)


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("interruption", ["after_success", "new_generation", "new_job_attempt"])
async def test_manager_recovers_only_exact_confirmed_upload_without_repeating_effect(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual: bool, interruption: str
) -> None:
    calls: list[list[str]] = []
    settings_store.set_bool("biliup_enabled", not manual)
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", 'biliup upload "{file}"')

    def external_upload(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.subprocess, "run", external_upload)
    clip_id, context = seed(tmp_path)
    result = _upload_clip(context, {"clip_id": clip_id})
    assert result["status"] == "success"
    assert job_store.get_job(context.job_id)["status"] == "running"
    with get_session() as db:
        assert db.get(AppSetting, f"upload_job:v1:{context.job_id}:1") is not None
        if interruption == "new_generation":
            upload = db.get(UploadTask, result["task_id"])
            upload.publish_generation += 1
            db.add(upload)
    if interruption == "new_job_attempt":
        job_store.fail(context.token, "interrupted")
        job_store.retry(context.job_id, "owner", is_admin=False)
        assert job_store.claim(JobAttempt(context.job_id, 2)) is not None
    manager = JobManager()
    register_job_handlers(manager)
    try:
        await manager.start()
        restored = job_store.get_job(context.job_id)
        assert restored is not None
        assert restored["status"] == ("succeeded" if interruption == "after_success" else "failed")
        assert restored["attempt"] == (2 if interruption == "new_job_attempt" else 1)
        assert restored["owner"] == "owner" and restored["recovered"]
        if interruption == "after_success":
            assert restored["result"] == result
        assert len(calls) == (0 if manual else 1)
    finally:
        await manager.stop()


async def test_receipt_replay_precedes_job_recovery(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_store.set_bool("biliup_enabled", True)
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", 'biliup upload "{file}"')
    calls: list[list[str]] = []

    def external_upload(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.subprocess, "run", external_upload)
    clip_id, context = seed(tmp_path)
    upload = uploader.enqueue_upload(clip_id)
    assert upload.id is not None
    prepared = prepare_upload_attempt(upload.id, "job-worker", job_attempt=context.token)
    assert execute_remote_upload(prepared["attempt_token"], job_attempt=context.token)["outcome"] == "success"
    # 模拟业务数据库尚未提交就中断；启动顺序先回放上传成功，再清算页面作业。
    assert job_store.get_job(context.job_id)["status"] == "running"
    assert recover_publish_state()["journal"] == 1
    manager = JobManager()
    register_job_handlers(manager)
    try:
        await manager.start()
        assert job_store.get_job(context.job_id)["status"] == "succeeded"
        assert len(calls) == 1
    finally:
        await manager.stop()


def test_superseded_job_cannot_prepare_external_upload(temp_db: None, tmp_path: Path) -> None:
    clip_id, context = seed(tmp_path)
    upload = uploader.enqueue_upload(clip_id)
    job_store.fail(context.token, "old execution")
    assert upload.id is not None
    with pytest.raises(JobSuperseded):
        prepare_upload_attempt(upload.id, "late-worker", job_attempt=context.token)
    with get_session() as db:
        assert not db.exec(select(UploadAttempt)).all()
        assert db.get(UploadTask, upload.id).publish_generation == 0
