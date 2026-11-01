"""持久化 Web 后台作业的进度、取消、重试和恢复测试。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.job_store import get_job
from app.jobs.context import JobContext
from app.jobs.contracts import JobAttempt, JobData, decode_job
from app.web.services.background_jobs import WebJobManager


@pytest.mark.parametrize("cancellable", [False, True])
async def test_cancelled_async_wrapper_cannot_release_slot_while_handler_thread_runs(
    temp_db: None, cancellable: bool
) -> None:
    manager = WebJobManager(max_concurrency=1)
    entered, release, second_entered = Event(), Event(), Event()

    def first(context: JobContext, payload: dict[str, object]) -> None:
        entered.set()
        assert release.wait(5)
        context.check_cancelled()

    def second(context: JobContext, payload: dict[str, object]) -> None:
        second_entered.set()

    manager.register("first", first)
    manager.register("second", second)
    job = await manager.enqueue("first", {}, label="first", owner="test", cancellable_while_running=cancellable)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        execution = manager._tasks[job["id"]]
        execution.cancel()
        waiting = await manager.enqueue("second", {}, label="second", owner="test")
        await asyncio.sleep(0.05)
        assert not execution.done() and not second_entered.is_set()
        assert get_job(waiting["id"])["status"] == "queued"
        release.set()
        await _wait_for_status(job["id"], {"cancelled" if cancellable else "succeeded"})
        await _wait_for_status(waiting["id"], {"succeeded"})
        assert second_entered.is_set()
    finally:
        release.set()
        await manager.stop()


@pytest.mark.parametrize("cancellable", [False, True])
async def test_shutdown_timeout_keeps_handler_and_blocks_new_work(temp_db: None, cancellable: bool) -> None:
    manager = WebJobManager(max_concurrency=1)
    await manager.start()
    entered = Event()
    release = Event()

    def handler(context: JobContext, payload: dict[str, object]) -> dict[str, bool]:
        entered.set()
        assert release.wait(5)
        return {"finished": True}

    manager.register("shutdown_test", handler)
    job = await manager.enqueue(
        "shutdown_test", {}, label="shutdown", owner="tester", cancellable_while_running=cancellable
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(RuntimeError, match="未确认退出"):
            await manager.stop(timeout_s=0.01)
        assert manager._stop_task is not None and not manager._stop_task.done()
        if cancellable:
            assert manager._cancel_events[job["id"]].is_set()
            await _wait_for_status(job["id"], {"cancelling"})
        state = get_job(job["id"])
        assert state is not None
        assert state["status"] == ("cancelling" if cancellable else "running")
        assert job["id"] in manager._tasks
        assert not manager._tasks[job["id"]].cancelled()
        assert job["id"] in manager._cancel_events
        with pytest.raises(RuntimeError, match="不能重新启动"):
            await manager.start()
        with pytest.raises(RuntimeError, match="正在关闭"):
            await manager.enqueue("shutdown_test", {}, label="late", owner="tester")
        with pytest.raises(RuntimeError, match="正在关闭"):
            await manager.retry(job["id"], "tester")
        release.set()
        result = await _wait_for_status(job["id"], {"cancelled" if cancellable else "succeeded"})
        assert result["finished_at"] is not None
    finally:
        release.set()
        await manager.stop()


async def test_background_failure_persists_safe_exception_chain(temp_db: None) -> None:
    manager = WebJobManager()
    await manager.start()

    def handler(context: JobContext, payload: dict[str, Any]) -> None:
        try:
            raise TimeoutError("https://api.example/private?token=private-value")
        except TimeoutError as exc:
            raise RuntimeError("Authorization: Bearer private-auth") from exc

    manager.register("broken", handler)
    job = await manager.enqueue("broken", {}, label="broken", owner="test")
    result = await _wait_for_status(job["id"], {"failed"})
    await manager.stop()
    assert "RuntimeError" in result["error"] and "TimeoutError" in result["error"]
    assert "private-" not in result["error"]


@pytest.mark.parametrize("status", ["failed", "skipped", "reconciliation_required", "success"])
async def test_upload_job_reflects_actual_upload_terminal_status(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    import subprocess

    from app.core import settings_store
    from app.db.entities import FinalClip, UploadTask
    from app.db.session import get_session
    from app.publishing import journal, uploader

    media = tmp_path / "upload.mp4"
    if status != "skipped":
        media.write_bytes(b"controlled upload boundary")
    with get_session() as db:
        db.add(FinalClip(id=1, candidate_id=1, file_path=str(media), title="标题", description="说明"))
        db.flush()
        db.add(UploadTask(id=55, clip_id=1, uploader="biliup", status="queued"))
    settings_store.set_bool("biliup_enabled", True)
    monkeypatch.setattr(journal, "_JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(uploader.settings, "biliup_upload_cmd", 'biliup upload "{file}"')

    def external_upload(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if status == "failed":
            raise PermissionError("token=private-error")
        if status == "reconciliation_required":
            raise subprocess.TimeoutExpired(args, 1800)
        assert status == "success"
        return subprocess.CompletedProcess(args, 0, b"BV12345678901", b"")

    monkeypatch.setattr(uploader.subprocess, "run", external_upload)
    manager = WebJobManager()
    await manager.start()
    job = await manager.enqueue(
        "clip_upload", {"clip_id": 1}, label="upload", owner="test", cancellable_while_running=False
    )
    result = await _wait_for_status(job["id"], {"failed", "succeeded"})
    await manager.stop()
    assert result["status"] == ("succeeded" if status == "success" else "failed")
    if status != "success":
        assert "upload_task_id=55" in result["error"] and status in result["error"]
        assert "private-error" not in result["error"]


async def test_failed_job_status_write_is_observed_and_saved_once(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db import job_store

    manager = WebJobManager()
    await manager.start()
    manager.register("value", lambda context, payload: {})
    failures = 0

    def fail_running(attempt: JobAttempt) -> JobData:
        nonlocal failures
        failures += 1
        raise OSError("database state unavailable")

    monkeypatch.setattr(job_store, "claim", fail_running)
    job = await manager.enqueue("value", {}, label="test", owner="test")
    result = await _wait_for_status(job["id"], {"failed"})
    await manager.stop()
    assert failures == 1 and "OSError" in result["error"]
    assert not manager._cancel_events


def _stored_job(**overrides: Any) -> dict[str, Any]:
    """构造当前且唯一的持久化 Web 作业格式。"""
    now = datetime.now(UTC).isoformat()
    value: dict[str, Any] = {
        "version": 1,
        "id": "stored-job",
        "type": "test_job",
        "label": "stored",
        "owner": "tester",
        "payload": {},
        "dedup_key": None,
        "cancellable_while_running": True,
        "status": "queued",
        "progress": 0,
        "message": "queued",
        "result": None,
        "error": None,
        "attempt": 1,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "recovered": False,
    }
    value.update(overrides)
    return value


async def _wait_for_status(job_id: str, statuses: set[str], timeout_s: float = 5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = get_job(job_id)
        if job is not None and job["status"] in statuses:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach {statuses}")


@pytest.mark.asyncio
async def test_background_job_persists_progress_and_result(temp_db: None) -> None:
    """作业立即返回并在后台持久化进度与结果。"""
    manager = WebJobManager(max_concurrency=1)
    await manager.start()

    def handler(context: JobContext, payload: dict[str, Any]) -> dict[str, Any]:
        context.report(45, "half")
        return {"value": payload["value"] * 2}

    manager.register("test_success", handler)
    job = await manager.enqueue("test_success", {"value": 21}, label="test", owner="tester")
    completed = await _wait_for_status(job["id"], {"succeeded"})
    await manager.stop()

    assert completed["progress"] == 100
    assert completed["result"] == {"value": 42}
    assert completed["started_at"] is not None
    assert completed["finished_at"] is not None


@pytest.mark.asyncio
async def test_background_job_cancel_stops_running_handler(temp_db: None) -> None:
    """运行中的处理器能收到取消信号并进入 cancelled。"""
    manager = WebJobManager(max_concurrency=1)
    await manager.start()

    def handler(context: JobContext, _payload: dict[str, Any]) -> None:
        for index in range(100):
            context.report(index, "working")
            time.sleep(0.02)
            context.check_cancelled()

    manager.register("test_cancel", handler)
    job = await manager.enqueue("test_cancel", {}, label="cancel", owner="tester")
    await _wait_for_status(job["id"], {"running"})
    manager.cancel(job["id"], "tester")
    cancelled = await _wait_for_status(job["id"], {"cancelled"})
    await manager.stop()

    assert cancelled["finished_at"] is not None
    assert "取消" in cancelled["message"]


@pytest.mark.asyncio
async def test_background_job_can_retry_after_failure(temp_db: None) -> None:
    """失败作业保留错误并可原地重试。"""
    manager = WebJobManager(max_concurrency=1)
    await manager.start()
    calls = 0

    def handler(_context: JobContext, _payload: dict[str, Any]) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    manager.register("test_retry", handler)
    job = await manager.enqueue("test_retry", {}, label="retry", owner="tester")
    failed = await _wait_for_status(job["id"], {"failed"})
    assert failed["error"] == "RuntimeError: boom"
    await manager.retry(job["id"], "tester")
    completed = await _wait_for_status(job["id"], {"succeeded"})
    await manager.stop()

    assert completed["attempt"] == 2
    assert completed["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_background_job_deduplicates_active_operation(temp_db: None) -> None:
    """相同业务键的活动作业不会因重复点击而创建两份。"""
    manager = WebJobManager(max_concurrency=1)
    await manager.start()

    def handler(context: JobContext, _payload: dict[str, Any]) -> None:
        time.sleep(0.2)
        context.check_cancelled()

    manager.register("test_dedup", handler)
    first = await manager.enqueue("test_dedup", {}, label="first", owner="tester", dedup_key="same")
    second = await manager.enqueue("test_dedup", {}, label="second", owner="tester", dedup_key="same")
    await _wait_for_status(first["id"], {"succeeded"})
    await manager.stop()

    assert second["id"] == first["id"]


@pytest.mark.asyncio
async def test_background_job_recovers_safe_running_operation(temp_db: None) -> None:
    """服务重启后安全的渲染类作业会重新排队执行。"""
    from app.db.entities import AppSetting
    from app.db.session import get_session

    stored = _stored_job(
        id="recover-job",
        type="test_recover",
        label="recover",
        owner="tester",
        payload={"value": 7},
        status="running",
        progress=50,
        message="interrupted process",
    )
    with get_session() as db:
        db.add(AppSetting(key="web_job:recover-job", value=json.dumps(stored)))

    manager = WebJobManager(max_concurrency=1)
    manager.register("test_recover", lambda _context, payload: {"value": payload["value"]})
    await manager.start()
    completed = await _wait_for_status("recover-job", {"succeeded"})
    await manager.stop()

    assert completed["result"] == {"value": 7}
    assert completed["recovered"] is True


@pytest.mark.asyncio
async def test_background_job_does_not_replay_cancelling_operation(temp_db: None) -> None:
    """服务重启后保留原取消意图，不重新执行正在取消的作业。"""
    from app.db.entities import AppSetting
    from app.db.session import get_session

    stored = _stored_job(
        id="cancelling-job",
        type="test_cancel_recovery",
        label="cancel recovery",
        owner="tester",
        payload={},
        status="cancelling",
        progress=50,
        message="stopping",
    )
    with get_session() as db:
        db.add(AppSetting(key="web_job:cancelling-job", value=json.dumps(stored)))

    called = False

    def handler(_context: JobContext, _payload: dict[str, Any]) -> None:
        nonlocal called
        called = True

    manager = WebJobManager(max_concurrency=1)
    manager.register("test_cancel_recovery", handler)
    await manager.start()
    recovered = get_job("cancelling-job")
    await manager.stop()

    assert recovered is not None
    assert recovered["status"] == "cancelled"
    assert recovered["recovered"] is True
    assert called is False


@pytest.mark.asyncio
async def test_running_upload_is_not_cancelled_or_replayed(temp_db: None) -> None:
    """不可幂等的外部上传开始后拒绝取消。"""
    manager = WebJobManager(max_concurrency=1)
    await manager.start()

    def handler(_context: JobContext, _payload: dict[str, Any]) -> None:
        time.sleep(0.3)

    manager.register("test_external", handler)
    job = await manager.enqueue(
        "test_external",
        {},
        label="external",
        owner="tester",
        cancellable_while_running=False,
    )
    await _wait_for_status(job["id"], {"running"})
    with pytest.raises(ValueError, match="不能安全取消"):
        manager.cancel(job["id"], "tester")
    await _wait_for_status(job["id"], {"succeeded"})
    await manager.stop()


def test_reviewer_can_only_read_own_job(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """审核员可轮询自己的作业，但不能读取其他人的作业。"""
    from app.db.entities import AppSetting
    from app.db.session import get_session
    from app.web import main

    now = datetime.now(UTC).isoformat()
    payload = _stored_job(
        id="owned-job",
        type="review_rerender",
        label="owned",
        owner="alice",
        payload={
            "candidate_id": 1,
            "start_ts": now,
            "end_ts": now,
            "version": "review-current",
        },
        status="succeeded",
        progress=100,
        message="done",
        result={},
        started_at=now,
        finished_at=now,
    )
    with get_session() as db:
        db.add(AppSetting(key="web_job:owned-job", value=json.dumps(payload)))

    monkeypatch.setattr(main, "_ADMIN_PASSWORD", "admin-pass")
    monkeypatch.setattr(main, "_REVIEWER_PASSWORDS", {"alice": "alice-pass", "bob": "bob-pass"})
    main._rate_buckets.clear()
    with TestClient(main.app) as client:
        own = client.get("/api/jobs/owned-job", auth=("alice", "alice-pass"))
        other = client.get("/api/jobs/owned-job", auth=("bob", "bob-pass"))
        admin = client.get("/api/jobs/owned-job", auth=("admin", "admin-pass"))
    main._rate_buckets.clear()

    assert own.status_code == 200
    assert other.status_code == 403
    assert admin.status_code == 200


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("version"),
        lambda value: value.update({"version": 0}),
        lambda value: value.update({"legacy_status": "queued"}),
    ],
)
def test_background_job_rejects_non_current_persisted_format(mutate: Any) -> None:
    """旧版本、缺字段和额外字段的作业记录不得被补写或迁移。"""
    payload = _stored_job()
    mutate(payload)

    with pytest.raises(ValueError, match="Web 作业"):
        decode_job(json.dumps(payload))
