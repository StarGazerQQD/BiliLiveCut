from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.analysis.editorial_contracts import (
    EXCERPT_MAX_CHARS,
    INPUT_MAX_BYTES,
    OUTPUT_MAX_BYTES,
    EditorialEvidence,
    EditorialExecutionV1,
    EditorialReviewInputV1,
    EditorialReviewResultV1,
    EvidenceDetail,
    ModelAttempt,
    TimeRange,
    TokenUsage,
    parse_editorial_review,
    validate_review_evidence,
)


def _evidence(**changes: object) -> EditorialEvidence:
    return EditorialEvidence.model_validate(
        {
            "evidence_id": "asr:1",
            "type": "asr",
            "state": "available",
            "start_s": 90.0,
            "end_s": 130.0,
            "timing_precision": "word",
            "context_role": "event",
            "excerpts": ["主播解释挑战规则，随后完成挑战。"],
            **changes,
        }
    )


def _source(**changes: object) -> EditorialReviewInputV1:
    return EditorialReviewInputV1.model_validate(
        {
            "target": {
                "kind": "hotspot",
                "session_id": 7,
                "target_id": 12,
                "target_key": "event:12",
                "bounds": {"start_s": 90.0, "end_s": 130.0},
                "peak_s": 110.0,
            },
            "window": {"start_s": 60.0, "end_s": 160.0},
            "recording_ranges": [{"start_s": 0.0, "end_s": 300.0}],
            "truncated": False,
            "audience_profile": "直播原有观众与普通观众",
            "preference_version": "audience-v1",
            "evidence": [_evidence()],
            **changes,
        }
    )


def _payload(**changes: object) -> dict[str, object]:
    return {
        "schema_version": "editorial-review-v1",
        "verdict": "recommend",
        "interest_rating": 3,
        "hook": "从解释挑战规则到完成挑战",
        "reason": "提供了完整的挑战说明与结果。",
        "evidence_ids": ["asr:1"],
        "standalone": True,
        "boundary_status": "complete",
        "suggested_bounds": {"start_s": 85.0, "end_s": 140.0, "evidence_ids": ["asr:1"]},
        "missing_context": None,
        "uncertainties": [],
        **changes,
    }


def _review(**changes: object) -> EditorialReviewResultV1:
    return EditorialReviewResultV1.model_validate(_payload(**changes))


def _attempt(**changes: object) -> ModelAttempt:
    return ModelAttempt.model_validate(
        {
            "number": 1,
            "purpose": "review",
            "provider_id": "route:analysis",
            "model": "configured-model",
            "protocol": "json_schema",
            "status": "success",
            "error": None,
            "elapsed_s": 0.5,
            **changes,
        }
    )


def _execution(**changes: object) -> EditorialExecutionV1:
    return EditorialExecutionV1.model_validate(
        {
            "status": "succeeded",
            "input_fingerprint": "a" * 64,
            "policy_version": "editorial-v1",
            "result": _review(),
            "error": None,
            "attempts": [_attempt()],
            "elapsed_s": 1.0,
            **changes,
        }
    )


def test_valid_result_survives_json_roundtrip_and_reference_validation() -> None:
    source = EditorialReviewInputV1.model_validate_json(_source().model_dump_json())
    result = parse_editorial_review(json.dumps(_payload(), ensure_ascii=False), source)
    assert result.verdict == "recommend"
    assert result.interest_rating == 3
    assert result.suggested_bounds is not None
    assert (result.suggested_bounds.start_s, result.suggested_bounds.end_s) == (85.0, 140.0)
    assert source.time_base == "session_seconds"
    assert len(source.model_dump_json().encode()) < INPUT_MAX_BYTES


@pytest.mark.parametrize("value", [True, False, 2.5, "3", -1, 5, float("nan"), float("inf")])
def test_interest_rating_rejects_coercion_and_out_of_range(value: object) -> None:
    with pytest.raises(ValidationError):
        _review(interest_rating=value)


@pytest.mark.parametrize("value", [True, "10", -0.1, float("nan"), float("inf"), float("-inf")])
def test_time_rejects_invalid_and_nonfinite_values(value: object) -> None:
    with pytest.raises(ValidationError):
        TimeRange.model_validate({"start_s": value, "end_s": 20.0})


@pytest.mark.parametrize("bounds", [(20.0, 20.0), (20.0, 19.0)])
def test_time_rejects_empty_and_reversed_windows(bounds: tuple[float, float]) -> None:
    with pytest.raises(ValidationError, match="终点必须晚于起点"):
        TimeRange(start_s=bounds[0], end_s=bounds[1])


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": "字段"},
        {"schema_version": "editorial-review-v2"},
        {"reason": " "},
        {"verdict": "maybe"},
        {"interest_rating": None},
        {"hook": " "},
        {"standalone": "true"},
        {"standalone": None},
        {"uncertainties": ["x" * 241]},
        {"hook": "x" * 121},
        {"reason": "x" * 241},
        {"evidence_ids": ["asr:1", "asr:1"]},
        {"evidence_ids": []},
        {"missing_context": "before"},
        {"boundary_status": "needs_expansion"},
        {"verdict": "needs_review"},
    ],
)
def test_result_rejects_invalid_fields_and_contradictions(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _review(**changes)


@pytest.mark.parametrize("direction", ["before", "after", "both"])
def test_missing_context_is_structured_and_preserves_unknowns(direction: str) -> None:
    result = _review(
        verdict="needs_review",
        interest_rating=None,
        hook="",
        evidence_ids=[],
        standalone=None,
        boundary_status="needs_expansion",
        suggested_bounds=None,
        missing_context=direction,
        uncertainties=["缺少对应上下文，不能判定是否独立完整。"],
    )
    assert validate_review_evidence(result, _source(evidence=[])).missing_context == direction
    assert result.interest_rating is None
    assert result.standalone is None


def test_no_asr_does_not_prevent_supported_editorial_recommendation() -> None:
    source = _source(evidence=[_evidence(type="visual", excerpts=["实际视觉输入记录了挑战结果。"])])
    assert validate_review_evidence(_review(), source).verdict == "recommend"


def test_reject_requires_available_supporting_evidence() -> None:
    assert (
        validate_review_evidence(_review(verdict="reject", hook="", interest_rating=0), _source()).verdict == "reject"
    )
    with pytest.raises(ValueError, match="不存在或不可用"):
        validate_review_evidence(_review(verdict="reject"), _source(evidence=[]))


@pytest.mark.parametrize("state", ["missing", "disabled", "error", "pending"])
def test_unavailable_evidence_cannot_be_quoted_or_referenced(state: str) -> None:
    with pytest.raises(ValidationError, match="不可用证据"):
        _evidence(state=state)
    source = _source(evidence=[_evidence(state=state, excerpts=[])])
    with pytest.raises(ValueError, match="不存在或不可用"):
        validate_review_evidence(_review(), source)


def test_degraded_only_evidence_requires_review_instead_of_certainty() -> None:
    source = _source(evidence=[_evidence(state="degraded")])
    with pytest.raises(ValueError, match="仅有降级"):
        validate_review_evidence(_review(), source)
    result = _review(verdict="needs_review", uncertainties=["转写质量低，需要核对原始录播。"])
    assert validate_review_evidence(result, source).verdict == "needs_review"


def test_unknown_time_stays_null_and_cannot_support_boundary_suggestion() -> None:
    item = _evidence(start_s=None, end_s=None, timing_precision="unknown", context_role="unknown")
    assert item.start_s is None and item.end_s is None
    with pytest.raises(ValueError, match="没有时间定位"):
        validate_review_evidence(_review(), _source(evidence=[item]))
    assert validate_review_evidence(_review(suggested_bounds=None), _source(evidence=[item])).verdict == "recommend"
    with pytest.raises(ValidationError, match="空时间"):
        _evidence(timing_precision="unknown")


@pytest.mark.parametrize(
    "changes",
    [
        {"start_s": None},
        {"end_s": 80.0},
        {"start_s": True},
        {"evidence_id": " "},
        {"excerpts": ["x" * (EXCERPT_MAX_CHARS + 1)]},
        {"detail": {"api_key": "must-not-be-accepted"}},
    ],
)
def test_evidence_rejects_invalid_time_text_and_unapproved_details(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _evidence(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"window": {"start_s": 0.0, "end_s": 89.0}},
        {"window": {"start_s": 91.0, "end_s": 130.0}},
        {"recording_ranges": [{"start_s": 0.0, "end_s": 100.0}, {"start_s": 120.0, "end_s": 300.0}]},
        {"recording_ranges": [{"start_s": 0.0, "end_s": 180.0}, {"start_s": 150.0, "end_s": 300.0}]},
        {"recording_ranges": [{"start_s": 0.0, "end_s": 180.0}, {"start_s": 180.0, "end_s": 300.0}]},
        {"recording_ranges": []},
        {"truncated": "false"},
    ],
)
def test_input_rejects_missing_peak_unmarked_truncation_and_recording_gaps(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _source(**changes)


def test_input_allows_declared_event_truncation_without_inventing_evidence() -> None:
    source = _source(window={"start_s": 100.0, "end_s": 150.0}, truncated=True, evidence=[])
    assert source.truncated is True
    assert source.target.bounds.start_s == 90.0


@pytest.mark.parametrize(
    "item",
    [
        {"start_s": 59.0, "context_role": "mixed"},
        {"end_s": 170.0, "context_role": "mixed"},
        {"context_role": "before"},
        {"context_role": "after"},
        {"start_s": 80.0},
    ],
)
def test_input_rejects_evidence_outside_window_or_false_context_role(item: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _source(evidence=[_evidence(**item)])


def test_input_rejects_duplicate_evidence_ids() -> None:
    with pytest.raises(ValidationError, match="ID 不能重复"):
        _source(evidence=[_evidence(), _evidence()])


def test_total_budget_includes_detail_and_serialization_overhead() -> None:
    evidence = [
        _evidence(
            evidence_id=f"asr:{index}",
            excerpts=["证据" * 10_000] * 8,
            detail={"metrics": {f"指标{n}": float(n) for n in range(24)}},
        )
        for index in range(2)
    ]
    with pytest.raises(ValidationError, match="完整编辑输入超过"):
        _source(evidence=evidence)
    with pytest.raises(ValidationError):
        EvidenceDetail(metrics={"score": float("nan")})
    with pytest.raises(ValidationError):
        EvidenceDetail(metrics={"score": True})


@pytest.mark.parametrize(
    "bounds",
    [
        {"start_s": 59.0, "end_s": 140.0, "evidence_ids": ["asr:1"]},
        {"start_s": 90.0, "end_s": 161.0, "evidence_ids": ["asr:1"]},
        {"start_s": 140.0, "end_s": 150.0, "evidence_ids": ["asr:1"]},
    ],
)
def test_suggested_bounds_must_stay_inside_context_and_cover_event(bounds: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_review_evidence(_review(suggested_bounds=bounds), _source())


@pytest.mark.parametrize("ids", [[], ["nonexistent"], ["asr:1", "asr:1"]])
def test_boundary_references_must_be_unique_subset_of_review_references(ids: list[str]) -> None:
    with pytest.raises(ValidationError):
        _review(suggested_bounds={"start_s": 90.0, "end_s": 130.0, "evidence_ids": ids})


def test_boundary_evidence_must_intersect_suggestion() -> None:
    source = _source(evidence=[_evidence(start_s=60.0, end_s=70.0, context_role="before")])
    with pytest.raises(ValueError, match="没有交集"):
        validate_review_evidence(_review(), source)


def test_parse_rejects_duplicate_fields_fences_arrays_and_size_overflow() -> None:
    valid = json.dumps(_payload(), ensure_ascii=False)
    for text in (
        f'{{"verdict":"reject",{valid[1:]}',
        f"```json\n{valid}\n```",
        "[]",
        "null",
        "x" * (OUTPUT_MAX_BYTES + 1),
    ):
        with pytest.raises(ValueError):
            parse_editorial_review(text, _source())


def test_revalidation_catches_mutated_lists_before_business_use() -> None:
    result = _review()
    result.evidence_ids.append("forged")
    with pytest.raises(ValueError, match="不存在或不可用"):
        validate_review_evidence(result, _source())


def test_schema_has_closed_objects_and_bounded_business_fields() -> None:
    schema = EditorialReviewResultV1.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["hook"]["maxLength"] == 120
    assert schema["$defs"]["SuggestedBounds"]["additionalProperties"] is False
    assert set(schema["required"]) == set(_payload())


def test_success_execution_roundtrip_preserves_actual_attempt_and_unknown_usage() -> None:
    original = _execution()
    result = EditorialExecutionV1.model_validate_json(original.model_dump_json())
    assert result == original
    assert result.attempts[0].usage.input_tokens is None
    assert result.attempts[0].usage.estimated_cost_usd is None
    assert result.result is not None and result.result.interest_rating == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "failed", "error": "timeout"},
        {"result": None},
        {"error": "provider_error"},
        {"attempts": []},
        {"attempts": [_attempt(number=2)]},
        {"attempts": [_attempt(status="failed", error="timeout")]},
        {"input_fingerprint": "invalid"},
        {"api_key": "secret"},
        {"elapsed_s": float("nan")},
    ],
)
def test_execution_rejects_fabricated_success_and_unapproved_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _execution(**changes)


@pytest.mark.parametrize(
    "status,error", [("disabled", "disabled"), ("cancelled", "cancelled"), ("failed", "daily_budget")]
)
def test_failure_and_disabled_execution_have_no_editorial_verdict(status: str, error: str) -> None:
    result = _execution(status=status, error=error, result=None, attempts=[])
    assert result.result is None
    assert result.error == error


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "failed"},
        {"error": "timeout"},
        {"usage": {"estimated_cost_usd": 0.0}},
        {"elapsed_s": True},
        {"endpoint": "https://invalid.example"},
    ],
)
def test_attempt_rejects_inconsistent_status_and_unknown_cost(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _attempt(**changes)


def test_usage_counts_are_strict_and_cost_is_explicit_estimate() -> None:
    usage = TokenUsage(input_tokens=10, output_tokens=20, reasoning_tokens=5, estimated_cost_usd=0.0003)
    assert usage.output_tokens == 20
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=True)
    with pytest.raises(ValidationError):
        TokenUsage(output_tokens=-1)
    for name in ("input_tokens", "output_tokens", "reasoning_tokens"):
        with pytest.raises(ValidationError):
            TokenUsage(**{name: 2**63})


def test_old_attempts_remain_readable_and_failure_diagnostic_roundtrips() -> None:
    assert _attempt().diagnostic is None
    saved = _attempt(
        status="failed",
        error="provider_error",
        diagnostic={"category": "http", "summary": "服务拒绝请求", "http_status": 400, "request_id": "req-1"},
    )
    assert ModelAttempt.model_validate_json(saved.model_dump_json()) == saved
    with pytest.raises(ValidationError, match="成功请求"):
        _attempt(diagnostic={"category": "http", "summary": "服务拒绝请求"})


@pytest.mark.parametrize(
    "changes",
    [
        {"http_status": 200},
        {"http_status": 600},
        {"summary": "x" * 241},
        {"category": "unknown"},
        {"request_id": "x" * 129},
        {"provider_code": "private\ntext"},
        {"raw_body": "private"},
    ],
)
def test_failure_diagnostic_rejects_unbounded_or_unapproved_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _attempt(
            status="failed",
            error="provider_error",
            diagnostic={"category": "http", "summary": "服务拒绝请求", **changes},
        )


def test_empty_evidence_cannot_claim_to_be_available() -> None:
    with pytest.raises(ValidationError, match="实际正文或测量指标"):
        _evidence(excerpts=[])
    with pytest.raises(ValidationError):
        _evidence(excerpts=[" \n "])
    item = _evidence(type="audio", excerpts=[], detail={"metrics": {"rms": 0.23}})
    assert item.detail.metrics == {"rms": 0.23}


@pytest.mark.parametrize("error", ["cancelled", "disabled"])
def test_cancel_and_disabled_cannot_be_reported_as_other_failure(error: str) -> None:
    with pytest.raises(ValidationError, match="对应各自执行状态"):
        _execution(status="failed", error=error, result=None, attempts=[])


def test_elapsed_time_cannot_hide_time_spent_in_retries() -> None:
    with pytest.raises(ValidationError, match="总耗时"):
        _execution(elapsed_s=0.1)


def test_large_multibyte_evidence_retains_full_text_and_still_rejects_new_boundary() -> None:
    text = "原始讲解内容。" * 2000
    source = _source(evidence=[_evidence(evidence_id=f"asr:{index}", excerpts=[text]) for index in range(6)])
    assert len(source.model_dump_json().encode()) > 65536
    assert source.evidence[0].excerpts == [text]
    with pytest.raises(ValidationError):
        _evidence(excerpts=["中" * (EXCERPT_MAX_CHARS + 1)])
    with pytest.raises(ValidationError, match="完整编辑输入超过字节上限"):
        _source(evidence=[_evidence(evidence_id=f"asr:{index}", excerpts=[text]) for index in range(30)])
