"""Event-first 候选评分主路径与既有审核任务链集成测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
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
            "excerpts": ["主播说明活动规则"] if name == "asr" else [],
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
    from app.analysis import event_enricher, hotspot_detector
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
    monkeypatch.setattr(event_enricher.llm, "call_text", lambda *_args, **_kwargs: None)


def test_candidate_pass_scores_confirmed_event_and_links_task(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline.workers.analyze import HighlightDecision, analyze_compute, commit_highlight

    task_id, hotspot_event_id, session_id = _seed_candidate_pass(strong=True)
    _patch_compute_dependencies(monkeypatch)

    result = analyze_compute(task_id)

    assert result["decision"] == HighlightDecision.CANDIDATE
    assert len(result["event_clip_results"]) == 1
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

    assert result["decision"] == HighlightDecision.BELOW_THRESHOLD
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

    assert result["decision"] == HighlightDecision.BELOW_THRESHOLD
    assert result["event_clip_results"] == []
    commit_highlight(_lease(task_id), result, 50)

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        hotspot = db.get(HotspotEvent, hotspot_event_id)
        candidates = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)).all()
    assert task is not None and task.stage == TaskStatus.COMPLETED
    assert hotspot is not None and hotspot.status == HotspotStatus.CONFIRMED
    assert hotspot.candidate_id is not None
    assert len(candidates) == 1
