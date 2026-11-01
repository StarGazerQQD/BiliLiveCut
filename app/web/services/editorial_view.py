# ruff: noqa: D100
from __future__ import annotations

from collections.abc import Mapping
from typing import get_args

from app.analysis.editorial_contracts import (
    EditorialExecutionV1,
    EditorialReviewInputV1,
    FailureCode,
    validate_review_evidence,
)
from app.analysis.editorial_provenance import ReviewProvenance, frozen_review_fingerprint
from app.db.entities import HighlightCandidate, HighlightEvent, HotspotEvent
from app.web.editorial_contracts import EditorialPresentation
from app.web.services.review_workflow import decode_features


def editorial_presentation(
    *,
    hotspot: HotspotEvent | None = None,
    candidate: HighlightCandidate | None = None,
    event: HighlightEvent | None = None,
    metadata: object = None,
) -> EditorialPresentation:
    """从同一记录读取解释和判断，旧分数不冒充新的模型兴趣等级。"""
    features = decode_features(
        hotspot.features_json if hotspot is not None else candidate.features_json if candidate else None
    )
    enrichment = features.get("event_enrichment")
    enrichment = enrichment if isinstance(enrichment, Mapping) else {}
    error = enrichment.get("error")
    if error is not None and error not in get_args(FailureCode):
        error = "invalid_output"
    title = hotspot.title if hotspot is not None else None
    summary = hotspot.summary if hotspot is not None else None
    state = "not_run"
    if title or summary:
        state = "available" if enrichment.get("source") == "llm" and error is None else "unverified"
    if enrichment.get("source") == "fallback":
        state = "failed"
    view = EditorialPresentation(
        hotspot_event_id=hotspot.id if hotspot is not None else None,
        candidate_id=candidate.id if candidate is not None else None,
        event_id=event.id if event is not None else None,
        title=title or ("热点解释待补全" if hotspot is not None else "片段解释待补全"),
        summary=summary or "尚无经过核验的内容摘要，请结合原始证据人工判断。",
        explanation_state=state,
        explanation_error=error,
        technical_score=candidate.highlight_score
        if candidate is not None
        else hotspot.clip_score
        if hotspot is not None
        else None,
    )
    metadata = metadata if metadata is not None else features.get("editorial_review")
    if metadata is None:
        return view
    try:
        if not isinstance(metadata, Mapping):
            raise ValueError("编辑记录必须为对象")
        execution = EditorialExecutionV1.model_validate(metadata.get("execution"))
        raw_source = metadata.get("input")
        source = EditorialReviewInputV1.model_validate(raw_source) if raw_source is not None else None
        if source is not None:
            if hotspot is not None and (
                source.target.kind != "hotspot"
                or source.target.target_id != hotspot.id
                or source.target.session_id != hotspot.session_id
            ):
                raise ValueError("编辑证据与热点身份不一致")
            if candidate is not None and source.target.session_id != candidate.session_id:
                raise ValueError("编辑证据与候选场次不一致")
            if (
                hotspot is None
                and candidate is not None
                and event is not None
                and (source.target.kind != "segment" or source.target.target_id != event.segment_id)
            ):
                raise ValueError("编辑证据与分段身份不一致")
        if execution.result is not None:
            if source is None:
                raise ValueError("编辑判断缺少原始证据")
            validate_review_evidence(execution.result, source)
        direction = metadata.get("expanded_context")
        if direction is not None and (not isinstance(direction, str) or direction not in {"before", "after", "both"}):
            raise ValueError("上下文方向无效")
        previous_attempts = []
        if metadata.get("provenance") is not None:
            provenance = ReviewProvenance.model_validate(metadata["provenance"])
            if [attempt.number for attempt in provenance.prior_attempts] != list(
                range(1, execution.attempt_offset + 1)
            ):
                raise ValueError("共享预算前序请求记录不完整")
            revision = metadata.get("revision")
            if not isinstance(revision, str):
                raise ValueError("编辑记录缺少数据修订")
            expected = frozen_review_fingerprint(provenance.initial_input, source, revision, policy=provenance.policy)
            if expected != execution.input_fingerprint:
                raise ValueError("执行输入与冻结策略指纹不一致")
            previous_attempts = provenance.prior_attempts
    except ValueError:
        # 旧记录可读，但损坏的机器判断不能继续作为可信结论展示。
        return view.model_copy(update={"editorial_state": "invalid"})
    return view.model_copy(
        update={
            "editorial_state": execution.status,
            "execution": execution,
            "previous_attempts": previous_attempts,
            "evidence": source.evidence if source is not None else [],
            "time_origin_epoch_s": source.time_origin_epoch_s if source is not None else None,
            "expanded_context": direction,
        }
    )


def legacy_model_score(candidate: HighlightCandidate | None) -> float | None:
    """仅保留确有旧模型评分的历史值；技术链占位零分保持未知。"""
    if candidate is None:
        return None
    features = decode_features(candidate.features_json)
    if "editorial_review" in features or "clip_scorer_version" in features or not candidate.llm_score:
        return None
    return candidate.llm_score
