from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.analysis.editorial_contracts import TimeRange, TokenUsage
from app.analysis.editorial_evaluation import (
    CandidateLabel,
    EvaluationLabels,
    EvaluationRecord,
    EvaluationSession,
    EvaluationSnapshot,
    SessionLabels,
    evaluate,
    label_template,
    match_intervals,
    snapshot_digest,
    snapshot_payload,
)
from app.analysis.editorial_evaluation_io import read_contract, write_result
from app.analysis.editorial_provenance import ReviewProvenance, canonical_hash, capture_provenance
from tests.unit.test_editorial_contracts import _attempt, _execution, _review, _source

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _range(start: float = 90.0, end: float = 130.0) -> TimeRange:
    return TimeRange(start_s=start, end_s=end)


def _record(key: str = "hotspot:12", verdict: str = "recommend") -> EvaluationRecord:
    return EvaluationRecord(
        key=key,
        session_id=7,
        bounds=_range(),
        input=_source(),
        execution=_execution(result=_review(verdict=verdict)),
        provenance=None,
        revision=None,
        title="挑战",
        summary="主播解释挑战规则。",
        explanation_verified=True,
    )


def _snapshot(records: list[EvaluationRecord] | None = None) -> EvaluationSnapshot:
    return EvaluationSnapshot(
        dataset_id="engineering-fixture",
        exporter_version="test",
        sessions=[EvaluationSession(session_id=7, recording_ranges=[_range(0.0, 300.0)], analysis_complete=True)],
        records=records if records is not None else [_record()],
    )


def _label(key: str = "hotspot:12", **changes: object) -> CandidateLabel:
    return CandidateLabel.model_validate(
        {
            "key": key,
            "worth_clipping": True,
            "reference_bounds": _range(),
            "title_supported": None,
            "summary_supported": None,
            "decision_supported": None,
            **changes,
        }
    )


def _labels(
    snapshot: EvaluationSnapshot, *, complete: bool = True, candidates: list[CandidateLabel] | None = None
) -> EvaluationLabels:
    return EvaluationLabels(
        snapshot_sha256=snapshot_digest(snapshot),
        annotation_version="test-v1",
        annotator="test",
        source="synthetic",
        sessions=[
            SessionLabels(
                session_id=7,
                split="holdout",
                watched_ranges=[_range(0.0, 300.0)],
                complete=complete,
                positives=[_range()] if complete else [],
                candidates=candidates if candidates is not None else [_label()],
            )
        ],
    )


def test_fixed_candidates_never_report_full_session_recall() -> None:
    snapshot = _snapshot([_record(), _record("hotspot:13", "reject"), _record("hotspot:14")])
    report = evaluate(snapshot, _labels(snapshot, candidates=[_label(), _label("hotspot:13")]), mode="fixed_candidates")
    group = report["groups"]["holdout"]
    assert (group["true_positive"], group["false_negative"], group["unlabeled_candidates"]) == (1, 1, 1)
    assert group["candidate_recall"] == 0.5
    assert group["full_session_recall"] is None
    assert group["cost"]["known_estimated_cost_usd"] is None
    assert group["factuality"]["title"]["rate"] is None
    assert group["start_mae_s"] == 0.0
    assert report["quality_status"] == "synthetic_engineering_only"


def test_full_session_duplicate_recommendations_count_as_false_positives() -> None:
    snapshot = _snapshot([_record(), _record("hotspot:13")])
    report = evaluate(snapshot, _labels(snapshot), mode="full_session")
    group = report["groups"]["holdout"]
    assert (group["true_positive"], group["false_positive"], group["false_negative"]) == (1, 1, 0)
    assert group["precision"] == 0.5
    assert group["full_session_recall"] == 1.0
    assert group["candidate_recall"] is None
    assert len(group["matches"]) == 1


def test_matching_uses_augmenting_paths_instead_of_greedy_pairs() -> None:
    assert match_intervals([_range(0.0, 20.0), _range(0.0, 10.0)], [_range(0.0, 10.0), _range(10.0, 20.0)], 0.5) == [
        (0, 1),
        (1, 0),
    ]


def test_matching_preserves_overlap_with_large_finite_timestamps() -> None:
    bounds = _range(0.0, 1e308)
    assert match_intervals([bounds], [bounds], 1.0) == [(0, 0)]


@pytest.mark.parametrize("reason", ["incomplete", "unwatched", "unanalyzed"])
def test_full_session_rejects_incomplete_coverage(reason: str) -> None:
    snapshot = _snapshot()
    labels = _labels(snapshot, complete=reason != "incomplete")
    if reason == "unwatched":
        labels = labels.model_copy(update={"sessions": [labels.sessions[0].model_copy(update={"watched_ranges": []})]})
    if reason == "unanalyzed":
        snapshot = snapshot.model_copy(
            update={"sessions": [snapshot.sessions[0].model_copy(update={"analysis_complete": False})]}
        )
        labels = labels.model_copy(update={"snapshot_sha256": snapshot_digest(snapshot)})
    with pytest.raises(ValueError, match="整场评估"):
        evaluate(snapshot, labels, mode="full_session")


@pytest.mark.parametrize("threshold", [0.0, -1.0, 1.1, float("nan"), float("inf")])
def test_invalid_matching_threshold_is_rejected(threshold: float) -> None:
    snapshot = _snapshot()
    with pytest.raises(ValueError, match="IoU"):
        evaluate(snapshot, _labels(snapshot), mode="full_session", iou_threshold=threshold)


def test_annotations_cannot_cross_sessions_or_stale_snapshots() -> None:
    snapshot = _snapshot()
    labels = _labels(snapshot)
    with pytest.raises(ValueError, match="交叉"):
        EvaluationLabels.model_validate(
            {
                **labels.model_dump(),
                "sessions": [labels.sessions[0], labels.sessions[0].model_copy(update={"split": "tuning"})],
            }
        )
    with pytest.raises(ValueError, match="指纹"):
        evaluate(snapshot, labels.model_copy(update={"snapshot_sha256": "0" * 64}), mode="fixed_candidates")
    with pytest.raises(ValueError, match="未知"):
        evaluate(snapshot, _labels(snapshot, candidates=[_label("hotspot:404")]), mode="fixed_candidates")


def test_empty_predictions_report_misses_and_unknown_precision() -> None:
    snapshot = _snapshot([])
    result = evaluate(snapshot, _labels(snapshot, candidates=[]), mode="full_session")["groups"]["holdout"]
    assert result["precision"] is None
    assert result["full_session_recall"] == 0.0
    assert result["false_negative"] == 1
    assert result["start_mae_s"] is None


def test_factuality_requires_actual_content_and_known_annotations() -> None:
    snapshot = _snapshot()
    labels = _labels(snapshot, candidates=[_label(title_supported=False, summary_supported=True)])
    facts = evaluate(snapshot, labels, mode="fixed_candidates")["groups"]["holdout"]["factuality"]
    assert facts["title"] == {"annotated": 1, "supported": 0, "rate": 0.0}
    assert facts["summary"]["rate"] == 1.0
    assert facts["decision"]["rate"] is None
    empty = _snapshot([_record().model_copy(update={"title": None})])
    with pytest.raises(ValueError, match="没有实际内容"):
        evaluate(empty, _labels(empty, candidates=[_label(title_supported=True)]), mode="fixed_candidates")


def test_shared_budget_costs_are_deduplicated_and_unknown_costs_stay_unknown() -> None:
    prior = _attempt(purpose="explanation", usage=TokenUsage(input_tokens=10, output_tokens=5, estimated_cost_usd=0.02))
    current = _attempt(number=2)
    provenance = capture_provenance(
        {
            "policy": "editorial-decision-v2",
            "prompt": "0" * 64,
            "routes": [],
            "attempts": 8,
            "deadline": 30.0,
            "request_timeout": 10.0,
            "max_tokens": 512,
        },
        _source(),
        operation_id="a" * 32,
        prior_attempts=[prior],
    )
    fingerprint = canonical_hash(
        {
            "initial": _source().model_dump(mode="json"),
            "source": _source().model_dump(mode="json"),
            "policy": provenance.policy,
            "revision": "test",
        }
    )
    record = EvaluationRecord.model_validate(
        {
            **_record().model_dump(),
            "provenance": provenance,
            "revision": "test",
            "execution": _execution(input_fingerprint=fingerprint, attempt_offset=1, attempts=[current]),
        }
    )
    snapshot = _snapshot([record, record.model_copy(update={"key": "hotspot:13"})])
    cost = evaluate(snapshot, _labels(snapshot), mode="fixed_candidates")["groups"]["holdout"]["cost"]
    assert cost["attempts"] == 2
    assert cost["known_estimated_cost_usd"] == 0.02
    assert cost["complete_estimated_cost_usd"] is None
    assert cost["unknown_cost_attempts"] == 1
    with pytest.raises(ValueError, match="指纹"):
        EvaluationRecord.model_validate({**record.model_dump(), "revision": "changed"})


def test_files_are_bounded_and_never_overwritten(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    path = tmp_path / "snapshot.json"
    snapshot = _snapshot()
    write_result(path, snapshot.model_dump(mode="json"))
    assert read_contract(path, EvaluationSnapshot) == snapshot
    with pytest.raises(FileExistsError):
        write_result(path, {})
    monkeypatch.setattr("app.analysis.editorial_evaluation_io.MAX_FILE_BYTES", 8)
    with pytest.raises(ValueError, match="超过"):
        read_contract(path, EvaluationSnapshot)
    with pytest.raises(ValueError, match="超过"):
        write_result(tmp_path / "large.json", {"large": "x" * 10})


@pytest.mark.parametrize("metadata", ["absent", "unknown", "known"])
def test_snapshot_request_metadata_preserves_existing_label_identity(tmp_path: Path, metadata: str) -> None:
    provenance = capture_provenance(
        {
            "policy": "editorial-decision-v2",
            "prompt": "0" * 64,
            "routes": [],
            "attempts": 8,
            "deadline": 30.0,
            "request_timeout": 10.0,
            "max_tokens": 512,
        },
        _source(),
        operation_id="b" * 32,
        prior_attempts=[_attempt(purpose="explanation")],
    )
    raw = _snapshot().model_dump(mode="json")
    record = raw["records"][0]
    record["provenance"] = provenance.model_dump(mode="json")
    record["revision"] = "frozen"
    execution = record["execution"]
    execution["attempt_offset"] = 1
    execution["attempts"][0]["number"] = 2
    execution["input_fingerprint"] = canonical_hash(
        {"initial": record["input"], "source": record["input"], "revision": "frozen", "policy": provenance.policy}
    )
    for attempt in execution["attempts"] + record["provenance"]["prior_attempts"]:
        if metadata == "absent":
            attempt.pop("request_id")
            attempt.pop("output_tokens_limit")
        else:
            attempt["request_id"] = "request-1" if metadata == "known" else None
            attempt["output_tokens_limit"] = 4096 if metadata == "known" else None
        assert attempt["reported_model"] is None and attempt["diagnostic"] is None
        assert attempt["usage"]["estimated_cost_usd"] is None
    path = tmp_path / "frozen.json"
    write_result(path, raw)
    snapshot = read_contract(path, EvaluationSnapshot)
    expected = canonical_hash(raw)
    assert snapshot_digest(snapshot) == expected
    assert snapshot_payload(snapshot) == raw
    roundtrip_path = tmp_path / "exported.json"
    write_result(roundtrip_path, snapshot_payload(snapshot))
    assert snapshot_digest(read_contract(roundtrip_path, EvaluationSnapshot)) == expected
    labels = _labels(snapshot).model_copy(update={"snapshot_sha256": expected})
    assert evaluate(snapshot, labels, mode="fixed_candidates")["snapshot_sha256"] == expected


@pytest.mark.parametrize("field,value", [("request_id", "request-2"), ("output_tokens_limit", 8192)])
def test_known_request_metadata_is_part_of_snapshot_identity(field: str, value: object) -> None:
    record = _record()
    original = _attempt(request_id="request-1", output_tokens_limit=4096)
    record = record.model_copy(update={"execution": _execution(attempts=[original])})
    snapshot = _snapshot([record])
    changed = record.model_copy(update={"execution": _execution(attempts=[original.model_copy(update={field: value})])})
    with pytest.raises(ValueError, match="指纹"):
        evaluate(_snapshot([changed]), _labels(snapshot), mode="fixed_candidates")


def test_duplicate_json_fields_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"records": [], "records": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        read_contract(path, EvaluationSnapshot)


def test_label_template_preserves_unknowns_and_requires_existing_holdout_sessions() -> None:
    snapshot = _snapshot()
    labels = label_template(snapshot, annotator="reviewer", annotation_version="v1", holdout_sessions=[7])
    assert labels.sessions[0].split == "holdout"
    assert labels.sessions[0].candidates[0].worth_clipping is None
    report = evaluate(snapshot, labels, mode="fixed_candidates")
    assert report["quality_status"] == "awaiting_annotation"
    assert report["groups"]["holdout"]["precision"] is None
    with pytest.raises(ValueError, match="独立评估场次"):
        label_template(snapshot, annotator="reviewer", annotation_version="v1", holdout_sessions=[99])


def test_provenance_refuses_secret_fields_in_policy() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        ReviewProvenance.model_validate(
            {
                "app_version": "test",
                "code_sha256": "0" * 64,
                "initial_input": None,
                "operation_id": "a" * 32,
                "prior_attempts": [],
                "policy": {
                    "policy": "v1",
                    "prompt": "0" * 64,
                    "routes": [],
                    "attempts": 8,
                    "deadline": 30.0,
                    "request_timeout": 10.0,
                    "max_tokens": 512,
                    "api_key": "must-not-export",
                },
            }
        )
