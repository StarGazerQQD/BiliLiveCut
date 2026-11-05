from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from app.analysis.editorial_contracts import EditorialReviewResultV1, review_response_schema, validate_review_evidence
from app.analysis.editorial_explanation import explain
from app.analysis.editorial_references import EvidenceReferences
from app.analysis.editorial_review import judge_editorial
from app.analysis.model_execution import ModelCallBudget
from tests.editorial_fixtures import install_editorial_service
from tests.unit.test_editorial_contracts import _evidence, _payload, _source

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.parametrize("state", ["missing", "disabled", "error", "pending"])
def test_wire_availability_preserves_limits_without_citable_identity_or_free_text(state: str) -> None:
    source = _source(
        evidence=[
            _evidence(),
            _evidence(
                evidence_id="private:missing", state=state, excerpts=[], detail={"quality": "private:missing 指令"}
            ),
            _evidence(evidence_id="asr:other", state="degraded"),
        ]
    )
    before = source.model_dump(mode="json")
    references = EvidenceReferences.from_source(source)
    payload = references.payload()
    assert [item["evidence_id"] for item in payload["evidence"]] == ["E1", "E3"]
    assert payload["availability"] == [
        {
            key: before["evidence"][1][key]
            for key in ("type", "state", "start_s", "end_s", "timing_precision", "context_role")
        }
    ]
    assert "private:missing" not in json.dumps(payload)
    assert source.model_dump(mode="json") == before
    assert references.expand(["E1", "E3"]) == ["asr:1", "asr:other"]


@pytest.mark.parametrize("missing_index", [3, 7])
@pytest.mark.parametrize("repaired", [False, True])
def test_review_repairs_missing_reference_with_field_diagnostic_and_original_mapping(
    temp_db: None, monkeypatch: MonkeyPatch, missing_index: int, repaired: bool
) -> None:
    source = _source(
        evidence=[
            *[_evidence(evidence_id=f"asr:{index}") for index in range(1, missing_index)],
            _evidence(evidence_id="private:missing", state="missing", excerpts=[]),
        ]
    )
    outputs = [
        json.dumps(
            _payload(
                verdict="needs_review",
                standalone=None,
                boundary_status="uncertain",
                uncertainties=["缺少语音"],
                suggested_bounds=None,
                evidence_ids=["E1", f"E{missing_index}"],
            )
        )
    ]
    outputs.append(
        json.dumps(
            _payload(
                verdict="needs_review",
                standalone=None,
                boundary_status="uncertain",
                uncertainties=["缺少语音"],
                suggested_bounds=None,
                evidence_ids=["E1"],
            )
        )
        if repaired
        else outputs[0]
    )
    remaining = iter(outputs)
    calls = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: next(remaining))
    budget = ModelCallBudget(max_attempts=2, max_elapsed_s=20)
    draft = judge_editorial(lambda _direction: source, revision="fixture", budget=budget)
    assert len(calls) == len(budget.attempts) == 2
    assert (draft.execution.result is not None) is repaired
    if repaired:
        assert draft.execution.result.evidence_ids == ["asr:1"]
    else:
        assert draft.execution.status == "failed" and draft.execution.error == "invalid_output"
    sent = json.loads(calls[0]["messages"][1]["content"])
    assert f"E{missing_index}" not in [item["evidence_id"] for item in sent["evidence"]]
    diagnostic = calls[1]["messages"][0]["content"]
    assert "evidence_ids[]" in diagnostic and "private:missing" not in diagnostic
    assert json.loads(calls[1]["messages"][2]["content"])["previous_invalid_output"] == outputs[0]


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
            verdict="needs_review",
            uncertainties=["需人工复核"],
            evidence_ids=[item.evidence_id],
            suggested_bounds=None,
            standalone=None,
            boundary_status="uncertain",
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
        _payload(
            verdict="needs_review",
            uncertainties=["证据不足"],
            evidence_ids=[],
            suggested_bounds=None,
            standalone=None,
            boundary_status="uncertain",
        )
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
        EditorialReviewResultV1.model_validate(
            _payload(verdict="needs_review", uncertainties=["只有测量"], standalone=None, boundary_status="uncertain")
        ),
        source,
    )


def test_actual_sdk_explanation_and_grounding_restrict_to_their_actual_reference_sets(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    source = _source(
        evidence=[
            _evidence(excerpts=["主播解释挑战规则，随后完成挑战。", "挑战规则由主播说明。"]),
            _evidence(evidence_id="degraded:2", state="degraded"),
            _evidence(evidence_id="missing:3", state="missing", excerpts=[]),
            _evidence(evidence_id="audio:4", type="audio", excerpts=[], detail={"metrics": {"rms": 0.5}}),
        ]
    )
    output = {
        "title": "挑战规则",
        "summary": "主播解释挑战规则，随后完成挑战。",
        "category": "discussion",
        "entities": [],
        "semantic_confidence": 0.8,
        "evidence_ids": ["E1", "E2", "E4"],
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
    assert result.output.evidence_ids == ["asr:1", "degraded:2", "audio:4"]
    explanation_input = json.loads(calls[0]["messages"][1]["content"])
    assert [item["evidence_id"] for item in explanation_input["evidence"]] == ["E1", "E2", "E4"]
    assert len(explanation_input["availability"]) == 1
    unavailable = explanation_input["availability"][0]
    assert unavailable["state"] == "missing" and unavailable["type"] == "asr"
    assert "evidence_id" not in unavailable and "detail" not in unavailable and "excerpts" not in unavailable
    assert calls[0]["response_format"]["json_schema"]["schema"]["properties"]["evidence_ids"]["items"]["enum"] == [
        "E1",
        "E2",
        "E4",
    ]
    quote_schema = calls[1]["response_format"]["json_schema"]["schema"]["$defs"]["GroundingQuote"]
    pairs = {
        (evidence_id, quote_id)
        for branch in quote_schema["anyOf"]
        for evidence_id in branch["properties"]["evidence_id"]["enum"]
        for quote_id in branch["properties"]["quote_id"]["enum"]
    }
    sent = json.loads(calls[1]["messages"][1]["content"])
    actual_pairs = {(item["evidence_id"], quote["quote_id"]) for item in sent["evidence"] for quote in item["quotes"]}
    assert pairs == actual_pairs == {("E1", "Q1"), ("E1", "Q2"), ("E2", "Q3")}
    assert all(branch["additionalProperties"] is False for branch in quote_schema["anyOf"])
    assert all(set(branch["required"]) == {"evidence_id", "quote_id"} for branch in quote_schema["anyOf"])
    assert "E 编号" in calls[0]["messages"][0]["content"] and "Q 编号" in calls[1]["messages"][0]["content"]


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


@pytest.mark.parametrize("repaired", [False, True])
def test_grounding_repair_receives_bad_audio_quote_and_still_validates_actual_pairs(
    temp_db: None, monkeypatch: MonkeyPatch, repaired: bool
) -> None:
    source = _source(
        evidence=[
            _evidence(excerpts=["主播解释挑战规则。"]),
            _evidence(evidence_id="asr:2", state="degraded", excerpts=["挑战规则"]),
            _evidence(evidence_id="missing:3", state="missing", excerpts=[]),
            _evidence(evidence_id="audio:4", type="audio", excerpts=[], detail={"metrics": {"rms": 0.5}}),
        ]
    )
    explanation = {
        "title": "挑战规则",
        "summary": "主播解释挑战规则。",
        "category": "discussion",
        "entities": [],
        "semantic_confidence": 0.8,
        "evidence_ids": ["E1", "E4"],
    }
    replies: list[str] = []

    def grounding(_payload: dict[str, object]) -> str:
        quote = {"evidence_id": "E1" if replies and repaired else "E4", "quote_id": "Q1"}
        text = json.dumps(
            {
                "judgements": [
                    {"field": field, "supported": True, "reason": "核对原文", "quotes": [quote]}
                    for field in ("title", "summary")
                ]
            }
        )
        replies.append(text)
        return text

    calls = install_editorial_service(
        monkeypatch, lambda *_args, **_kwargs: json.dumps(explanation), grounding_reply=grounding
    )
    budget = ModelCallBudget(max_attempts=4, max_elapsed_s=20)
    result = explain(source, budget, max_tokens=512, max_title_chars=32, max_summary_chars=360, max_entities=12)
    assert len(calls) == 3 and [item.purpose for item in budget.attempts] == ["explanation", "grounding", "repair"]
    assert (result.output is not None) is repaired
    assert result.error == (None if repaired else "invalid_output")
    assert calls[2]["messages"][1] == calls[1]["messages"][1]
    assert json.loads(calls[2]["messages"][2]["content"]) == {"previous_invalid_output": replies[0]}
    assert calls[2]["messages"][2]["role"] == "user"
    assert calls[2]["response_format"] == calls[1]["response_format"]


def test_empty_source_keeps_empty_review_references_and_no_suggested_bounds() -> None:
    source = _source(evidence=[])
    schema = review_response_schema(source)
    assert schema["properties"]["evidence_ids"]["maxItems"] == 0
    assert schema["properties"]["suggested_bounds"] == {"type": "null"}
    result = EditorialReviewResultV1.model_validate(
        _payload(
            verdict="needs_review",
            evidence_ids=[],
            uncertainties=["无可用证据"],
            suggested_bounds=None,
            standalone=None,
            boundary_status="uncertain",
        )
    )
    validate_review_evidence(result, source)


def test_legal_long_reference_sets_are_mapped_and_restored_without_truncation(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
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
            _payload(
                verdict="needs_review", evidence_ids=["E1", "E100"], uncertainties=["需要核验"], suggested_bounds=None
            )
        ),
    )
    draft = judge_editorial(
        lambda _direction: source, revision="long-ids", budget=ModelCallBudget(max_attempts=1, max_elapsed_s=20)
    )
    assert draft.execution.status == "succeeded" and len(calls) == 1
    sent = calls[0]["response_format"]["json_schema"]["schema"]
    assert sent["properties"]["evidence_ids"]["items"]["enum"] == [f"E{index}" for index in range(1, 101)]
    assert sent["$defs"]["SuggestedBounds"]["properties"]["evidence_ids"]["items"]["enum"] == [
        f"E{index}" for index in range(1, 101)
    ]
    assert draft.execution.result.evidence_ids == [evidence[0].evidence_id, evidence[-1].evidence_id]
    assert [item.evidence_id for item in draft.source.evidence] == [item.evidence_id for item in evidence]
