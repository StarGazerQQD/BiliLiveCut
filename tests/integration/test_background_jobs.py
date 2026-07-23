"""持久化 Web 后台作业的进度、取消、重试和恢复测试。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.web.services.background_jobs import JobContext, WebJobManager, get_job


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
    assert failed["error"] == "boom"
    manager.retry(job["id"], "tester")
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
    from app.db.models import AppSetting
    from app.db.session import get_session

    now = datetime.now(UTC).isoformat()
    stored = {
        "id": "recover-job",
        "type": "test_recover",
        "label": "recover",
        "owner": "tester",
        "payload": {"value": 7},
        "status": "running",
        "progress": 50,
        "message": "old process",
        "attempt": 1,
        "created_at": now,
        "updated_at": now,
        "cancellable_while_running": True,
    }
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
    from app.db.models import AppSetting
    from app.db.session import get_session
    from app.web import main

    now = datetime.now(UTC).isoformat()
    payload = {
        "id": "owned-job",
        "type": "review_rerender",
        "label": "owned",
        "owner": "alice",
        "payload": {},
        "status": "succeeded",
        "progress": 100,
        "message": "done",
        "attempt": 1,
        "created_at": now,
        "updated_at": now,
        "finished_at": now,
    }
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
