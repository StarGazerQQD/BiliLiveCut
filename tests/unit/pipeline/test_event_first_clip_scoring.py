"""Event-first 候选评分主路径与既有审核任务链集成测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pytest
from sqlmodel import select

from app.analysis.audio import AudioFeatures
from app.db.entities import (
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    HotspotStatus,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentTask,
    SessionStatus,
    TaskStatus,
)
from app.db.session import get_session
from app.pipeline.lease import TaskLease
from tests.editorial_fixtures import install_editorial_service

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

_START = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)


def _seed_candidate_pass(*, strong: bool) -> tuple[int, int, int]:
    modalities: dict[str, float | None]
    if strong:
        modalities = {"danmaku": 0.92, "audio": 0.88, "sensevoice": 0.82, "asr": None, "trend": None}
        heat, detector_clip = 0.94, 0.89
    else:
        modalities = {"danmaku": None, "audio": None, "sensevoice": None, "asr": 0.95, "trend": 0.90}
        heat, detector_clip = 0.25, 0.45
    evidence = [
        {
            "id": f"pipeline-event:{name}",
            "type": name,
            "metrics": {
                "score": score,
                **({"semantic_novelty": score, "topic_change": score} if name == "asr" else {}),
            },
            "excerpts": ["主播说明活动规则"]
            if name == "asr"
            else ["大家在讨论这轮挑战规则"]
            if name == "danmaku"
            else [],
        }
        for name, score in modalities.items()
        if score is not None
    ]
    with get_session() as db:
        room = LiveRoom(
            input_url="event-first-clip",
            room_id=181801,
            auto_analyze=True,
            highlight_threshold=0.38,
            review_threshold=0.30,
        )
        db.add(room)
        db.flush()
        assert room.id is not None
        session = RecordingSession(room_id=room.id, status=SessionStatus.STOPPED, started_at=_START)
        db.add(session)
        db.flush()
        assert session.id is not None
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path="event-first-clip.ts",
            start_ts=_START,
            end_ts=_START + timedelta(seconds=300),
            duration_s=300.0,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        event = HotspotEvent(
            event_key=f"pipeline-event:{'strong' if strong else 'semantic'}",
            session_id=session.id,
            start_ts=_START + timedelta(seconds=100),
            peak_ts=_START + timedelta(seconds=120),
            end_ts=_START + timedelta(seconds=150),
            status=HotspotStatus.CONFIRMED,
            heat_score=heat,
            clip_score=detector_clip,
            semantic_confidence=0.9 if not strong else 0.1,
            evidence_coverage=0.85,
            features_json=json.dumps(
                {
                    "detector_version": "test-v1",
                    "ticks": [{"modality_scores": modalities}],
                    "event_lifecycle": {"version": 1},
                },
                ensure_ascii=False,
            ),
            evidence_json=json.dumps({"version": 1, "items": evidence}, ensure_ascii=False),
            transcript_text="主播说明活动规则" if not strong else None,
            representative_danmaku_json=json.dumps(
                [{"text": "大家在讨论这轮挑战规则", "role": "information", "count": 5}] if strong else [],
                ensure_ascii=False,
            ),
        )
        db.add(event)
        db.flush()
        assert event.id is not None
        task = SegmentTask(
            segment_id=segment.id,
            session_id=session.id,
            stage=TaskStatus.ANALYZING,
            claimed_by="event-worker",
            lease_token="event-token",
            context_json='{"event_first":{"analysis_pass":"candidate"}}',
        )
        db.add(task)
        db.flush()
        assert task.id is not None
        return task.id, event.id, session.id


def _lease(task_id: int) -> TaskLease:
    return TaskLease(
        task_id=task_id,
        worker_id="event-worker",
        lease_token="event-token",
        expected_stage=TaskStatus.ANALYZING,
    )


def _patch_compute_dependencies(monkeypatch: MonkeyPatch) -> None:
    from app.analysis import hotspot_detector
    from app.pipeline.workers import analyze

    audio = AudioFeatures(
        sample_rate=16_000,
        hop_s=1.0,
        times=np.asarray([0.0, 300.0]),
        rms=np.asarray([0.1, 0.9]),
        duration_s=300.0,
        silences=[],
    )
    monkeypatch.setattr(analyze.audio_mod, "analyze_audio", lambda _path: audio)
    monkeypatch.setattr(hotspot_detector, "detect_segment_hotspots", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(hotspot_detector, "log_hotspot_detection", lambda *_args, **_kwargs: None)


def test_candidate_pass_scores_confirmed_event_and_links_task(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline.workers.analyze import HighlightDecision, analyze_compute, commit_highlight

    task_id, hotspot_event_id, session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)

    result = analyze_compute(task_id)

    assert result["decision"] == HighlightDecision.EVENT_PASS
    assert "event_clip_results" not in result
    assert "additional_candidates" not in result
    commit_highlight(_lease(task_id), result, 50)

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        hotspot = db.get(HotspotEvent, hotspot_event_id)
        candidates = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)).all()
        events = db.exec(select(HighlightEvent).where(HighlightEvent.session_id == session_id)).all()
    assert task is not None and task.stage == TaskStatus.CANDIDATE_CREATED
    assert hotspot is not None and hotspot.candidate_id == task.candidate_id
    assert len(candidates) == 1
    assert len(events) == 1
    assert events[0].id == task.event_id
    assert events[0].candidate_id == task.candidate_id


def test_candidate_pass_keeps_semantic_only_hotspot_without_candidate(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis.clip_scorer import pending_hotspot_event_ids
    from app.pipeline.workers.analyze import HighlightDecision, analyze_compute, commit_highlight

    task_id, hotspot_event_id, session_id = _seed_candidate_pass(strong=False)
    _patch_compute_dependencies(monkeypatch)

    result = analyze_compute(task_id)

    assert result["decision"] == HighlightDecision.EVENT_PASS
    commit_highlight(_lease(task_id), result, 50)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        hotspot = db.get(HotspotEvent, hotspot_event_id)
        candidates = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)).all()
    assert task is not None and task.stage == TaskStatus.COMPLETED
    assert hotspot is not None and hotspot.candidate_id is None
    assert hotspot.status == HotspotStatus.CONFIRMED
    assert hotspot.clip_score == 0.35
    assert json.loads(hotspot.features_json or "{}")["event_clip_score"]["decision"] == "below_threshold"
    assert candidates == []
    assert pending_hotspot_event_ids(session_id) == []


def test_terminal_commit_scores_event_confirmed_after_compute(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline.workers.analyze import HighlightDecision, analyze_compute, commit_highlight

    task_id, hotspot_event_id, session_id = _seed_candidate_pass(strong=True)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_event_id)
        assert hotspot is not None
        hotspot.status = HotspotStatus.PROVISIONAL
        db.add(hotspot)
    _patch_compute_dependencies(monkeypatch)

    result = analyze_compute(task_id)

    assert result["decision"] == HighlightDecision.EVENT_PASS
    commit_highlight(_lease(task_id), result, 50)

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        hotspot = db.get(HotspotEvent, hotspot_event_id)
        candidates = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)).all()
    assert task is not None and task.stage == TaskStatus.CANDIDATE_CREATED
    assert task.candidate_id == hotspot.candidate_id and task.event_id is not None
    assert hotspot is not None and hotspot.status == HotspotStatus.CONFIRMED
    assert hotspot.candidate_id is not None
    assert len(candidates) == 1


def test_late_confirmation_rechecks_lease_after_model_call(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline.workers.analyze import analyze_compute, commit_highlight

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        event.status = HotspotStatus.PROVISIONAL
        db.add(event)
    _patch_compute_dependencies(monkeypatch)
    result = analyze_compute(task_id)

    def replacing_worker(prompt: str, **kwargs: object) -> None:
        with get_session() as db:
            task = db.get(SegmentTask, task_id)
            task.lease_token = "replacement-owner-token"
            db.add(task)
        return None

    install_editorial_service(monkeypatch, replacing_worker)
    commit_highlight(_lease(task_id), result, 50)
    with get_session() as db:
        assert db.get(SegmentTask, task_id).lease_token == "replacement-owner-token"
        assert db.get(SegmentTask, task_id).stage == TaskStatus.ANALYZING
        assert not db.exec(select(HighlightCandidate)).all()
        assert not db.exec(select(HighlightEvent)).all()


def test_lost_lease_during_generation_prevents_grounding_and_commit(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.pipeline.workers.analyze import analyze_compute, commit_highlight

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)

    def replacing_worker(_prompt: str, **_kwargs: object) -> str:
        with get_session() as db:
            task = db.get(SegmentTask, task_id)
            task.lease_token = "new-owner"
            db.add(task)
        return json.dumps(
            {
                "title": "观众讨论挑战规则",
                "summary": "弹幕在讨论这轮挑战的规则。",
                "category": "discussion",
                "entities": [],
                "semantic_confidence": 0.3,
                "evidence_ids": ["pipeline-event:danmaku"],
            }
        )

    requests = install_editorial_service(monkeypatch, replacing_worker, grounded=True)
    result = analyze_compute(task_id)
    assert not requests
    commit_highlight(_lease(task_id), result, 50)
    assert len(requests) == 1
    with get_session() as db:
        assert db.get(SegmentTask, task_id).lease_token == "new-owner"
        assert db.get(SegmentTask, task_id).stage == TaskStatus.ANALYZING
        assert db.get(HotspotEvent, event_id).title is None
        assert not db.exec(select(HighlightCandidate)).all()


def test_late_confirmation_commit_is_idempotent_and_keeps_review_gate(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline.scheduler import advance_candidate
    from app.pipeline.workers.analyze import analyze_compute, commit_highlight

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        event.status = HotspotStatus.PROVISIONAL
        db.add(event)
    _patch_compute_dependencies(monkeypatch)
    result = analyze_compute(task_id)
    commit_highlight(_lease(task_id), result, 50)
    commit_highlight(_lease(task_id), result, 50)
    advance_candidate()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.AWAITING_REVIEW
        assert task.candidate_id == db.get(HotspotEvent, event_id).candidate_id
        assert len(db.exec(select(HighlightCandidate)).all()) == len(db.exec(select(HighlightEvent)).all()) == 1
        assert len(db.exec(select(SegmentTask)).all()) == 1


def test_partial_candidate_batch_rolls_back_before_followup_failure(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    import pytest

    from app.analysis.clip_scorer import compute_hotspot_clip_draft
    from app.pipeline.workers.analyze import HighlightDecision, _commit_event_analysis

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        second = HotspotEvent(**event.model_dump(exclude={"id"}))
        second.event_key = "second-pending-event"
        second.start_ts = _START + timedelta(seconds=200)
        second.peak_ts = _START + timedelta(seconds=230)
        second.end_ts = _START + timedelta(seconds=260)
        db.add(second)
    draft = compute_hotspot_clip_draft(event_id)
    assert draft.decision == HighlightDecision.CANDIDATE

    from app.analysis.llm import BudgetAccountingError

    install_editorial_service(monkeypatch, lambda *_args, **_kwargs: "{}")
    # 模型目录变化使旧评分失效，按实际新目录重算后再测试事务回滚。
    draft = compute_hotspot_clip_draft(event_id)

    def fail_ledger_write(*_args: object) -> None:
        raise PermissionError("预算账本不可写")

    monkeypatch.setattr("app.analysis.llm.os.replace", fail_ledger_write)
    with pytest.raises(BudgetAccountingError):
        _commit_event_analysis(
            _lease(task_id),
            [{"event_id": event_id, "enrichment": None, "clip_draft": draft}],
            50,
        )
    with get_session() as db:
        assert not db.exec(select(HighlightCandidate)).all()
        assert not db.exec(select(HighlightEvent)).all()
        assert db.get(HotspotEvent, event_id).candidate_id is None
        assert db.get(SegmentTask, task_id).stage == TaskStatus.ANALYZING


def test_late_confirmation_retries_ledger_failure_without_orphan_candidate(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline import scheduler
    from app.pipeline.claiming import pop_and_claim

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        event.status = HotspotStatus.PROVISIONAL
        task = db.get(SegmentTask, task_id)
        task.stage = TaskStatus.QUEUED_FOR_ANALYSIS
        task.claimed_by = task.lease_token = None
        db.add(event)
        db.add(task)
    _patch_compute_dependencies(monkeypatch)

    install_editorial_service(monkeypatch, lambda *_args, **_kwargs: "{}")

    def fail_ledger_write(*_args: object) -> None:
        raise PermissionError("预算账本不可写")

    claim = pop_and_claim(TaskStatus.QUEUED_FOR_ANALYSIS)
    assert claim is not None
    with monkeypatch.context() as faults:
        faults.setattr("app.analysis.llm.os.replace", fail_ledger_write)
        scheduler.execute_task(claim.id, claim.stage, claim.lease_token)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.TRANSIENT_FAILED and task.failed_stage == TaskStatus.ANALYZING
        assert task.attempts == 1 and task.next_retry_at is not None
        assert not db.exec(select(HighlightCandidate)).all()
        assert not db.exec(select(HighlightEvent)).all()
    install_editorial_service(monkeypatch, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "now_utc", lambda: datetime.now(UTC) + timedelta(hours=1))
    scheduler.retry_expired()
    claim = pop_and_claim(TaskStatus.QUEUED_FOR_ANALYSIS)
    assert claim is not None
    scheduler.execute_task(claim.id, claim.stage, claim.lease_token)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.CANDIDATE_CREATED and task.candidate_id is not None
        assert len(db.exec(select(HighlightCandidate)).all()) == len(db.exec(select(SegmentTask)).all()) == 1


def test_evidence_changed_during_model_call_is_recomputed_before_scoring(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    from app.analysis import event_enricher
    from app.pipeline.workers.analyze import analyze_compute, commit_highlight

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)
    calls = 0

    def update_once(prompt: str, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            with get_session() as db:
                event = db.get(HotspotEvent, event_id)
                assert event is not None
                event.transcript_text = "主播介绍本轮挑战的新规则。"
                db.add(event)

    install_editorial_service(monkeypatch, update_once)
    result = analyze_compute(task_id)
    assert calls == 0
    commit_highlight(_lease(task_id), result, 50)
    assert calls == 4
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        assert event is not None and event.candidate_id is not None
        bundle = event_enricher.build_event_evidence_bundle(db, event_id)
        metadata = json.loads(event.features_json or "{}")
        assert metadata["event_enrichment"]["bundle_fingerprint"] == bundle.fingerprint
        assert metadata["event_clip_score"]["bundle_fingerprint"] == bundle.fingerprint
        assert "尚无可采用的内容摘要" in event.summary
        assert metadata["event_enrichment"]["error"] == "provider_error"
        assert len(metadata["event_enrichment"]["attempts"]) == 3
        assert metadata["editorial_review"]["execution"]["attempt_offset"] == 3
        assert metadata["editorial_review"]["execution"]["attempts"][0]["number"] == 4
        assert "新规则" in str(bundle.to_prompt_payload())
        assert db.get(SegmentTask, task_id).candidate_id == event.candidate_id
        assert len(db.exec(select(HighlightCandidate)).all()) == 1


def test_continuously_changing_evidence_uses_bounded_task_retry(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.pipeline import scheduler
    from app.pipeline.claiming import pop_and_claim

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None
        task.stage = TaskStatus.QUEUED_FOR_ANALYSIS
        task.claimed_by = task.lease_token = None
        db.add(task)
    _patch_compute_dependencies(monkeypatch)
    calls = 0

    def update_always(prompt: str, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        with get_session() as db:
            event = db.get(HotspotEvent, event_id)
            assert event is not None
            event.transcript_text = f"主播介绍第 {calls} 轮挑战的新规则。"
            db.add(event)

    install_editorial_service(monkeypatch, update_always)
    claim = pop_and_claim(TaskStatus.QUEUED_FOR_ANALYSIS)
    assert claim is not None
    scheduler.execute_task(claim.id, claim.stage, claim.lease_token)
    assert calls == 3
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None and task.stage == TaskStatus.TRANSIENT_FAILED
        assert task.attempts == 1 and task.next_retry_at is not None
        assert not task.error_is_permanent
        assert not db.exec(select(HighlightCandidate)).all()
        assert not db.exec(select(HighlightEvent)).all()
        assert db.get(HotspotEvent, event_id).title is None


@pytest.mark.parametrize("status", [HotspotStatus.DISMISSED, HotspotStatus.ENRICHING, "deleted", "merged"])
def test_target_changed_during_model_call_does_not_score_old_event(
    temp_db: None, monkeypatch: MonkeyPatch, status: str
) -> None:
    from app.pipeline.workers.analyze import _compute_event_clip_result

    _task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)
    changed = False

    def change_target(prompt: str, **kwargs: object) -> None:
        nonlocal changed
        if changed:
            return
        changed = True
        with get_session() as db:
            event = db.get(HotspotEvent, event_id)
            assert event is not None
            if status == "deleted":
                db.delete(event)
            else:
                if status == "merged":
                    canonical = HotspotEvent(**event.model_dump(exclude={"id"}))
                    canonical.event_key = "new-canonical-event"
                    db.add(canonical)
                    db.flush()
                    event.merged_into_id = canonical.id
                event.status = status
                db.add(event)

    install_editorial_service(monkeypatch, change_target)
    assert _compute_event_clip_result(event_id) is None
    with get_session() as db:
        assert not db.exec(select(HighlightCandidate)).all()
        assert not db.exec(select(HighlightEvent)).all()


@pytest.mark.parametrize("mismatch", ["event_id", "bundle_fingerprint"])
def test_commit_rejects_mixed_enrichment_and_clip_snapshots(
    temp_db: None, monkeypatch: MonkeyPatch, mismatch: str
) -> None:
    from app.pipeline.workers.analyze import _commit_event_analysis, _compute_event_clip_result

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)
    item = _compute_event_clip_result(event_id)
    assert item is not None
    item["enrichment"] = replace(item["enrichment"], **{mismatch: event_id + 1 if mismatch == "event_id" else "stale"})
    with pytest.raises(ValueError, match="同一事件和证据快照"):
        _commit_event_analysis(_lease(task_id), [item], 50)
    with get_session() as db:
        assert db.get(HotspotEvent, event_id).title is None
        assert db.get(SegmentTask, task_id).stage == TaskStatus.ANALYZING
        assert not db.exec(select(HighlightCandidate)).all()


def test_late_enrichment_does_not_overwrite_already_scored_event(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.pipeline.workers.analyze import analyze_compute, commit_highlight, score_hotspot_event

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)
    result = analyze_compute(task_id)
    candidate = score_hotspot_event(event_id)
    assert candidate is not None
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        assert event is not None
        event.title = "已经确认的标题"
        db.add(event)
    commit_highlight(_lease(task_id), result, 50)
    with get_session() as db:
        assert db.get(HotspotEvent, event_id).title == "已经确认的标题"
        assert len(db.exec(select(HighlightCandidate)).all()) == 1


@pytest.mark.parametrize("attempt_limit", [1, 8])
def test_merged_event_is_recomputed_as_canonical_before_task_completion(
    temp_db: None, monkeypatch: MonkeyPatch, attempt_limit: int
) -> None:
    from app.core.config import settings
    from app.pipeline.workers.analyze import analyze_compute, commit_highlight

    task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)
    monkeypatch.setattr(settings, "editorial_model_max_attempts", attempt_limit)
    canonical_id: int | None = None
    calls = 0

    def merge_once(prompt: str, **kwargs: object) -> None:
        nonlocal canonical_id, calls
        calls += 1
        if calls != 1:
            return
        with get_session() as db:
            event = db.get(HotspotEvent, event_id)
            assert event is not None
            canonical = HotspotEvent(**event.model_dump(exclude={"id"}))
            canonical.event_key = "merged-canonical-event"
            db.add(canonical)
            db.flush()
            canonical_id = canonical.id
            event.status = HotspotStatus.MERGED
            event.merged_into_id = canonical_id
            db.add(event)

    install_editorial_service(monkeypatch, merge_once)
    result = analyze_compute(task_id)
    assert calls == 0
    commit_highlight(_lease(task_id), result, 50)
    assert calls == min(4, attempt_limit) and canonical_id is not None
    with get_session() as db:
        canonical = db.get(HotspotEvent, canonical_id)
        task = db.get(SegmentTask, task_id)
        assert canonical is not None and canonical.candidate_id is not None
        metadata = json.loads(canonical.features_json)["event_enrichment"]
        assert len(metadata["attempts"]) == min(3, attempt_limit)
        editorial = json.loads(canonical.features_json)["editorial_review"]["execution"]
        assert len(metadata["attempts"]) + len(editorial["attempts"]) == calls
        assert metadata["error"] == ("attempt_budget" if attempt_limit == 1 else "provider_error")
        assert task is not None and task.stage == TaskStatus.CANDIDATE_CREATED
        assert task.candidate_id == canonical.candidate_id
        assert db.get(HotspotEvent, event_id).candidate_id is None
        assert len(db.exec(select(HighlightCandidate)).all()) == len(db.exec(select(HighlightEvent)).all()) == 1


def test_unrelated_value_error_is_not_treated_as_evidence_change(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.pipeline.workers.analyze import analyze_compute, commit_highlight

    task_id, _event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)
    calls = 0

    def invalid_model_configuration(prompt: str, **kwargs: object) -> str:
        from app.db.entities import AppSetting

        nonlocal calls
        calls += 1
        with get_session() as db:
            db.add(AppSetting(key="model_configuration", value="invalid-json"))
        return "{}"

    install_editorial_service(monkeypatch, invalid_model_configuration)
    result = analyze_compute(task_id)
    with pytest.raises(ValueError, match="模型配置无效"):
        commit_highlight(_lease(task_id), result, 50)
    assert calls == 1


def test_saved_enrichment_cannot_be_reused_after_evidence_changes(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.clip_scorer import compute_hotspot_clip_draft
    from app.analysis.event_enricher import EventEvidenceChangedError, enrich_hotspot_event
    from app.pipeline.workers.analyze import score_hotspot_event

    _task_id, event_id, _session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)
    assert enrich_hotspot_event(event_id) is not None
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        assert event is not None
        event.transcript_text = "主播公布新的比赛赛程。"
        db.add(event)
    with pytest.raises(EventEvidenceChangedError, match="已保存的事件补全"):
        compute_hotspot_clip_draft(event_id)
    candidate = score_hotspot_event(event_id)
    assert candidate is not None
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        assert event is not None and "尚无经过核验的内容摘要" in event.summary
        metadata = json.loads(event.features_json)["event_enrichment"]
        from app.analysis.event_enricher import build_event_evidence_bundle

        current = build_event_evidence_bundle(db, event_id)
        assert "新的比赛赛程" in str(current.to_prompt_payload())
        assert metadata["bundle_fingerprint"] == current.fingerprint
