from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from app.analysis.clip_scorer import validate_event_clip_draft
from app.analysis.editorial_contracts import TimeRange, validate_review_evidence
from app.analysis.editorial_explanation import explain
from app.analysis.editorial_provenance import capture_provenance, frozen_review_fingerprint
from app.analysis.editorial_references import EvidenceReferences
from app.analysis.editorial_review import (
    editorial_auto_approvable,
    final_bounds_metadata,
    judge_editorial,
    review_policy_identity,
)
from app.analysis.editorial_review_sources import event_review_source
from app.analysis.event_enricher import build_event_evidence_bundle, commit_event_enrichment
from app.analysis.model_execution import ModelCallBudget
from app.core.config import settings
from app.db.entities import HotspotEvent, Transcript
from app.db.session import get_session
from app.pipeline.workers.analyze import _compute_event_clip_result
from app.web.services.editorial_view import editorial_presentation
from tests.editorial_fixtures import install_editorial_service
from tests.integration.test_editorial_snapshot_stability import _long_recording
from tests.unit.test_editorial_contracts import _evidence, _execution, _payload, _review, _source
from tests.unit.test_editorial_review import START


@pytest.mark.parametrize("suggestion", [None, {"start_s": 90.0, "end_s": 140.0, "evidence_ids": ["before"]}])
def test_outside_context_cannot_prove_a_complete_clip(suggestion: dict[str, object] | None) -> None:
    source = _source(evidence=[_evidence(evidence_id="before", context_role="before", start_s=60.0, end_s=90.0)])
    result = _review(evidence_ids=["before"], suggested_bounds=suggestion)
    with pytest.raises(ValueError, match="片内|范围|定位|没有交集"):
        validate_review_evidence(result, source)


def test_expanded_review_and_refreshed_explanation_use_identical_material(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id, segment_id = _long_recording(tmp_path)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        event.start_ts = START + timedelta(seconds=400)
        event.peak_ts = START + timedelta(seconds=410)
        event.end_ts = START + timedelta(seconds=420)
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        transcript.final_text = "前文介绍比赛计分方法和比赛规则。目标事件解释比赛计分方法。"
        transcript.words_json = json.dumps(
            [
                {"w": "前文介绍比赛计分方法和比赛规则。", "start": 50.0, "end": 60.0},
                {"w": "目标事件解释比赛计分方法。", "start": 400.0, "end": 420.0},
            ]
        )

    def respond(prompt: str, **_kwargs: object) -> str:
        data = json.loads(prompt.rsplit("\n", 1)[1])
        ids = [item["evidence_id"] for item in data["evidence"] if item["excerpts"]]
        if '"verdict"' in prompt:
            need_before = data["window"]["start_s"] > 0
            return json.dumps(
                _payload(
                    verdict="needs_review",
                    evidence_ids=ids[:1],
                    suggested_bounds=None,
                    standalone=None,
                    boundary_status="needs_expansion" if need_before else "uncertain",
                    missing_context="before" if need_before else None,
                    uncertainties=["仍需复核画面"],
                )
            )
        return json.dumps(
            {
                "title": "解释比赛规则",
                "summary": "介绍比赛的计分方法。",
                "category": "discussion",
                "entities": [],
                "semantic_confidence": 0.8,
                "evidence_ids": [item["evidence_id"] for item in data["evidence"] if item["context_role"] == "before"][
                    :1
                ]
                or ids[:1],
            }
        )

    monkeypatch.setattr(settings, "editorial_model_max_attempts", 8)
    calls = install_editorial_service(monkeypatch, respond, grounded=True)
    calculated = _compute_event_clip_result(event_id)
    assert calculated is not None
    review = calculated["review"]
    enrichment = calculated["enrichment"]
    assert review.expanded_context == "before" and enrichment is not None
    assert enrichment.semantic_confidence > 0.4
    assert len(calls) == 6
    assert [attempt.number for attempt in review.provenance.subsequent_attempts] == [5, 6]
    generated = [
        json.loads(call["messages"][1]["content"]) for call in calls if '"category"' in call["messages"][0]["content"]
    ]
    assert len(generated) == 2
    assert generated[0]["window"] != generated[1]["window"]
    with get_session() as db:
        expected = event_review_source(db, event_id, "before")
    assert expected is not None
    assert generated[1]["window"] == expected.window.model_dump(mode="json")
    assert generated[1]["evidence"] == EvidenceReferences.from_source(expected).payload()["evidence"]
    with get_session() as db:
        assert commit_event_enrichment(db, enrichment)
    calls.clear()
    cached = _compute_event_clip_result(event_id)
    assert cached is not None and cached["enrichment"] is None and len(calls) == 2
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        words = json.loads(transcript.words_json)
        words[0]["w"] = "前文介绍了新的比赛规则。"
        transcript.words_json = json.dumps(words)
    with get_session() as db:
        assert build_event_evidence_bundle(db, event_id).fingerprint == enrichment.bundle_fingerprint
        assert not commit_event_enrichment(db, enrichment)
        assert validate_event_clip_draft(db, calculated["clip_draft"], enrichment=enrichment, review=review) is None


def test_legacy_judgement_is_retained_without_confirming_actual_clip(temp_db: None) -> None:
    source = _source(evidence=[_evidence(start_s=None, end_s=None, timing_precision="unknown", context_role="unknown")])
    policy = {**review_policy_identity(), "policy": "editorial-decision-v3"}
    provenance = capture_provenance(policy, source, operation_id="a" * 32, prior_attempts=[])
    execution = _execution(
        policy_version="editorial-decision-v3",
        result=_review(suggested_bounds=None),
        input_fingerprint=frozen_review_fingerprint(source, source, "legacy", policy=policy),
    )
    metadata = {
        "execution": execution.model_dump(mode="json"),
        "input": source.model_dump(mode="json"),
        "provenance": provenance.model_dump(mode="json"),
        "revision": "legacy",
    }
    view = editorial_presentation(metadata=metadata)
    assert view.execution is not None and view.execution.result.standalone is True
    assert view.editorial_state == "succeeded" and view.clip_standalone is None
    assert not editorial_auto_approvable(metadata)


@pytest.mark.parametrize("applied", [False, True])
def test_final_bounds_confirmation_requires_actual_application(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, applied: bool
) -> None:
    install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            _payload(evidence_ids=["E1"], suggested_bounds={"start_s": 85.0, "end_s": 140.0, "evidence_ids": ["E1"]})
        ),
    )
    draft = judge_editorial(
        lambda _direction: _source(), revision="bounds", budget=ModelCallBudget(max_attempts=2, max_elapsed_s=30)
    )
    assert draft.execution.result is not None and draft.execution.result.standalone is True
    actual = TimeRange(start_s=85.0 if applied else 90.0, end_s=140.0 if applied else 130.0)
    confirmed = final_bounds_metadata(draft, actual, media_accessible=True, suggestion_applied=applied)
    assert confirmed["clip_standalone"] is (True if applied else None)
    assert confirmed["bounds_complete"] is applied


def test_each_stage_can_repair_once_within_the_original_shared_budget(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = {"explanation": 0, "grounding": 0, "review": 0}

    def respond(prompt: str, **_kwargs: object) -> str:
        stage = "review" if '"verdict"' in prompt else "explanation"
        counts[stage] += 1
        if counts[stage] == 1:
            return "invalid-json"
        if stage == "review":
            return json.dumps(_payload(evidence_ids=["E1"], suggested_bounds=None))
        return json.dumps(
            {
                "title": "挑战规则",
                "summary": "主播解释挑战规则。",
                "category": "discussion",
                "entities": [],
                "semantic_confidence": 0.8,
                "evidence_ids": ["E1"],
            }
        )

    def grounding(payload: dict[str, object]) -> str:
        counts["grounding"] += 1
        if counts["grounding"] == 1:
            return "invalid-json"
        evidence = payload["evidence"][0]
        return json.dumps(
            {
                "judgements": [
                    {
                        "field": field,
                        "supported": True,
                        "reason": "原文支持",
                        "quotes": [
                            {"evidence_id": evidence["evidence_id"], "quote_id": evidence["quotes"][0]["quote_id"]}
                        ],
                    }
                    for field in ("title", "summary")
                ]
            }
        )

    calls = install_editorial_service(monkeypatch, respond, grounding_reply=grounding)
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=30)
    generated = explain(_source(), budget, max_tokens=512, max_title_chars=32, max_summary_chars=360, max_entities=12)
    assert generated.output is not None
    reviewed = judge_editorial(lambda _direction: _source(), revision="shared", budget=budget)
    assert reviewed.execution.status == "succeeded" and len(calls) == 6
    assert budget.remaining_requests == 2
    assert [attempt.purpose for attempt in budget.attempts] == [
        "explanation",
        "repair",
        "grounding",
        "repair",
        "review",
        "repair",
    ]
