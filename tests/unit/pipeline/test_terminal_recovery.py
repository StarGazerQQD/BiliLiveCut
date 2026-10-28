"""终态租约、取消重试及全场分析的真实数据库回归。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger
from sqlmodel import select

from app.analysis import session_summary
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus, Transcript
from app.db.session import get_session
from app.pipeline import heartbeat, scheduler, stale_recovery, task_worker
from app.pipeline.lease import TaskLease
from app.pipeline.stage_result import mark_failed


def seed(stage: str = TaskStatus.TRANSCRIBING) -> tuple[int, int, int]:
    with get_session() as db:
        room = LiveRoom(input_url="recovery")
        db.add(room)
        db.flush()
        recording = RecordingSession(room_id=room.id, ended_at=datetime.now(UTC))
        db.add(recording)
        db.flush()
        segment = RawSegment(session_id=recording.id, seq=0, file_path="part.wav", duration_s=8.336)
        db.add(segment)
        db.flush()
        task = SegmentTask(
            segment_id=segment.id,
            session_id=recording.id,
            stage=stage,
            claimed_by="worker",
            lease_token="old",
            claimed_at=datetime.now(UTC),
        )
        db.add(task)
        db.flush()
        return task.id, segment.id, recording.id


@pytest.mark.parametrize("permanent", [True, False])
def test_failure_releases_all_lease_fields(temp_db: None, permanent: bool) -> None:
    task_id, _, _ = seed()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        mark_failed(task, "model failed", permanent=permanent)
        db.add(task)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.claimed_by is task.claimed_at is task.lease_token is task.heartbeat_at is None
        assert task.failed_stage == TaskStatus.TRANSCRIBING
        assert task.last_error == "model failed"


def test_final_transcription_failure_remains_error(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    task_id, _, _ = seed()
    monkeypatch.setattr(scheduler, "_WORKER_ID", "worker")
    monkeypatch.setattr(scheduler, "start_heartbeat_thread", lambda *args: threading.Event())

    def unavailable(lease: object) -> None:
        raise RuntimeError("所有 ASR 引擎均不可用")

    monkeypatch.setattr(scheduler, "run_transcribe", unavailable)
    task_logger = logging.getLogger(scheduler.__name__)
    task_logger.addHandler(caplog.handler)
    try:
        scheduler.execute_task(task_id, TaskStatus.TRANSCRIBING, "old")
    finally:
        task_logger.removeHandler(caplog.handler)
    assert any(record.levelname == "ERROR" and "阶段" in record.message for record in caplog.records)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None and task.stage == TaskStatus.TRANSIENT_FAILED
        assert "所有 ASR 引擎均不可用" in task.last_error


def test_confirmed_device_failure_continues_without_asr_evidence(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.transcription.process_pool import ASRProcessPool
    from app.core.config import settings
    from tests.unit.test_asr_process_pool import cpu_torch_child

    task_id, segment_id, _ = seed()
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        assert segment is not None
        segment.file_path = str(pcm_wav)
        db.add(segment)
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    monkeypatch.setattr(settings, "asr_primary_device", "cuda")
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    monkeypatch.setattr(scheduler, "_WORKER_ID", "worker")
    pool = ASRProcessPool(cpu_torch_child)
    monkeypatch.setattr("app.analysis.transcription.isolated.get_process_pool", lambda: pool)
    try:
        scheduler.execute_task(task_id, TaskStatus.TRANSCRIBING, "old")
    finally:
        pool.shutdown(0)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None and task.stage == TaskStatus.TRANSCRIBED
        context = json.loads(task.context_json)["event_first"]
        assert context["asr_evidence_state"] == "unavailable"
        assert "CPU" in context["asr_unavailable_reason"]
        assert task.next_retry_at is None and task.claimed_by is None and task.lease_token is None


@pytest.mark.parametrize("stage", [TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED])
def test_terminal_cancel_returns_business_failure(temp_db: None, stage: str) -> None:
    task_id, _, _ = seed(stage)
    assert task_worker.cancel_task(task_id) is False


@pytest.mark.parametrize(
    "stage,expected",
    [
        (TaskStatus.ANALYZING, TaskStatus.QUEUED_FOR_ANALYSIS),
        (TaskStatus.RENDERING, TaskStatus.QUEUED_FOR_RENDER),
        (TaskStatus.QUEUED_FOR_PUBLISH, TaskStatus.QUEUED_FOR_PUBLISH),
    ],
)
def test_cancel_then_retry_uses_current_stage_not_old_failure(temp_db: None, stage: str, expected: str) -> None:
    task_id, _, session_id = seed(stage)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.failed_stage = TaskStatus.TRANSCRIBING
        db.add(task)
    assert task_worker.cancel_task(task_id)
    if stage in {TaskStatus.ANALYZING, TaskStatus.RENDERING}:
        with get_session() as db:
            assert db.get(SegmentTask, task_id).stage == TaskStatus.CANCELLING
        assert not task_worker.retry_task(task_id)
        scheduler.settle_cancellation(TaskLease(task_id, "worker", "old", stage))
    with get_session() as db:
        old_request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
    assert task_worker.retry_task(task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == expected
        assert task.stage_key.endswith(expected)
        assert task.lease_token is task.claimed_by is task.completed_at is None
        new_request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert new_request["request_id"] != old_request["request_id"]


@pytest.mark.parametrize("expired", [True, False])
def test_abandoned_cancellation_never_requeues(temp_db: None, monkeypatch: pytest.MonkeyPatch, expired: bool) -> None:
    task_id, _, _ = seed()
    assert task_worker.cancel_task(task_id)
    now = datetime.now(UTC)
    monkeypatch.setattr(stale_recovery, "now_utc", lambda: now)
    monkeypatch.setattr(stale_recovery.settings, "stale_timeout_s", 120)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.claimed_at = now - timedelta(seconds=180 if expired else 10)
        db.add(task)
    stale_recovery.recover_stale()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == (TaskStatus.CANCELLED if expired else TaskStatus.CANCELLING)
        assert task.failed_stage == TaskStatus.TRANSCRIBING
        assert (task.completed_at is not None) == expired


def test_old_cancellation_completion_cannot_clear_new_lease(temp_db: None) -> None:
    task_id, _, _ = seed(TaskStatus.RENDERING)
    assert task_worker.cancel_task(task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.claimed_by, task.lease_token = "new-worker", "new-lease"
        db.add(task)
    scheduler.settle_cancellation(TaskLease(task_id, "worker", "old", TaskStatus.RENDERING))
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.CANCELLING
        assert (task.claimed_by, task.lease_token) == ("new-worker", "new-lease")


def test_publishing_cannot_be_falsely_cancelled(temp_db: None) -> None:
    task_id, _, _ = seed(TaskStatus.PUBLISHING)
    assert not task_worker.cancel_task(task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.PUBLISHING
        assert task.lease_token == "old"


def test_heartbeat_without_lease_cannot_modify_another_owner(temp_db: None) -> None:
    task_id, _, _ = seed()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.heartbeat_at = datetime.now(UTC)
        db.add(task)
    with get_session() as db:
        before = db.get(SegmentTask, task_id).heartbeat_at
    with pytest.raises(ValueError, match="租约"):
        heartbeat.start_heartbeat_thread(task_id)
    heartbeat.clear_heartbeat_if_own(task_id)
    with get_session() as db:
        assert db.get(SegmentTask, task_id).heartbeat_at == before


def test_repeated_cancel_does_not_create_another_summary_generation(temp_db: None) -> None:
    task_id, _, session_id = seed(TaskStatus.RENDERING)
    assert task_worker.cancel_task(task_id)
    with get_session() as db:
        before = db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value
    assert task_worker.cancel_task(task_id)
    with get_session() as db:
        assert db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value == before


@pytest.mark.parametrize("job_cancel", [False, True])
def test_manual_render_cancel_settles_without_automatic_retry(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, job_cancel: bool
) -> None:
    from app.core.process_control import ProcessCancelledError
    from app.db.entities import FinalClip, HighlightCandidate, HighlightEvent
    from app.pipeline import orchestrator
    from app.pipeline.lifecycle import execution_active
    from app.web.services.candidates import approve_candidate_sync

    task_id, segment_id, session_id = seed(TaskStatus.APPROVED_WAITING_RENDER)
    now = datetime.now(UTC)
    with get_session() as db:
        candidate = HighlightCandidate(
            session_id=session_id,
            peak_ts=now,
            start_ts=now,
            end_ts=now + timedelta(seconds=8),
            dedup_hash="manual-cancel",
        )
        db.add(candidate)
        db.flush()
        event = HighlightEvent(candidate_id=candidate.id, session_id=session_id, segment_id=segment_id)
        db.add(event)
        db.flush()
        task = db.get(SegmentTask, task_id)
        task.candidate_id, task.event_id = candidate.id, event.id
        db.add(task)
        candidate_id = candidate.id
    cancelled = threading.Event()
    used_lease: list[TaskLease] = []

    def renderer(
        candidate_id: int, *, task_lease: TaskLease | None, cancel_check: Callable[[], bool], **kwargs: object
    ) -> FinalClip | None:
        assert task_lease is not None
        used_lease.append(task_lease)
        assert execution_active(task_lease.task_id, task_lease.lease_token)
        assert not cancel_check()
        if job_cancel:
            cancelled.set()
        else:
            assert task_worker.cancel_task(task_lease.task_id)
        assert cancel_check()
        raise ProcessCancelledError("cancelled at media boundary")

    monkeypatch.setattr(orchestrator, "produce_clip", renderer)
    with pytest.raises(ProcessCancelledError):
        approve_candidate_sync(candidate_id, cancel_check=cancelled.is_set)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.CANCELLED
        assert task.failed_stage == TaskStatus.RENDERING
        assert task.next_retry_at is task.claimed_by is task.lease_token is None
    assert not execution_active(task_id, used_lease[0].lease_token)


def test_cancel_during_copywriting_rejects_late_metadata(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.entities import FinalClip, HighlightCandidate, HighlightEvent
    from app.pipeline.lease import LeaseLostError
    from app.publishing import copywriter

    task_id, segment_id, session_id = seed(TaskStatus.RENDERING)
    now = datetime.now(UTC)
    with get_session() as db:
        candidate = HighlightCandidate(
            session_id=session_id,
            peak_ts=now,
            start_ts=now,
            end_ts=now + timedelta(seconds=8),
            dedup_hash="copy-cancel",
            highlight_score=0.9,
        )
        db.add(candidate)
        db.flush()
        db.add(HighlightEvent(candidate_id=candidate.id, session_id=session_id, segment_id=segment_id))
        clip = FinalClip(candidate_id=candidate.id, file_path="owned.mp4", title="kept title", status="generated")
        db.add(clip)
        db.flush()
        clip_id = clip.id

    def late_llm(text: str, reason: str) -> copywriter.Copy:
        assert task_worker.cancel_task(task_id)
        return copywriter._fallback_copy(text, reason)

    monkeypatch.setattr(copywriter, "_llm_copy", late_llm)
    with pytest.raises(LeaseLostError):
        copywriter.generate_copy(clip_id, task_lease=TaskLease(task_id, "worker", "old", TaskStatus.RENDERING))
    with get_session() as db:
        clip = db.get(FinalClip, clip_id)
        assert clip.title == "kept title" and clip.status == "generated"


def test_stale_cancel_waits_for_local_synchronous_call_to_return(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, _, _ = seed(TaskStatus.ANALYZING)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.claimed_by = scheduler._WORKER_ID
        db.add(task)
    started, response = threading.Event(), threading.Event()

    def external_call(lease: TaskLease) -> None:
        started.set()
        assert response.wait(10)

    monkeypatch.setattr(scheduler, "run_analyze", external_call)
    recovery_time = datetime.now(UTC) + timedelta(seconds=stale_recovery.settings.stale_timeout_s + 60)
    monkeypatch.setattr(stale_recovery, "now_utc", lambda: recovery_time)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(scheduler.execute_task, task_id, TaskStatus.ANALYZING, "old")
        try:
            assert started.wait(5)
            assert task_worker.cancel_task(task_id)
            stale_recovery.recover_stale()
            with get_session() as db:
                task = db.get(SegmentTask, task_id)
                assert task.stage == TaskStatus.CANCELLING and task.completed_at is None
        finally:
            response.set()
        future.result(timeout=5)
    with get_session() as db:
        assert db.get(SegmentTask, task_id).stage == TaskStatus.CANCELLED


@pytest.mark.parametrize("stage", [TaskStatus.CANCELLING, TaskStatus.CANCELLED, TaskStatus.TRANSCRIBED])
def test_heartbeat_state_change_is_not_reported_as_takeover(temp_db: None, stage: str) -> None:
    task_id, _, _ = seed(stage)
    messages: list[str] = []
    seen = threading.Event()

    def collect(message: str) -> None:
        text = str(message)
        messages.append(text)
        if "heartbeat_stopped" in text:
            seen.set()

    sink = logger.add(collect, level="INFO")
    stop = heartbeat.start_heartbeat_thread(task_id, "old", TaskStatus.TRANSCRIBING)
    try:
        assert seen.wait(5)
    finally:
        stop.set()
        for thread in threading.enumerate():
            if thread.name == f"hb-{task_id}":
                thread.join(5)
                assert not thread.is_alive()
        logger.remove(sink)
    assert not any("lease_lost" in text for text in messages)
    assert any(f"stage={stage}" in text for text in messages)


@pytest.mark.parametrize("expired", [True, False])
def test_null_heartbeat_uses_claim_time(temp_db: None, monkeypatch: pytest.MonkeyPatch, expired: bool) -> None:
    task_id, _, _ = seed()
    now = datetime.now(UTC)
    monkeypatch.setattr(stale_recovery, "now_utc", lambda: now)
    monkeypatch.setattr(stale_recovery.settings, "stale_timeout_s", 120)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.claimed_at = now - timedelta(seconds=180 if expired else 10)
        db.add(task)
    stale_recovery.recover_stale()
    with get_session() as db:
        assert db.get(SegmentTask, task_id).stage == (
            TaskStatus.QUEUED_FOR_TRANS if expired else TaskStatus.TRANSCRIBING
        )


def test_failed_lease_cleanup_unblocks_degraded_summary(temp_db: None) -> None:
    _, _, session_id = seed(TaskStatus.FAILED)
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    assert session_summary.claim_pending_session_summary() is None
    stale_recovery.recover_stale()
    claim = session_summary.claim_pending_session_summary()
    assert claim is not None and session_summary.execute_session_summary_claim(claim)
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["status"] == "ready"
    assert view["coverage"]["mode"] == "degraded"
    assert len(view["coverage"]["missing"]) == 1


def test_no_speech_is_covered_without_sending_empty_text_to_llm(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _, segment_id, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    from app.analysis import llm

    def forbidden(*args: object, **kwargs: object) -> str:
        pytest.fail("无文本不得调用 LLM")

    monkeypatch.setattr(llm, "call_text", forbidden)
    with get_session() as db:
        db.add(
            Transcript(segment_id=segment_id, final_text="", auxiliary_json=json.dumps({"result_type": "no_speech"}))
        )
    result = session_summary.build_session_timeline_summary(session_id)
    assert result["coverage"]["mode"] == "full"
    assert result["coverage"]["covered_duration_s"] == 8.336
    assert result["coverage"]["no_speech_segments"] == 1
    from app.web.services.transcripts import list_transcripts

    item = list_transcripts(session_id=session_id)[0]
    assert item["result_type"] == "no_speech" and item["text"] == ""


def test_changed_input_during_generation_is_requeued(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _, segment_id, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    claim = session_summary.claim_pending_session_summary()
    original = session_summary.build_session_timeline_summary

    def change_input(recording_id: int) -> dict[str, object]:
        result = original(recording_id)
        with get_session() as db:
            db.add(Transcript(segment_id=segment_id, final_text="晚到的转写"))
        return result

    monkeypatch.setattr(session_summary, "build_session_timeline_summary", change_input)
    assert session_summary.execute_session_summary_claim(claim) is False
    with get_session() as db:
        assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None
        request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert request["status"] == "pending"
        assert request["attempts"] == 0


def test_compensation_is_independent_of_timeline_and_idempotent(temp_db: None) -> None:
    _, _, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    assert session_summary.compensate_session_summaries() == (1, 0)
    assert session_summary.compensate_session_summaries() == (0, 0)
    claim = session_summary.claim_pending_session_summary()
    assert claim.session_id == session_id
    assert session_summary.execute_session_summary_claim(claim)
    assert session_summary.compensate_session_summaries() == (0, 0)


def test_shutdown_revokes_lease_and_rejects_late_commit_without_attempt(temp_db: None) -> None:
    from app.pipeline.lease import TaskLease
    from app.pipeline.workers.transcribe import commit_transcript

    task_id, segment_id, _ = seed()
    assert stale_recovery.release_stopped_computations("worker") == 1
    commit_transcript(
        TaskLease(task_id, "worker", "old", TaskStatus.TRANSCRIBING), {"text": "late", "segment_id": segment_id}, 20
    )
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.QUEUED_FOR_TRANS
        assert task.attempts == 0 and task.lease_token is None
        assert db.exec(select(Transcript)).all() == []


def test_summary_stop_fences_old_result_without_attempt(temp_db: None) -> None:
    _, _, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    claim = session_summary.claim_pending_session_summary()
    result = session_summary.build_session_timeline_summary(session_id)
    session_summary.interrupt_summary_execution(claim)
    assert session_summary._commit_summary_result(claim, result) is False
    with get_session() as db:
        request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert request["attempts"] == 0
        assert request["request_id"] != claim.request_id


def test_filled_gap_replaces_degraded_summary_as_recovered(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.analysis import llm

    task_id, segment_id, session_id = seed(TaskStatus.FAILED)
    stale_recovery.recover_stale()
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    assert session_summary.execute_session_summary_claim(session_summary.claim_pending_session_summary())
    assert task_worker.retry_task(task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.stage = TaskStatus.COMPLETED
        db.add(task)
        db.add(Transcript(segment_id=segment_id, final_text="恢复后的内容"))
    monkeypatch.setattr(llm, "call_text", lambda *args, **kwargs: '{"summary":"补齐结果"}')
    assert session_summary.execute_session_summary_claim(session_summary.claim_pending_session_summary())
    view = session_summary.session_timeline_summary_view(session_id, ended=True, processing_state="completed")
    assert view["summary_mode"] == "recovered"
    assert view["coverage"]["coverage_ratio"] == 1
    assert view["summary"] == "补齐结果"


def test_resource_reservations_are_released_by_lease_generation(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import resource_budget
    from app.pipeline import lifecycle, scheduler

    reservations = {(159, "old"): {"cpu": 1}, (159, "new"): {"cpu": 2}}
    released: list[dict[str, int]] = []
    monkeypatch.setattr(lifecycle, "_task_resources", reservations)
    monkeypatch.setattr(resource_budget, "release_resources", lambda **cost: released.append(cost))
    monkeypatch.setattr(scheduler, "start_heartbeat_thread", lambda *args: threading.Event())
    monkeypatch.setattr(scheduler, "clear_heartbeat_if_own", lambda *args: None)
    monkeypatch.setattr(scheduler, "run_transcribe", lambda lease: None)
    scheduler.execute_task(159, TaskStatus.TRANSCRIBING, "old")
    assert reservations == {(159, "new"): {"cpu": 2}}
    assert released == [{"cpu": 1}]
    scheduler.execute_task(159, TaskStatus.TRANSCRIBING, "new")
    assert reservations == {}
    assert released == [{"cpu": 1}, {"cpu": 2}]


async def test_shutdown_failure_still_fences_computation_and_summary(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.transcription import process_pool

    task_id, _, _ = seed()
    _, _, session_id = seed(TaskStatus.COMPLETED)
    stale_recovery.recover_stale()
    session_summary.request_session_timeline_summary(session_id, reason="ended")
    claim = session_summary.claim_pending_session_summary()
    result = session_summary.build_session_timeline_summary(session_id)
    monkeypatch.setattr(task_worker, "_WORKER_ID", "worker")

    def broken_shutdown(grace: float) -> None:
        raise RuntimeError("native process exit unconfirmed")

    monkeypatch.setattr(process_pool, "get_process_pool", lambda: SimpleNamespace(shutdown=broken_shutdown))
    worker = task_worker.TaskWorker()
    dummy = asyncio.create_task(asyncio.sleep(0))
    worker._summary_claims[dummy] = claim
    with pytest.raises(RuntimeError, match="native process exit unconfirmed"):
        await worker.stop()
    await dummy
    with get_session() as db:
        assert db.get(SegmentTask, task_id).lease_token is None
    assert session_summary._commit_summary_result(claim, result) is False


@pytest.mark.parametrize("stage", [TaskStatus.TRANSCRIBING, TaskStatus.RENDERING])
@pytest.mark.parametrize("attempts", [0, 2])
def test_shutdown_does_not_consume_task_retry_budget(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, stage: str, attempts: int
) -> None:
    from app.analysis.transcription.process_pool import ASRShutdownError
    from app.core.process_control import ProcessShutdownError
    from app.pipeline import scheduler

    task_id, _, _ = seed(stage)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None
        task.attempts = attempts
        db.add(task)
    monkeypatch.setattr(scheduler, "_WORKER_ID", "worker")
    monkeypatch.setattr(scheduler, "start_heartbeat_thread", lambda *args: threading.Event())

    def interrupted(lease: object) -> None:
        error = ASRShutdownError if stage == TaskStatus.TRANSCRIBING else ProcessShutdownError
        raise error("shutdown deadline")

    monkeypatch.setattr(scheduler, "run_transcribe" if stage == TaskStatus.TRANSCRIBING else "run_render", interrupted)
    scheduler.execute_task(task_id, stage, "old")
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == stale_recovery.resume_stage(stage) and task.attempts == attempts
        assert task.lease_token is None


@pytest.mark.parametrize("summary", [False, True])
async def test_database_claim_wait_keeps_loop_responsive_and_cancel_requeues(temp_db: None, summary: bool) -> None:
    from app.db import session as db_session

    stage = TaskStatus.COMPLETED if summary else TaskStatus.QUEUED_FOR_TRANS
    task_id, _, session_id = seed(stage)
    if summary:
        stale_recovery.recover_stale()
        session_summary.request_session_timeline_summary(session_id, reason="ended")
    locked = threading.Event()
    release = threading.Event()

    def hold_database_write_lock() -> None:
        with db_session.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            locked.set()
            assert release.wait(4), "test write lock release was not signalled"
            connection.rollback()

    holder = threading.Thread(target=hold_database_write_lock)
    holder.start()
    assert await asyncio.to_thread(locked.wait, 2)
    worker = task_worker.TaskWorker()
    worker._running = True
    task_worker.shutdown_event.clear()
    operation = (
        worker._dispatch_session_summaries()
        if summary
        else worker._dispatch(TaskStatus.QUEUED_FOR_TRANS, worker._transcribing, 1)
    )
    pending = asyncio.create_task(operation)
    started = time.monotonic()
    try:
        await asyncio.sleep(0.1)
        assert time.monotonic() - started < 1, "SQLite lock blocked the event loop"
        assert not pending.done()
        pending.cancel()
        await asyncio.sleep(0)
        assert not pending.done(), "cancellation abandoned a database claim still in flight"
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 3)
        if not pending.done():
            pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert not holder.is_alive()
    assert not worker._session_summaries and not worker._transcribing
    with get_session() as db:
        if summary:
            request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
            assert request["status"] == "pending" and request["attempts"] == 0
        else:
            task = db.get(SegmentTask, task_id)
            assert task.stage == TaskStatus.QUEUED_FOR_TRANS and task.attempts == 0
            assert task.lease_token is None
