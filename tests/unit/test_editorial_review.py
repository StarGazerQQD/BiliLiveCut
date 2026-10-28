from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlmodel import select

from app.analysis.editorial_contracts import EditorialReviewInputV1
from app.analysis.editorial_review import editorial_auto_approvable, judge_editorial
from app.analysis.editorial_review_sources import compute_event_review, event_review_is_current
from app.analysis.model_execution import ModelCallBudget
from app.db.entities import (
    AppSetting,
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    HotspotStatus,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    Transcript,
)
from app.db.session import get_session
from tests.editorial_fixtures import install_editorial_service
from tests.unit.test_editorial_contracts import _evidence, _payload, _source

if TYPE_CHECKING:
    from pytest import MonkeyPatch

START = datetime(2026, 10, 26, 10)
TEXT = "主播解释比赛规则，说明每轮怎样计分以及最终胜负。"


def _budget(attempts: int = 8) -> ModelCallBudget:
    return ModelCallBudget(max_attempts=attempts, max_elapsed_s=30.0)


def _service(monkeypatch: MonkeyPatch, verdict: str) -> list[dict[str, object]]:
    def respond(prompt: str, **_kwargs: object) -> str:
        data = json.loads(prompt.rsplit("\n", 1)[1])
        ids = [item["evidence_id"] for item in data["evidence"] if item["excerpts"]]
        if '"verdict"' in prompt:
            return json.dumps(
                _payload(
                    verdict=verdict,
                    evidence_ids=ids[:1],
                    hook="比赛规则和计分方法",
                    reason="规则解释可供观众理解比赛。",
                    suggested_bounds=None,
                    standalone=None if verdict == "needs_review" else True,
                    boundary_status="uncertain" if verdict == "needs_review" else "complete",
                    uncertainties=["需结合画面确认"] if verdict == "needs_review" else [],
                ),
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "title": "主播解释比赛规则",
                "summary": TEXT,
                "category": "discussion",
                "entities": [],
                "semantic_confidence": 0.8,
                "evidence_ids": ids[:1],
            },
            ensure_ascii=False,
        )

    return install_editorial_service(monkeypatch, respond, grounded=True)


def _seed(media: Path, *, heat: float = 0.1) -> tuple[int, int]:
    with get_session() as db:
        room = LiveRoom(input_url="editorial-review", room_id=9541, highlight_threshold=0.8, auto_approve=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, started_at=START)
        db.add(session)
        db.flush()
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path=str(media),
            start_ts=START,
            end_ts=START + timedelta(seconds=8.336),
            duration_s=8.336,
        )
        db.add(segment)
        db.flush()
        db.add(
            Transcript(
                segment_id=segment.id,
                final_text=TEXT,
                words_json=json.dumps(
                    [
                        {"w": TEXT, "start": 2.0, "end": 6.0},
                    ]
                ),
            )
        )
        event = HotspotEvent(
            event_key="editorial:one",
            session_id=session.id,
            start_ts=START + timedelta(seconds=2),
            peak_ts=START + timedelta(seconds=4),
            end_ts=START + timedelta(seconds=6),
            status=HotspotStatus.CONFIRMED,
            heat_score=heat,
            clip_score=heat,
            evidence_coverage=0.9,
            transcript_text=TEXT,
            evidence_json='{"items":[]}',
        )
        db.add(event)
        db.flush()
        assert event.id is not None and segment.id is not None
        return event.id, segment.id


@pytest.mark.parametrize("verdict,expected", [("recommend", True), ("reject", False), ("needs_review", True)])
def test_hotspot_editorial_verdict_controls_candidate(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    verdict: str,
    expected: bool,
) -> None:
    from app.pipeline.workers.analyze import score_hotspot_event

    event_id, _ = _seed(pcm_wav, heat=0.95 if verdict == "reject" else 0.1)
    requests = _service(monkeypatch, verdict)
    candidate = score_hotspot_event(event_id)
    assert (candidate is not None) is expected
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        features = json.loads(event.features_json)
    execution = features["editorial_review"]["execution"]
    assert execution["result"]["verdict"] == verdict
    assert execution["result"]["interest_rating"] == 3
    assert execution["attempt_offset"] == 2
    assert execution["attempts"][0]["number"] == 3
    assert len(requests) == 3
    assert "event_completeness" not in features["event_clip_score"]["components"]
    if candidate is not None:
        assert candidate.highlight_score < 0.8
        assert candidate.status == "pending"


def test_event_review_detects_changed_input_before_commit(
    temp_db: None, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    event_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    draft = compute_event_review(event_id, _budget())
    with get_session() as db:
        assert event_review_is_current(db, event_id, draft)
        event = db.get(HotspotEvent, event_id)
        event.transcript_text = "主播讨论另一场活动的参与方式。"
        db.add(event)
    with get_session() as db:
        assert not event_review_is_current(db, event_id, draft)


def test_disabled_review_preserves_unknown_and_has_no_requests(temp_db: None) -> None:
    draft = judge_editorial(lambda _direction: _source(), revision="test", budget=_budget())
    assert draft.execution.status == "disabled"
    assert draft.execution.result is None
    assert draft.execution.attempts == []
    assert not editorial_auto_approvable(draft.metadata())


def test_audio_measurement_cannot_become_definitive_editorial_verdict(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    source = _source(evidence=[_evidence(type="audio", excerpts=[], detail={"metrics": {"rms": 0.8}})])
    requests = install_editorial_service(
        monkeypatch, lambda *_args, **_kwargs: json.dumps(_payload(suggested_bounds=None))
    )
    draft = judge_editorial(lambda _direction: source, revision="test", budget=_budget())
    assert draft.execution.status == "failed"
    assert draft.execution.error == "invalid_output"
    assert draft.execution.result is None
    assert len(requests) == 2


def test_context_extension_shared_budget_and_single_expansion(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    original = _source()
    expanded = _source(
        window={"start_s": 0.0, "end_s": 160.0},
        evidence=[
            _evidence(),
            _evidence(
                evidence_id="before:1", start_s=10.0, end_s=50.0, context_role="before", excerpts=["前文介绍比赛规则"]
            ),
        ],
    )
    loaded: list[str | None] = []

    def load(direction: str | None) -> EditorialReviewInputV1:
        loaded.append(direction)
        return original if direction is None else expanded

    calls = install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            _payload(
                verdict="needs_review",
                standalone=None,
                boundary_status="needs_expansion",
                missing_context="before",
                suggested_bounds=None,
                uncertainties=["缺少比赛结果"],
            )
        ),
    )
    budget = _budget(2)
    draft = judge_editorial(load, revision="test", budget=budget)
    assert loaded == [None, "before"]
    assert len(calls) == 2
    assert [
        call["response_format"]["json_schema"]["schema"]["properties"]["evidence_ids"]["items"]["enum"]
        for call in calls
    ] == [["asr:1"], ["asr:1", "before:1"]]
    assert draft.expanded_context == "before"
    assert draft.execution.result.verdict == "needs_review"
    assert not editorial_auto_approvable(draft.metadata())
    assert budget.claim_context() is False
    again = judge_editorial(load, revision="test", budget=budget)
    assert again.execution.error == "attempt_budget" and len(calls) == 2


@pytest.mark.parametrize("verdict", ["reject", "needs_review"])
def test_human_approval_overrides_machine_without_changing_machine_record(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    verdict: str,
) -> None:
    from app.pipeline.approval import approve_event_and_task

    event_id, segment_id = _seed(pcm_wav)
    _service(monkeypatch, verdict)
    review = compute_event_review(event_id, _budget())
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        candidate = HighlightCandidate(
            session_id=segment.session_id,
            start_ts=START,
            end_ts=segment.end_ts,
            peak_ts=START + timedelta(seconds=4),
            highlight_score=1.0,
            dedup_hash="test-review",
        )
        db.add(candidate)
        db.flush()
        event = HighlightEvent(
            candidate_id=candidate.id,
            session_id=segment.session_id,
            features_json=json.dumps({"editorial_review": review.metadata()}),
        )
        db.add(event)
        db.flush()
        review_id = event.id
    assert not approve_event_and_task(task_id=None, event_id=review_id, source="auto")
    assert approve_event_and_task(task_id=None, event_id=review_id, source="human", approved_by="test")
    with get_session() as db:
        event = db.get(HighlightEvent, review_id)
        assert event.review_status == ReviewStatus.APPROVED_SOLO
        assert json.loads(event.features_json)["editorial_review"]["execution"]["result"]["verdict"] == verdict


@pytest.mark.parametrize("verdict,expected", [("recommend", True), ("reject", False)])
def test_legacy_segment_calls_editorial_before_heat_filter_and_persists_rejections(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    verdict: str,
    expected: bool,
) -> None:
    from app.pipeline.workers.analyze import score_segment_direct

    _, segment_id = _seed(pcm_wav)
    calls = _service(monkeypatch, verdict)
    candidate = score_segment_direct(segment_id)
    assert (candidate is not None) is expected
    assert calls
    with get_session() as db:
        record = db.get(AppSetting, f"editorial_segment:{segment_id}")
        records = json.loads(record.value)["windows"]
        candidates = db.exec(select(HighlightCandidate)).all()
    assert records and all(item["editorial_review"]["execution"]["result"]["verdict"] == verdict for item in records)
    assert len(candidates) == int(expected)
    if candidate is not None:
        assert candidate.highlight_score < 0.8


@pytest.mark.parametrize("media_removed", [False, True])
def test_complete_recommendation_rechecks_media_at_auto_approval(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
    media_removed: bool,
) -> None:
    from app.pipeline.approval import approve_event_and_task
    from app.pipeline.workers.analyze import score_hotspot_event

    event_id, _ = _seed(pcm_wav, heat=1.0)
    with get_session() as db:
        room = db.exec(select(LiveRoom)).one()
        room.auto_approve_threshold = 0.2
        db.add(room)
    _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(event_id)
    assert candidate is not None
    with get_session() as db:
        event = db.exec(select(HighlightEvent)).one()
        review_id = event.id
        assert editorial_auto_approvable(json.loads(event.features_json)["editorial_review"])
    if media_removed:
        pcm_wav.unlink()
    assert approve_event_and_task(task_id=None, event_id=review_id, source="auto") is not media_removed


def test_no_asr_still_requests_review_using_real_audio_evidence(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.pipeline.workers.analyze import _score_segment_draft

    _, segment_id = _seed(pcm_wav)
    with get_session() as db:
        transcript = db.exec(select(Transcript)).one()
        db.delete(transcript)
    calls = install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            _payload(
                verdict="needs_review",
                evidence_ids=[f"segment:{segment_id}:audio"],
                interest_rating=None,
                standalone=None,
                boundary_status="uncertain",
                suggested_bounds=None,
                uncertainties=["缺少语义和画面"],
            )
        ),
    )
    result = _score_segment_draft(segment_id)
    assert result is not None and result["decision"] == "candidate"
    metadata = json.loads(result["features_json"])
    assert metadata["editorial_review"]["execution"]["result"]["verdict"] == "needs_review"
    assert metadata["semantic_completeness"] is None
    assert result["initial_status"] == "pending" and len(calls) == 1


def test_legacy_changed_transcript_does_not_store_stale_snapshot(
    temp_db: None,
    pcm_wav: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis.editorial_review_sources import store_segment_reviews
    from app.pipeline.workers.analyze import _score_segment_draft

    _, segment_id = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    result = _score_segment_draft(segment_id)
    with get_session() as db:
        assert result["editorial_snapshot"].is_current(db)
        transcript = db.exec(select(Transcript)).one()
        transcript.words_json = json.dumps([{"w": "另一个讨论内容", "start": 2.0, "end": 6.0}])
        db.add(transcript)
    with get_session() as db:
        assert not store_segment_reviews(db, segment_id, [result["editorial_snapshot"]])
        assert db.get(AppSetting, f"editorial_segment:{segment_id}") is None


def test_editorial_transport_failure_keeps_real_failure_not_business_reject(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    requests = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: None)
    draft = judge_editorial(lambda _direction: _source(), revision="test", budget=_budget())
    assert draft.execution.error == "provider_error"
    assert draft.execution.status == "failed" and draft.execution.result is None
    assert len(requests) == len(draft.execution.attempts) == 1


def test_context_expansion_without_new_evidence_does_not_make_second_request(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    calls = install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            _payload(
                verdict="needs_review",
                standalone=None,
                boundary_status="needs_expansion",
                missing_context="after",
                suggested_bounds=None,
                uncertainties=["缺少后文"],
            )
        ),
    )
    draft = judge_editorial(lambda _direction: _source(), revision="test", budget=_budget())
    assert draft.expanded_context is None and len(calls) == 1
    assert draft.execution.result.missing_context == "after"
