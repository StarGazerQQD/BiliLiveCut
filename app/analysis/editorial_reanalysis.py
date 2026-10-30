# ruff: noqa: D100
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue
from sqlmodel import Session, select

from app.analysis import llm
from app.analysis.clip_scorer import EventClipDraft, mark_event_clip_evaluated, validate_event_clip_draft
from app.analysis.editorial_review import EditorialReviewDraft
from app.analysis.editorial_review_sources import (
    SegmentReviewSnapshot,
    event_review_is_current,
    media_snapshot_is_current,
    media_validation_scope,
)
from app.analysis.event_enricher import EventEnrichmentDraft, commit_event_enrichment
from app.analysis.reanalysis_targets import (
    ReanalysisTarget,
    list_reanalysis_targets,
    target_fingerprint,
    target_protection,
)
from app.analysis.timeline import datetime_epoch
from app.core.cancellation import check_shutdown
from app.core.runtime_settings import configured_task
from app.db.entities import (
    AppSetting,
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    RawSegment,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session
from app.jobs.context import JobContext
from app.pipeline.stage_result import make_stage_key
from app.web.services.review_workflow import WORKFLOW_KEY, decode_features


@dataclass(frozen=True, slots=True)
class ReanalysisDraft:
    """只在内存中持有真实计算结果，原始证据由快照与提交核验保护。"""

    target: ReanalysisTarget
    payload: dict[str, Any] | None
    skip_reason: str | None = None


def _current(db: Session, target: ReanalysisTarget, own_task_id: int | None) -> str | None:
    reason = target_protection(db, target, own_task_id)
    if reason:
        return reason
    if not media_snapshot_is_current(db, target.session_id):
        return "state_changed"
    if target_fingerprint(db, target, own_task_id) != target.fingerprint:
        return "state_changed"
    return None


@configured_task
def compute_reanalysis(
    targets: list[ReanalysisTarget],
    *,
    cancelled: Callable[[], bool],
    own_task_id: int | None = None,
) -> list[ReanalysisDraft]:
    """依次重评冻结对象；同一次操作共享模型次数和绝对时间预算。"""
    from app.pipeline.workers.analyze import _compute_event_clip_result, _score_segment_draft

    if len(targets) > 100 or len({item.key for item in targets}) != len(targets):
        raise ValueError("重分析每次最多选择 100 个不同对象")
    budget = llm.new_editorial_budget(cancelled=cancelled)
    results: list[ReanalysisDraft] = []
    for target in targets:
        interruption = budget.interruption()
        with get_session() as db:
            reason = interruption or _current(db, target, own_task_id)
        if reason:
            results.append(ReanalysisDraft(target, None, reason))
            continue
        if target.kind == "hotspot":
            payload = _compute_event_clip_result(
                target.source_id,
                budget=budget,
                expected_candidate_id=target.candidate_id,
                force=True,
            )
        else:
            with get_session() as db:
                segment = db.get(RawSegment, target.segment_id)
                if segment is None or segment.start_ts is None:
                    results.append(ReanalysisDraft(target, None, "missing_source"))
                    continue
                offset = datetime_epoch(target.peak_ts) - datetime_epoch(segment.start_ts)
            payload = _score_segment_draft(
                target.segment_id, peak_offset_s=offset, budget=budget, exclude_candidate_id=target.candidate_id
            )
        results.append(ReanalysisDraft(target, payload, None if payload is not None else "source_changed"))
    return results


def _validate_computed(db: Session, draft: ReanalysisDraft) -> bool:
    payload = draft.payload
    if payload is None:
        return False
    target = draft.target
    if target.kind == "segment":
        snapshot = payload.get("editorial_snapshot")
        return (
            isinstance(snapshot, SegmentReviewSnapshot)
            and snapshot.segment_id == target.segment_id
            and abs(datetime_epoch(snapshot.peak_ts) - datetime_epoch(target.peak_ts)) < 1e-6
            and snapshot.is_current(db)
        )
    clip = payload.get("clip_draft")
    review = payload.get("review")
    enrichment = payload.get("enrichment")
    if not isinstance(clip, EventClipDraft) or not isinstance(review, EditorialReviewDraft):
        raise ValueError("热点重评缺少真实评分或编辑判断")
    if enrichment is not None and not isinstance(enrichment, EventEnrichmentDraft):
        raise ValueError("热点重评解释类型无效")
    if clip.hotspot_event_id != target.source_id or clip.session_id != target.session_id:
        raise ValueError("热点重评结果不属于选中的对象")
    if enrichment is not None and (
        enrichment.event_id != target.source_id or enrichment.bundle_fingerprint != clip.bundle_fingerprint
    ):
        raise ValueError("热点重评解释与评分来源不同")
    metadata = decode_features(clip.features_json).get("editorial_review")
    if not isinstance(metadata, dict) or metadata.get("execution") != review.execution.model_dump(mode="json"):
        raise ValueError("热点重评结果的编辑执行记录不一致")
    return (
        event_review_is_current(db, target.source_id, review)
        and validate_event_clip_draft(
            db,
            clip,
            expected_candidate_id=target.candidate_id,
            enrichment=enrichment,
            review=review,
        )
        is not None
    )


def _merge_machine_features(previous: str | None, current: str) -> str:
    old = decode_features(previous)
    new = decode_features(current)
    for key, value in old.items():
        if key not in new:
            new[key] = value
    if WORKFLOW_KEY in old:
        new[WORKFLOW_KEY] = old[WORKFLOW_KEY]
    return json.dumps(new, ensure_ascii=False, allow_nan=False)


def _persist_candidate(db: Session, target: ReanalysisTarget, payload: dict[str, Any]) -> HighlightCandidate | None:
    from app.analysis.scoring_config import get_scoring_config
    from app.pipeline.workers.analyze import _draft_clusters_existing, _get_or_create_candidate, _get_or_create_event

    candidate = db.get(HighlightCandidate, target.candidate_id) if target.candidate_id is not None else None
    if target.candidate_id is not None and candidate is None:
        raise ValueError("重分析候选在提交时消失")
    selected = payload["decision"] == "candidate"
    if candidate is None and not selected:
        return None
    if (
        candidate is None
        and db.exec(
            select(HighlightCandidate.id).where(
                HighlightCandidate.dedup_hash == payload["dedup_hash"],
            )
        ).first()
        is not None
    ):
        # 计算期间产生的同业务键记录不属于本次快照，不能借幂等插入覆盖它。
        return None
    if isinstance(payload["start_ts"], str):
        payload = dict(payload)
        for key in ("start_ts", "end_ts", "peak_ts"):
            payload[key] = datetime.fromisoformat(payload[key])
    if candidate is not None:
        payload = dict(payload, dedup_hash=candidate.dedup_hash)
    cfg = get_scoring_config()
    if selected and _draft_clusters_existing(
        db, payload, dedup_hash=payload["dedup_hash"], cooldown_s=cfg.cooldown_s, iou_threshold=cfg.iou_threshold
    ):
        if candidate is None:
            return None
        selected = False
        payload = dict(payload, reason=f"{payload['reason']}；与既有候选重叠或处于同事件冷却期")
    status = payload["initial_status"] if selected else CandidateStatus.REJECTED
    if candidate is None:
        candidate = _get_or_create_candidate(
            db,
            payload["dedup_hash"],
            target.session_id,
            payload["peak_ts"],
            payload["start_ts"],
            payload["end_ts"],
            payload["rule_score"],
            payload.get("llm_score", 0.0),
            payload["highlight_score"],
            payload["features_json"],
            payload["reason"],
            status,
        )
    else:
        for name in ("start_ts", "end_ts", "peak_ts", "rule_score", "llm_score", "highlight_score", "reason"):
            setattr(candidate, name, payload[name])
        candidate.features_json = _merge_machine_features(candidate.features_json, payload["features_json"])
        candidate.status = status
        db.add(candidate)
    event_id = _get_or_create_event(
        db,
        candidate.id,
        target.session_id,
        payload["start_ts"],
        payload["end_ts"],
        payload["rule_score"],
        payload.get("llm_score", 0.0),
        payload["highlight_score"],
        payload["features_json"],
        payload["reason"],
        segment_id=target.segment_id,
        asr_text=payload.get("asr_text"),
    )
    event = db.get(HighlightEvent, event_id)
    assert event is not None
    if target.event_id is not None and event_id != target.event_id:
        raise ValueError("重分析不得替换原有复核事件")
    if event.adjusted_start_ts is not None:
        event.adjusted_start_ts = payload["start_ts"]
    if event.adjusted_end_ts is not None:
        event.adjusted_end_ts = payload["end_ts"]
    event.raw_start_ts, event.raw_end_ts = payload["start_ts"], payload["end_ts"]
    for name in ("rule_score", "llm_score", "highlight_score", "reason"):
        setattr(event, name, payload[name])
    event.features_json = _merge_machine_features(event.features_json, payload["features_json"])
    event.asr_text = payload.get("asr_text")
    event.review_status = ReviewStatus.PENDING if selected else ReviewStatus.REJECTED
    event.review_reason = None if selected else payload["reason"]
    event.updated_at = datetime.now(UTC)
    db.add(event)
    return candidate


def _persist_window(db: Session, target: ReanalysisTarget, payload: dict[str, Any], candidate_id: int | None) -> None:
    snapshot = payload["editorial_snapshot"]
    assert isinstance(snapshot, SegmentReviewSnapshot)
    key = f"editorial_segment:{target.segment_id}"
    row = db.get(AppSetting, key) or AppSetting(key=key, value="{}")
    stored = json.loads(row.value)
    windows = stored.get("windows", [])
    window = {
        "start_ts": snapshot.start_ts.isoformat(),
        "end_ts": snapshot.end_ts.isoformat(),
        "peak_ts": snapshot.peak_ts.isoformat(),
        "editorial_review": snapshot.review.metadata(),
        "candidate_id": candidate_id,
    }
    windows = [
        item
        for item in windows
        if abs(datetime_epoch(datetime.fromisoformat(item["peak_ts"])) - datetime_epoch(snapshot.peak_ts)) >= 1e-6
    ]
    windows.append(window)
    row.value = json.dumps(
        {"schema_version": "segment-editorial-v1", "segment_id": target.segment_id, "windows": windows},
        ensure_ascii=False,
        allow_nan=False,
    )
    db.add(row)


def _update_idle_tasks(db: Session, candidate: HighlightCandidate, own_task_id: int | None) -> None:
    """仅撤回尚未开始的机器下游动作；活跃执行和人工决定已由前置保护排除。"""
    tasks = db.exec(select(SegmentTask).where(SegmentTask.candidate_id == candidate.id)).all()
    for task in tasks:
        if task.id == own_task_id:
            continue
        task.stage = (
            TaskStatus.COMPLETED if candidate.status == CandidateStatus.REJECTED else TaskStatus.AWAITING_REVIEW
        )
        task.stage_key = make_stage_key(task.segment_id, task.stage)
        task.failed_stage = None
        task.next_retry_at = None
        task.last_error = None
        task.error_is_permanent = False
        db.add(task)


def commit_reanalysis(
    db: Session, drafts: list[ReanalysisDraft], *, own_task_id: int | None = None
) -> dict[str, JsonValue]:
    """在调用方的写事务内核验整批快照，再原位更新结果和闲置任务。"""
    checked = [(draft, draft.skip_reason or _current(db, draft.target, own_task_id)) for draft in drafts]
    checked = [
        (draft, reason or (None if _validate_computed(db, draft) else "evidence_changed")) for draft, reason in checked
    ]
    check_shutdown()
    results: list[JsonValue] = []
    for draft, reason in checked:
        target, payload = draft.target, draft.payload
        if reason is not None or payload is None:
            results.append({"key": target.key, "status": "skipped", "reason": reason})
            continue
        if target.kind == "hotspot":
            clip = payload["clip_draft"]
            enrichment = payload["enrichment"]
            if enrichment is not None and not commit_event_enrichment(db, enrichment):
                raise ValueError("已校验的热点解释在同一事务中失效")
            candidate = _persist_candidate(db, target, clip.to_payload())
            hotspot = db.get(HotspotEvent, target.source_id)
            assert hotspot is not None
            if candidate is not None:
                hotspot.candidate_id = candidate.id
            committed_clip = clip
            if clip.decision == "candidate" and (candidate is None or candidate.status == CandidateStatus.REJECTED):
                committed_clip = replace(clip, decision="duplicate")
            mark_event_clip_evaluated(db, hotspot, committed_clip)
            execution = payload["review"].execution
        else:
            candidate = _persist_candidate(db, target, payload)
            _persist_window(db, target, payload, candidate.id if candidate else None)
            execution = payload["editorial_snapshot"].review.execution
        if candidate is not None:
            _update_idle_tasks(db, candidate, own_task_id)
        results.append(
            {
                "key": target.key,
                "status": "updated",
                "candidate_id": candidate.id if candidate else None,
                "execution": execution.model_dump(mode="json"),
            }
        )
    return {
        "targets": results,
        "updated": sum(item["status"] == "updated" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
    }


def run_editorial_reanalysis(context: JobContext, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """持久作业的正式入口，业务结果与当前作业成功终态原子提交。"""
    raw = payload["targets"]
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        raise ValueError("重分析目标必须为非空且不超过 100 项")
    targets = [ReanalysisTarget.model_validate(item) for item in raw]
    if any(item.session_id != payload["session_id"] for item in targets):
        raise ValueError("重分析目标必须属于指定场次")
    # 以数据库解析身份，持久载荷不能自行构造来源、峰值或候选关联。
    with get_session() as db:
        current = {item.key: item for item in list_reanalysis_targets(db, targets[0].session_id)}
        for target in targets:
            live = current.get(target.key)
            if live is not None:
                for name in ("kind", "source_id", "session_id", "segment_id", "peak_ts", "candidate_id", "event_id"):
                    if getattr(live, name) != getattr(target, name):
                        raise ValueError("重分析载荷与服务端业务身份不一致")
    context.report(5, "正在核验重分析对象")
    drafts = compute_reanalysis(targets, cancelled=context.cancelled)
    context.check_cancelled()
    with media_validation_scope(session_ids=[item.session_id for item in targets]), context.result_transaction() as db:
        result = commit_reanalysis(db, drafts)
        context.check_cancelled()
        context.complete(db, result)
    return result
