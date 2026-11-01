from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.db.entities import AppSetting, SegmentTask, TaskStatus, Transcript
from app.db.session import get_session
from app.pipeline import claiming, stale_recovery
from app.pipeline.lifecycle import _WORKER_ID, finish_execution, register_execution
from app.pipeline.recovery_budget import RecoveryBudget, consume_recovery
from app.pipeline.stage_result import enqueue_next, mark_completed
from app.pipeline.task_worker import retry_task

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def seed(queued: str, retries: int = 3) -> int:
    with get_session() as db:
        task = SegmentTask(segment_id=1, session_id=1, stage=queued, max_retries=retries)
        db.add(task)
        db.flush()
        assert task.id is not None
        return task.id


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claiming, "now_utc", lambda: NOW - timedelta(minutes=5))
    monkeypatch.setattr(stale_recovery, "now_utc", lambda: NOW)


@pytest.mark.parametrize("asr", [False, True])
def test_repeated_real_claims_and_recovery_have_persistent_limit(temp_db: None, asr: bool) -> None:
    queued = TaskStatus.QUEUED_FOR_TRANS if asr else TaskStatus.QUEUED_FOR_RENDER
    stage = TaskStatus.TRANSCRIBING if asr else TaskStatus.RENDERING
    task_id = seed(queued)
    for attempt in range(3):
        claimed = claiming.pop_and_claim(queued)
        assert claimed is not None and claimed.id == task_id
        stale_recovery.recover_stale()
        stale_recovery.recover_stale()
        with get_session() as db:
            row = db.get(AppSetting, f"task_recovery:v1:{task_id}:{stage}")
            assert row is not None
            assert RecoveryBudget.model_validate_json(row.value).consumed == attempt + 1
            task = db.get(SegmentTask, task_id)
            assert task is not None
            if attempt < 2:
                assert task.stage == queued
            else:
                assert task.stage == (TaskStatus.TRANSCRIBED if asr else TaskStatus.FAILED)
                assert task.claimed_by is task.lease_token is None
                if asr:
                    assert db.get(Transcript, 1) is None
                    assert "unavailable" in json.loads(task.context_json or "{}")["event_first"].values()
                else:
                    assert "预算耗尽" in (task.last_error or "")


def test_same_execution_is_counted_once_and_success_resets_in_transaction(temp_db: None) -> None:
    task_id = seed(TaskStatus.QUEUED_FOR_ANALYSIS)
    claimed = claiming.pop_and_claim(TaskStatus.QUEUED_FOR_ANALYSIS)
    assert claimed is not None
    key = f"task_recovery:v1:{task_id}:{TaskStatus.ANALYZING}"
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None
        assert not consume_recovery(db, task, "lost")
        db.flush()
        assert not consume_recovery(db, task, "same execution")
    with get_session() as db:
        row = db.get(AppSetting, key)
        assert row is not None and RecoveryBudget.model_validate_json(row.value).consumed == 1
        task = db.get(SegmentTask, task_id)
        assert task is not None
        mark_completed(task)
        enqueue_next(task, TaskStatus.COMPLETED)
    with get_session() as db:
        assert db.get(AppSetting, key) is None


def test_explicit_retry_resets_exhausted_budget(temp_db: None) -> None:
    task_id = seed(TaskStatus.QUEUED_FOR_RENDER, retries=1)
    assert claiming.pop_and_claim(TaskStatus.QUEUED_FOR_RENDER) is not None
    stale_recovery.recover_stale()
    assert retry_task(task_id)
    with get_session() as db:
        assert db.get(AppSetting, f"task_recovery:v1:{task_id}:{TaskStatus.RENDERING}") is None
    assert claiming.pop_and_claim(TaskStatus.QUEUED_FOR_RENDER) is not None


def test_stale_heartbeat_does_not_release_live_execution(temp_db: None) -> None:
    task_id = seed(TaskStatus.QUEUED_FOR_ANALYSIS)
    task = claiming.pop_and_claim(TaskStatus.QUEUED_FOR_ANALYSIS)
    assert task is not None and task.claimed_by == _WORKER_ID and task.lease_token
    register_execution(task_id, task.lease_token)
    try:
        stale_recovery.recover_stale()
        with get_session() as db:
            current = db.get(SegmentTask, task_id)
            assert current is not None and current.stage == TaskStatus.ANALYZING
            assert current.lease_token == task.lease_token
            assert db.get(AppSetting, f"task_recovery:v1:{task_id}:{TaskStatus.ANALYZING}") is None
    finally:
        finish_execution(task_id, task.lease_token)
