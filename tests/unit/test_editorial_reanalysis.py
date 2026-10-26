from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlmodel import select

from app.analysis.editorial_reanalysis import commit_reanalysis, compute_reanalysis
from app.analysis.reanalysis import queue_session_reanalysis
from app.analysis.reanalysis_targets import (
    ReanalysisSelection,
    ReanalysisTarget,
    list_reanalysis_targets,
    preview_reanalysis,
)
from app.db.entities import (
    AppSetting,
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    RawSegment,
    SegmentTask,
    TaskStatus,
    Transcript,
)
from app.db.session import get_session
from app.pipeline.workers.analyze import analyze_compute, commit_highlight, score_hotspot_event, score_segment_direct
from app.web.services.review_workflow import claim_event
from tests.unit.test_editorial_review import _seed, _service

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _targets() -> list[ReanalysisTarget]:
    with get_session() as db:
        session_id = db.exec(select(RawSegment.session_id)).first()
        return list_reanalysis_targets(db, session_id)


@pytest.mark.parametrize("legacy", [False, True])
def test_reanalysis_changes_both_directions_without_replacing_ids(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    legacy: bool,
) -> None:
    hotspot_id, segment_id = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    if legacy:
        with get_session() as db:
            db.delete(db.get(HotspotEvent, hotspot_id))
        candidate = score_segment_direct(segment_id)
    else:
        candidate = score_hotspot_event(hotspot_id)
    assert candidate is not None
    cid = candidate.id
    with get_session() as db:
        event = db.exec(select(HighlightEvent)).one()
        eid = event.id
        original_created = event.created_at
        db.add(
            SegmentTask(
                segment_id=segment_id,
                session_id=candidate.session_id,
                candidate_id=cid,
                event_id=eid,
                stage=TaskStatus.QUEUED_FOR_RENDER,
            )
        )
    for verdict, expected_status, expected_stage in (
        ("reject", "rejected", TaskStatus.COMPLETED),
        ("recommend", "pending", TaskStatus.AWAITING_REVIEW),
    ):
        calls = _service(monkeypatch, verdict)
        targets = _targets()
        assert len(targets) == 1 and targets[0].protection is None
        drafts = compute_reanalysis(targets, cancelled=lambda: False)
        with get_session() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            result = commit_reanalysis(db, drafts)
        assert result["updated"] == 1 and calls
        with get_session() as db:
            current = db.exec(select(HighlightCandidate)).one()
            event = db.exec(select(HighlightEvent)).one()
            assert current.id == cid and current.status == expected_status
            assert event.id == eid and event.created_at == original_created and event.candidate_id == cid
            assert json.loads(event.features_json)["editorial_review"]["execution"]["result"]["verdict"] == verdict
            assert db.exec(select(SegmentTask)).one().stage == expected_stage
            if not legacy:
                assert db.get(HotspotEvent, hotspot_id).candidate_id == cid


@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "manual",
        "boundary",
        "transcript",
        "configuration",
        "active_task",
        "other_segment_task",
        "active_media_job",
    ],
)
def test_late_reanalysis_cannot_overwrite_changed_state(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    change: str,
) -> None:
    hotspot_id, segment_id = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(hotspot_id)
    _service(monkeypatch, "reject")
    drafts = compute_reanalysis(_targets(), cancelled=lambda: False)
    with get_session() as db:
        event = db.exec(select(HighlightEvent)).one()
        if change == "claim":
            claim_event(event, "reviewer", "reviewer")
        elif change == "manual":
            event.review_by = "human"
            event.review_reason = "保留这段"
        elif change == "boundary":
            event.adjusted_start_ts = event.raw_start_ts + timedelta(seconds=1)
        elif change == "transcript":
            transcript = db.exec(select(Transcript)).one()
            transcript.final_text = "人工订正的新事实。"
            db.add(transcript)
        elif change == "configuration":
            row = db.get(AppSetting, "configuration_revision") or AppSetting(key="configuration_revision", value="0")
            row.value = str(int(row.value) + 1)
            db.add(row)
        elif change == "active_media_job":
            from tests.integration.test_job_transactions import _job

            job = _job(job_type="candidate_render")
            job["payload"] = {"candidate_id": candidate.id, "reviewed_by": "admin"}
            db.add(AppSetting(key=f"web_job:{job['id']}", value=json.dumps(job)))
        else:
            if change == "other_segment_task":
                original = db.get(RawSegment, segment_id)
                other = RawSegment(session_id=original.session_id, seq=1, file_path="other.ts")
                db.add(other)
                db.flush()
                segment_id = other.id
            db.add(
                SegmentTask(
                    segment_id=segment_id,
                    session_id=candidate.session_id,
                    candidate_id=candidate.id,
                    event_id=event.id,
                    stage=TaskStatus.RENDERING,
                    lease_token="running",
                )
            )
        db.add(event)
    with get_session() as db:
        before = db.exec(select(HighlightEvent)).one().model_dump()
        result = commit_reanalysis(db, drafts)
    assert result["updated"] == 0 and result["skipped"] == 1
    with get_session() as db:
        assert db.exec(select(HighlightEvent)).one().model_dump() == before
        assert db.get(HighlightCandidate, candidate.id).status == "pending"


def test_active_claim_without_draft_is_protected_before_calls_and_queue(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(hotspot_id)
    with get_session() as db:
        event = db.exec(select(HighlightEvent)).one()
        claim_event(event, "reviewer", "reviewer")
        db.add(event)
    calls = _service(monkeypatch, "reject")
    targets = _targets()
    assert targets[0].protection == "active_claim"
    drafts = compute_reanalysis(targets, cancelled=lambda: False)
    assert drafts[0].skip_reason == "active_claim" and not calls
    result = queue_session_reanalysis(candidate.session_id, reason="new_model")
    assert result.preserved == 1 and result.queued == 0


def test_preview_filters_rejected_hotspots_without_model_calls(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    calls = _service(monkeypatch, "reject")
    assert score_hotspot_event(hotspot_id) is None
    count = len(calls)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        selection = ReanalysisSelection(session_id=hotspot.session_id, verdicts=["reject"], limit=1)
        first = preview_reanalysis(db, selection)
        assert preview_reanalysis(db, selection) == first
        assert first["total"] == 1 and first["targets"][0]["candidate_id"] is None
        assert preview_reanalysis(db, selection.model_copy(update={"failed_only": True}))["total"] == 0
    assert len(calls) == count


def test_pipeline_reanalysis_preserves_ids_with_real_lease(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline.lease import TaskLease

    hotspot_id, segment_id = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(hotspot_id)
    assert queue_session_reanalysis(candidate.session_id, reason="new_policy").queued == 1
    with get_session() as db:
        task = db.exec(select(SegmentTask)).one()
        task.stage, task.claimed_by, task.lease_token = TaskStatus.ANALYZING, "test-worker", "lease-1"
        task_id = task.id
        db.add(task)
    _service(monkeypatch, "reject")
    computed = analyze_compute(task_id)
    assert computed["reanalysis_results"]
    commit_highlight(TaskLease(task_id, "test-worker", "lease-1", TaskStatus.ANALYZING), computed, 10)
    with get_session() as db:
        assert db.get(HighlightCandidate, candidate.id).status == "rejected"
        assert db.exec(select(HighlightEvent)).one().candidate_id == candidate.id
        assert db.get(HotspotEvent, hotspot_id).candidate_id == candidate.id
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.COMPLETED and task.lease_token is None
        assert json.loads(task.context_json)["reanalysis"]["result"]["updated"] == 1


@pytest.mark.asyncio
async def test_persistent_job_atomically_updates_result_and_deduplicates_active_requests(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.db.job_store import get_job
    from app.web.services.background_jobs import WebJobManager
    from app.web.services.editorial_reanalysis import enqueue_editorial_reanalysis

    hotspot_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    candidate = await asyncio.to_thread(score_hotspot_event, hotspot_id)
    calls = _service(monkeypatch, "reject")
    selection = ReanalysisSelection(session_id=candidate.session_id)
    with get_session() as db:
        fingerprint = preview_reanalysis(db, selection)["fingerprint"]
    manager = WebJobManager()
    try:
        first = await enqueue_editorial_reanalysis(manager, selection, fingerprint, actor="admin")
        second = await enqueue_editorial_reanalysis(manager, selection, fingerprint, actor="admin")
        assert first["id"] == second["id"]
        await asyncio.gather(*tuple(manager._tasks.values()))
        job = get_job(first["id"])
        assert job["status"] == "succeeded", job
        assert job["result"]["updated"] == 1
        with pytest.raises(ValueError, match="预览已过期"):
            await enqueue_editorial_reanalysis(manager, selection, fingerprint, actor="admin")
        assert len(calls) == 3
        with get_session() as db:
            assert db.get(HighlightCandidate, candidate.id).status == "rejected"
    finally:
        await manager.stop()


def test_rejected_legacy_window_can_become_candidate_without_losing_window_record(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    hotspot_id, segment_id = _seed(pcm_wav)
    with get_session() as db:
        db.delete(db.get(HotspotEvent, hotspot_id))
    _service(monkeypatch, "reject")
    assert score_segment_direct(segment_id) is None
    targets = _targets()
    assert len(targets) == 1 and targets[0].candidate_id is None and targets[0].verdict == "reject"
    _service(monkeypatch, "recommend")
    drafts = compute_reanalysis(_targets(), cancelled=lambda: False)
    with get_session() as db:
        result = commit_reanalysis(db, drafts)
    assert result["updated"] == 1
    with get_session() as db:
        candidate = db.exec(select(HighlightCandidate)).one()
        record = json.loads(db.get(AppSetting, f"editorial_segment:{segment_id}").value)
        assert len(record["windows"]) == 1 and record["windows"][0]["candidate_id"] == candidate.id


def test_reanalysis_batch_shares_attempt_budget(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core.config import settings

    hotspot_id, _ = _seed(pcm_wav)
    with get_session() as db:
        first = db.get(HotspotEvent, hotspot_id)
        values = first.model_dump(exclude={"id", "event_key"})
        db.add(HotspotEvent(**values, event_key="second-target"))
    calls = _service(monkeypatch, "reject")
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 3)
    drafts = compute_reanalysis(_targets(), cancelled=lambda: False)
    assert len(calls) == 3
    assert drafts[0].payload is not None and drafts[1].skip_reason == "attempt_budget"


@pytest.mark.asyncio
async def test_cancel_during_remote_response_prevents_business_commit(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.db.job_store import get_job
    from app.web.services.background_jobs import WebJobManager
    from tests.editorial_fixtures import install_editorial_service

    hotspot_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    candidate = await asyncio.to_thread(score_hotspot_event, hotspot_id)
    entered, release = threading.Event(), threading.Event()

    def response(_prompt: str, **_kwargs: object) -> None:
        entered.set()
        assert release.wait(10), "测试取消通知未到达 HTTP 边界"
        return None

    calls = install_editorial_service(monkeypatch, response, grounded=True)
    targets = await asyncio.to_thread(_targets)
    manager = WebJobManager()
    try:
        job = await manager.enqueue(
            "editorial_reanalysis",
            {
                "session_id": candidate.session_id,
                "targets": [item.model_dump(mode="json") for item in targets],
            },
            label="重评",
            owner="admin",
        )
        assert await asyncio.to_thread(entered.wait, 10)
        await asyncio.to_thread(manager.cancel, job["id"], "admin")
        release.set()
        await asyncio.gather(*tuple(manager._tasks.values()))
        assert get_job(job["id"])["status"] == "cancelled"
        assert len(calls) == 1
        with get_session() as db:
            assert db.get(HighlightCandidate, candidate.id).status == "pending"
    finally:
        release.set()
        await manager.stop()


def test_lost_pipeline_lease_cannot_commit_reanalysis(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline.lease import TaskLease

    hotspot_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(hotspot_id)
    queue_session_reanalysis(candidate.session_id, reason="new_policy")
    with get_session() as db:
        task = db.exec(select(SegmentTask)).one()
        task.stage, task.claimed_by, task.lease_token = TaskStatus.ANALYZING, "worker", "old"
        task_id = task.id
        db.add(task)
    _service(monkeypatch, "reject")
    result = analyze_compute(task_id)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        task.lease_token = "new"
        db.add(task)
    commit_highlight(TaskLease(task_id, "worker", "old", TaskStatus.ANALYZING), result, 10)
    with get_session() as db:
        assert db.get(HighlightCandidate, candidate.id).status == "pending"
        assert db.get(SegmentTask, task_id).lease_token == "new"
