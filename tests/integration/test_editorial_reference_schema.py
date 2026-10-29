from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from app.analysis.editorial_contracts import EditorialReviewResultV1, review_response_schema, validate_review_evidence
from app.analysis.editorial_explanation import explain
from app.analysis.model_execution import ModelCallBudget
from tests.editorial_fixtures import install_editorial_service
from tests.unit.test_editorial_contracts import _evidence, _payload, _source

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize("state", ["available", "degraded", "missing", "disabled", "error", "pending"])
@pytest.mark.parametrize("precision", ["word", "segment", "event", "unknown"])
def test_reference_schema_matches_local_availability_and_time_rules(state: str, precision: str) -> None:
    usable = state in {"available", "degraded"}
    known = precision != "unknown"
    item = _evidence(
        state=state,
        excerpts=["比赛规则"] if usable else [],
        timing_precision=precision,
        start_s=90.0 if known else None,
        end_s=130.0 if known else None,
        context_role="event" if known else "unknown",
    )
    source = _source(evidence=[item])
    schema = review_response_schema(source)
    references = schema["properties"]["evidence_ids"]
    result = EditorialReviewResultV1.model_validate(
        _payload(
            verdict="needs_review", uncertainties=["需人工复核"], evidence_ids=[item.evidence_id], suggested_bounds=None
        )
    )
    if usable:
        assert references["items"]["enum"] == [item.evidence_id]
        validate_review_evidence(result, source)
    else:
        assert references["maxItems"] == 0 and "enum" not in references["items"]
        with pytest.raises(ValueError, match="不存在或不可用"):
            validate_review_evidence(result, source)
    empty = EditorialReviewResultV1.model_validate(
        _payload(verdict="needs_review", uncertainties=["证据不足"], evidence_ids=[], suggested_bounds=None)
    )
    validate_review_evidence(empty, source)
    with_bounds = result.model_copy(
        update={"suggested_bounds": EditorialReviewResultV1.model_validate(_payload()).suggested_bounds}
    )
    if usable and known:
        assert schema["$defs"]["SuggestedBounds"]["properties"]["evidence_ids"]["items"]["enum"] == [item.evidence_id]
        validate_review_evidence(with_bounds, source)
    else:
        assert schema["properties"]["suggested_bounds"] == {"type": "null"}
        with pytest.raises(ValueError):
            validate_review_evidence(with_bounds, source)


def test_point_timed_metric_remains_eligible_for_needs_review_bounds() -> None:
    item = _evidence(type="audio", state="degraded", excerpts=[], detail={"metrics": {"rms": 0.5}}, end_s=90.0)
    source = _source(evidence=[item])
    schema = review_response_schema(source)
    assert schema["properties"]["evidence_ids"]["items"]["enum"] == [item.evidence_id]
    assert schema["$defs"]["SuggestedBounds"]["properties"]["evidence_ids"]["items"]["enum"] == [item.evidence_id]
    validate_review_evidence(
        EditorialReviewResultV1.model_validate(_payload(verdict="needs_review", uncertainties=["只有测量"])), source
    )


def test_actual_sdk_explanation_and_grounding_restrict_to_their_actual_reference_sets(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    source = _source(
        evidence=[
            _evidence(),
            _evidence(evidence_id="degraded:2", state="degraded"),
            _evidence(evidence_id="missing:3", state="missing", excerpts=[]),
        ]
    )
    output = {
        "title": "挑战规则",
        "summary": "主播解释挑战规则，随后完成挑战。",
        "category": "discussion",
        "entities": [],
        "semantic_confidence": 0.8,
        "evidence_ids": ["asr:1"],
    }
    calls = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: json.dumps(output), grounded=True)
    result = explain(
        source,
        ModelCallBudget(max_attempts=2, max_elapsed_s=20),
        max_tokens=512,
        max_title_chars=32,
        max_summary_chars=360,
        max_entities=12,
    )
    assert result.output is not None and len(calls) == 2
    assert calls[0]["response_format"]["json_schema"]["schema"]["properties"]["evidence_ids"]["items"]["enum"] == [
        "asr:1",
        "degraded:2",
    ]
    assert calls[1]["response_format"]["json_schema"]["schema"]["$defs"]["GroundingQuote"]["properties"]["evidence_id"][
        "enum"
    ] == ["asr:1"]
    assert "逐字复制" in calls[0]["messages"][0]["content"] and "逐字复制" in calls[1]["messages"][0]["content"]


def test_explanation_without_citable_evidence_stops_before_network(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    source = _source(evidence=[_evidence(state="missing", excerpts=[])])
    calls = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: "{}")
    result = explain(
        source,
        ModelCallBudget(max_attempts=2, max_elapsed_s=20),
        max_tokens=512,
        max_title_chars=32,
        max_summary_chars=360,
        max_entities=12,
    )
    assert result.output is None and result.error == "context_unavailable" and not calls


def test_empty_source_keeps_empty_review_references_and_no_suggested_bounds() -> None:
    source = _source(evidence=[])
    schema = review_response_schema(source)
    assert schema["properties"]["evidence_ids"]["maxItems"] == 0
    assert schema["properties"]["suggested_bounds"] == {"type": "null"}
    result = EditorialReviewResultV1.model_validate(
        _payload(verdict="needs_review", evidence_ids=[], uncertainties=["无可用证据"], suggested_bounds=None)
    )
    validate_review_evidence(result, source)


def test_legal_long_reference_sets_are_sent_without_truncating_ids(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    from app.analysis.editorial_contracts import INPUT_MAX_BYTES, SCHEMA_MAX_BYTES
    from app.analysis.editorial_review import judge_editorial

    evidence = [_evidence(evidence_id=f"id{index:03d}:" + "x" * 240, excerpts=["规则"]) for index in range(100)]
    source = _source(evidence=evidence)
    schema = review_response_schema(source)
    assert len(source.model_dump_json().encode()) <= INPUT_MAX_BYTES
    schema_size = len(json.dumps(schema, separators=(",", ":")).encode())
    assert 16384 < schema_size < SCHEMA_MAX_BYTES
    calls = install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            _payload(verdict="needs_review", evidence_ids=[], uncertainties=["需要核验"], suggested_bounds=None)
        ),
    )
    draft = judge_editorial(
        lambda _direction: source, revision="long-ids", budget=ModelCallBudget(max_attempts=1, max_elapsed_s=20)
    )
    assert draft.execution.status == "succeeded" and len(calls) == 1
    sent = calls[0]["response_format"]["json_schema"]["schema"]
    assert sent["properties"]["evidence_ids"]["items"]["enum"] == [item.evidence_id for item in evidence]
    assert sent["$defs"]["SuggestedBounds"]["properties"]["evidence_ids"]["items"]["enum"] == [
        item.evidence_id for item in evidence
    ]
