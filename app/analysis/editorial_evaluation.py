# ruff: noqa: D100
from __future__ import annotations

from collections import deque
from importlib.resources import files
from statistics import mean
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import __version__
from app.analysis.editorial_contracts import (
    EditorialExecutionV1,
    EditorialReviewInputV1,
    Identity,
    ModelAttempt,
    TimeRange,
    validate_review_evidence,
)
from app.analysis.editorial_provenance import ReviewProvenance, canonical_hash, frozen_review_fingerprint

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False, hide_input_in_errors=True)


class EvaluationRecord(_Contract):
    """一个已有业务对象的冻结结果，机器判断与人工标注分别保存。"""

    key: Identity
    session_id: int = Field(gt=0)
    bounds: TimeRange
    input: EditorialReviewInputV1 | None
    execution: EditorialExecutionV1 | None
    provenance: ReviewProvenance | None
    revision: str | None = Field(max_length=256)
    title: str | None = Field(max_length=1000)
    summary: str | None = Field(max_length=4000)
    explanation_verified: bool

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """核对证据、执行输入与冻结策略，不信任外部文件中的成功标志。"""
        if self.input is not None and self.input.target.session_id != self.session_id:
            raise ValueError("评估证据不属于该场次")
        if self.execution is not None and self.execution.result is not None:
            if self.input is None:
                raise ValueError("编辑结果缺少输入")
            validate_review_evidence(self.execution.result, self.input)
        if self.provenance is not None:
            if self.execution is None or self.revision is None:
                raise ValueError("执行来源缺少执行记录或数据修订")
            expected = frozen_review_fingerprint(
                self.provenance.initial_input, self.input, self.revision, policy=self.provenance.policy
            )
            if expected != self.execution.input_fingerprint:
                raise ValueError("执行输入与策略指纹不一致")
            if [a.number for a in self.provenance.prior_attempts] != list(range(1, self.execution.attempt_offset + 1)):
                raise ValueError("共享预算前序请求记录不完整")
        return self


class EvaluationSession(_Contract):
    """实际录制范围和分析完成情况；断流缺口不算作已覆盖素材。"""

    session_id: int = Field(gt=0)
    recording_ranges: list[TimeRange] = Field(min_length=1, max_length=1000)
    analysis_complete: bool

    @model_validator(mode="after")
    def ordered(self) -> Self:
        """录制范围必须排序并合并相邻块。"""
        if any(a.end_s >= b.start_s for a, b in zip(self.recording_ranges, self.recording_ranges[1:], strict=False)):
            raise ValueError("录制范围必须按顺序合并")
        return self


class EvaluationSnapshot(_Contract):
    """同一只读快照下的全部对象，数据集身份用于跨运行复用场次分组。"""

    schema_version: Literal["editorial-evaluation-v1"] = "editorial-evaluation-v1"
    dataset_id: Identity
    exporter_version: Identity
    sessions: list[EvaluationSession] = Field(min_length=1, max_length=1000)
    records: list[EvaluationRecord] = Field(max_length=10000)

    @model_validator(mode="after")
    def unique(self) -> Self:
        """拒绝重复对象、未知场次或不属于录制范围的成片边界。"""
        ids = {s.session_id for s in self.sessions}
        if len(ids) != len(self.sessions) or len({r.key for r in self.records}) != len(self.records):
            raise ValueError("评估场次和对象不可重复")
        by_id = {s.session_id: s for s in self.sessions}
        for record in self.records:
            if record.session_id not in ids:
                raise ValueError("评估对象引用了未知场次")
            if not any(block.contains(record.bounds) for block in by_id[record.session_id].recording_ranges):
                raise ValueError("评估成片范围超出实际连续录制")
        return self


class CandidateLabel(_Contract):
    """针对冻结对象的独立人工标注，未知判断必须保留空值。"""

    key: Identity
    worth_clipping: bool | None
    reference_bounds: TimeRange | None
    title_supported: bool | None
    summary_supported: bool | None
    decision_supported: bool | None


class SessionLabels(_Contract):
    """整个场次只属于一个集合，整场模式另需完整观看范围与全部正例。"""

    session_id: int = Field(gt=0)
    split: Literal["tuning", "holdout"]
    watched_ranges: list[TimeRange] = Field(max_length=1000)
    complete: bool
    positives: list[TimeRange] = Field(max_length=1000)
    candidates: list[CandidateLabel] = Field(max_length=10000)

    @model_validator(mode="after")
    def unique(self) -> Self:
        """同一对象和同一正例不重复计数。"""
        if len({c.key for c in self.candidates}) != len(self.candidates):
            raise ValueError("人工标注对象重复")
        if len({(p.start_s, p.end_s) for p in self.positives}) != len(self.positives):
            raise ValueError("人工正例范围重复")
        if not self.complete and self.positives:
            raise ValueError("未完成整场标注时不能提交完整正例集合")
        return self


class EvaluationLabels(_Contract):
    """明确绑定快照与人工标注来源，合成用例不能冒充真实质量。"""

    schema_version: Literal["editorial-labels-v1"] = "editorial-labels-v1"
    snapshot_sha256: Digest
    annotation_version: Identity
    annotator: Identity
    source: Literal["human", "synthetic"]
    sessions: list[SessionLabels] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def disjoint(self) -> Self:
        """禁止同一场次同时进入调试和独立验收集。"""
        if len({s.session_id for s in self.sessions}) != len(self.sessions):
            raise ValueError("场次分组存在交叉或重复")
        return self


def _absent_request_metadata(attempts: list[ModelAttempt]) -> dict[int, set[str]]:
    return {
        index: absent
        for index, attempt in enumerate(attempts)
        if (absent := {"request_id", "output_tokens_limit"} - attempt.model_fields_set)
    }


def snapshot_payload(snapshot: EvaluationSnapshot) -> dict[str, object]:
    """导出完整冻结资料，保留旧请求未记录的新增字段及显式未知值之间的区别。"""
    records: dict[int, dict[str, object]] = {}
    for index, record in enumerate(snapshot.records):
        omitted: dict[str, object] = {}
        if record.execution is not None:
            omitted["execution"] = {"attempts": _absent_request_metadata(record.execution.attempts)}
        if record.provenance is not None:
            omitted["provenance"] = {"prior_attempts": _absent_request_metadata(record.provenance.prior_attempts)}
        records[index] = omitted
    return snapshot.model_dump(mode="json", exclude={"records": records})


def snapshot_digest(snapshot: EvaluationSnapshot) -> str:
    """返回绑定人工标注与报告的完整快照指纹。"""
    return canonical_hash(snapshot_payload(snapshot))


def label_template(
    snapshot: EvaluationSnapshot,
    *,
    annotator: str,
    annotation_version: str,
    holdout_sessions: list[int],
) -> EvaluationLabels:
    """生成全未知标注模板，先冻结场次分组，再由人工独立标注。"""
    ids = {s.session_id for s in snapshot.sessions}
    if not holdout_sessions or len(set(holdout_sessions)) != len(holdout_sessions) or set(holdout_sessions) - ids:
        raise ValueError("独立评估场次必须存在且不能重复")
    return EvaluationLabels(
        snapshot_sha256=snapshot_digest(snapshot),
        annotator=annotator,
        annotation_version=annotation_version,
        source="human",
        sessions=[
            SessionLabels(
                session_id=s.session_id,
                split="holdout" if s.session_id in holdout_sessions else "tuning",
                watched_ranges=[],
                complete=False,
                positives=[],
                candidates=[
                    CandidateLabel(
                        key=r.key,
                        worth_clipping=None,
                        reference_bounds=None,
                        title_supported=None,
                        summary_supported=None,
                        decision_supported=None,
                    )
                    for r in snapshot.records
                    if r.session_id == s.session_id
                ],
            )
            for s in snapshot.sessions
        ],
    )


def _iou(a: TimeRange, b: TimeRange) -> float:
    overlap = max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))
    return overlap / (max(a.end_s, b.end_s) - min(a.start_s, b.start_s))


def match_intervals(predictions: list[TimeRange], truth: list[TimeRange], threshold: float) -> list[tuple[int, int]]:
    """用增广路径做确定性一对一最大匹配，重复推荐不会重复获得正例。"""
    edges = [[j for j, ref in enumerate(truth) if _iou(pred, ref) >= threshold] for pred in predictions]
    matched: dict[int, int] = {}
    for start in range(len(predictions)):
        parents: dict[int, tuple[int, int] | None] = {start: None}
        queue = deque([start])
        terminal: tuple[int, int] | None = None
        while queue and terminal is None:
            left = queue.popleft()
            for right in edges[left]:
                previous = matched.get(right)
                if previous is None:
                    terminal = (left, right)
                    break
                if previous not in parents:
                    parents[previous] = (left, right)
                    queue.append(previous)
        while terminal is not None:
            left, right = terminal
            matched[right] = left
            terminal = parents[left]
    return sorted((left, right) for right, left in matched.items())


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _recommended(record: EvaluationRecord) -> bool:
    return bool(
        record.execution is not None
        and record.execution.result is not None
        and record.execution.result.verdict == "recommend"
    )


def _cost(records: list[EvaluationRecord]) -> dict[str, object]:
    unique: dict[tuple[str, int], ModelAttempt] = {}
    for record in records:
        if record.execution is None:
            continue
        provenance = record.provenance
        operation = provenance.operation_id if provenance is not None else record.key
        for attempt in (provenance.prior_attempts if provenance is not None else []) + record.execution.attempts:
            key = (operation, attempt.number)
            if key in unique and unique[key] != attempt:
                raise ValueError("共享请求的成本记录相互矛盾")
            unique[key] = attempt
    attempts = list(unique.values())
    known = [a.usage.estimated_cost_usd for a in attempts if a.usage.estimated_cost_usd is not None]
    return {
        "scope": "stored_shared_budget_attempts",
        "legacy_records_without_prior_cost": sum(r.provenance is None for r in records),
        "attempts": len(attempts),
        "unknown_cost_attempts": len(attempts) - len(known),
        "known_estimated_cost_usd": sum(known) if known else None,
        "complete_estimated_cost_usd": sum(known) if attempts and len(known) == len(attempts) else None,
        "reported_input_tokens": sum(a.usage.input_tokens for a in attempts if a.usage.input_tokens is not None)
        if any(a.usage.input_tokens is not None for a in attempts)
        else None,
        "unknown_input_token_attempts": sum(a.usage.input_tokens is None for a in attempts),
        "reported_output_tokens": sum(a.usage.output_tokens for a in attempts if a.usage.output_tokens is not None)
        if any(a.usage.output_tokens is not None for a in attempts)
        else None,
        "unknown_output_token_attempts": sum(a.usage.output_tokens is None for a in attempts),
        "stored_request_elapsed_s": sum(a.elapsed_s for a in attempts),
    }


def evaluate(
    snapshot: EvaluationSnapshot,
    labels: EvaluationLabels,
    *,
    mode: Literal["fixed_candidates", "full_session"],
    iou_threshold: float = 0.5,
) -> dict[str, object]:
    """计算可复现离线指标；缺少完整覆盖时拒绝报告整场召回率。"""
    if (
        mode not in {"fixed_candidates", "full_session"}
        or isinstance(iou_threshold, bool)
        or not 0 < iou_threshold <= 1
    ):
        raise ValueError("评估模式或 IoU 阈值无效")
    if labels.snapshot_sha256 != snapshot_digest(snapshot):
        raise ValueError("标注与当前快照指纹不一致")
    sessions = {s.session_id: s for s in snapshot.sessions}
    if {s.session_id for s in labels.sessions} != set(sessions):
        raise ValueError("每个导出场次都必须明确标注分组")
    by_key = {r.key: r for r in snapshot.records}
    results: dict[str, object] = {}
    for split in ("tuning", "holdout"):
        group = [s for s in labels.sessions if s.split == split]
        tp = fp = fn = labeled = unknown = 0
        start_errors: list[float] = []
        end_errors: list[float] = []
        facts: dict[str, list[bool]] = {"title": [], "summary": [], "decision": []}
        matches: list[dict[str, object]] = []
        for annotated in group:
            session = sessions[annotated.session_id]
            records = [r for r in snapshot.records if r.session_id == session.session_id]
            for label in annotated.candidates:
                record = by_key.get(label.key)
                if record is None or record.session_id != session.session_id:
                    raise ValueError("标注引用了未知或其他场次的对象")
                if label.reference_bounds is not None and not any(
                    block.contains(label.reference_bounds) for block in session.recording_ranges
                ):
                    raise ValueError("标注边界超出真实录制范围")
                for name in facts:
                    supported = getattr(label, f"{name}_supported")
                    available = (
                        record.execution is not None and record.execution.result is not None
                        if name == "decision"
                        else bool(getattr(record, name))
                    )
                    if supported is not None:
                        if not available:
                            raise ValueError("没有实际内容的字段不能标注事实性")
                        facts[name].append(supported)
                if mode == "fixed_candidates":
                    if label.worth_clipping is None:
                        continue
                    labeled += 1
                    recommended = _recommended(record)
                    tp += recommended and label.worth_clipping
                    fp += recommended and not label.worth_clipping
                    fn += not recommended and label.worth_clipping
                    if recommended and label.worth_clipping and label.reference_bounds is not None:
                        start_errors.append(abs(record.bounds.start_s - label.reference_bounds.start_s))
                        end_errors.append(abs(record.bounds.end_s - label.reference_bounds.end_s))
            if mode == "fixed_candidates":
                unknown += len(records) - sum(c.worth_clipping is not None for c in annotated.candidates)
                continue
            if (
                not session.analysis_complete
                or not annotated.complete
                or annotated.watched_ranges != session.recording_ranges
            ):
                raise ValueError("整场评估要求分析完成且人工覆盖全部录制范围")
            if any(not any(block.contains(p) for block in session.recording_ranges) for p in annotated.positives):
                raise ValueError("整场正例超出真实录制范围")
            selected = [r for r in records if _recommended(r)]
            pairs = match_intervals([r.bounds for r in selected], annotated.positives, iou_threshold)
            tp += len(pairs)
            fp += len(selected) - len(pairs)
            fn += len(annotated.positives) - len(pairs)
            labeled += len(annotated.positives)
            for left, right in pairs:
                actual, expected = selected[left], annotated.positives[right]
                start_errors.append(abs(actual.bounds.start_s - expected.start_s))
                end_errors.append(abs(actual.bounds.end_s - expected.end_s))
                matches.append({"key": actual.key, "session_id": session.session_id, "positive_index": right})
        selected_records = [r for r in snapshot.records if r.session_id in {s.session_id for s in group}]
        provenance = [r.provenance for r in selected_records if r.provenance is not None]
        results[split] = {
            "session_ids": [s.session_id for s in group],
            "records": len(selected_records),
            "labeled": labeled,
            "unlabeled_candidates": unknown,
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "precision": _ratio(tp, tp + fp),
            "candidate_recall": _ratio(tp, tp + fn) if mode == "fixed_candidates" else None,
            "full_session_recall": _ratio(tp, tp + fn) if mode == "full_session" else None,
            "boundary_samples": len(start_errors),
            "start_mae_s": mean(start_errors) if start_errors else None,
            "end_mae_s": mean(end_errors) if end_errors else None,
            "factuality": {
                k: {"annotated": len(v), "supported": sum(v), "rate": _ratio(sum(v), len(v))} for k, v in facts.items()
            },
            "cost": _cost(selected_records),
            "matches": matches,
            "unknown_provenance": sum(r.provenance is None for r in selected_records),
            "app_versions": sorted({p.app_version for p in provenance}),
            "code_identities": sorted({p.code_sha256 for p in provenance}),
            "policy_identities": sorted({canonical_hash(p.policy) for p in provenance}),
            "unavailable_decisions": sum(r.execution is None or r.execution.result is None for r in selected_records),
        }
    has_annotations = any(
        s.complete if mode == "full_session" else any(c.worth_clipping is not None for c in s.candidates)
        for s in labels.sessions
    )
    return {
        "schema_version": "editorial-evaluation-report-v1",
        "dataset_id": snapshot.dataset_id,
        "evaluator_version": __version__,
        "evaluator_sha256": canonical_hash(
            {
                name: files("app").joinpath(name).read_text(encoding="utf-8")
                for name in ("analysis/editorial_evaluation.py", "analysis/editorial_evaluation_io.py")
            }
        ),
        "snapshot_sha256": snapshot_digest(snapshot),
        "labels_sha256": canonical_hash(labels.model_dump(mode="json")),
        "mode": mode,
        "iou_threshold": iou_threshold,
        "annotation_version": labels.annotation_version,
        "quality_status": ("human_annotated" if has_annotations else "awaiting_annotation")
        if labels.source == "human"
        else "synthetic_engineering_only",
        "groups": results,
    }
