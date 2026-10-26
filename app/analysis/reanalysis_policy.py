# ruff: noqa: D100
from __future__ import annotations

from sqlmodel import Session, or_, select

from app.db.entities import (
    AppSetting,
    CandidateStatus,
    ClipVariant,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    HighlightTopic,
    SegmentTask,
    TaskStatus,
    ThresholdFeedback,
)
from app.jobs.contracts import ACTIVE, decode_job
from app.web.services.review_workflow import claim_state, has_review_draft, workflow


def task_is_active(task: SegmentTask) -> bool:
    """阻止在执行、取消或发布任务上重新写入分析结果。"""
    return bool(
        task.claimed_by
        or task.lease_token
        or task.stage
        in {
            TaskStatus.TRANSCRIBING,
            TaskStatus.ANALYZING,
            TaskStatus.RENDERING,
            TaskStatus.PUBLISHING,
            TaskStatus.CANCELLING,
        }
    )


def task_protection_reason(task: SegmentTask) -> str | None:
    """取消和已经生成媒体的任务同样禁止被重新激活。"""
    if task_is_active(task):
        return "active_task"
    if (
        task.stage
        in {
            TaskStatus.CANCELLED,
            TaskStatus.RENDERED,
            TaskStatus.QUEUED_FOR_PUBLISH,
            TaskStatus.AWAITING_PUBLISH_CONFIRMATION,
        }
        or task.clip_id is not None
    ):
        return "downstream_task"
    return None


def event_protection_reason(db: Session, event: HighlightEvent, own_task_id: int | None = None) -> str | None:
    """统一保护人工认领、历史、边界和下游资产，不要求认领已有草稿。"""
    if event.review_by != "auto":
        return "human_decision"
    if claim_state(event)["active"]:
        return "active_claim"
    if has_review_draft(event.features_json) or workflow(event)["history"]:
        return "human_workflow"
    if (
        event.adjusted_start_ts is not None
        and event.adjusted_start_ts != event.raw_start_ts
        or event.adjusted_end_ts is not None
        and event.adjusted_end_ts != event.raw_end_ts
    ):
        return "manual_bounds"
    if event.topic_id is not None:
        return "topic"
    for task in db.exec(
        select(SegmentTask).where(
            or_(
                SegmentTask.candidate_id == event.candidate_id,
                SegmentTask.event_id == event.id,
            )
        )
    ).all():
        if task.id != own_task_id and (reason := task_protection_reason(task)):
            return reason
    for row in db.exec(select(AppSetting).where(AppSetting.key.startswith("web_job:"))).all():
        job = decode_job(row.value)
        if job["status"] not in ACTIVE or job["type"] == "editorial_reanalysis":
            continue
        payload = job["payload"]
        event_ids = payload.get("event_ids")
        if payload.get("candidate_id") == event.candidate_id or (isinstance(event_ids, list) and event.id in event_ids):
            return "active_media_job"
    for model, column, value in (
        (ClipVariant, ClipVariant.event_id, event.id),
        (HighlightTopic, HighlightTopic.event_id, event.id),
        (FinalClip, FinalClip.candidate_id, event.candidate_id),
        (ThresholdFeedback, ThresholdFeedback.candidate_id, event.candidate_id),
    ):
        if value is not None and db.exec(select(model.id).where(column == value).limit(1)).first() is not None:
            return "downstream_asset"
    candidate = db.get(HighlightCandidate, event.candidate_id)
    if candidate is None:
        return "missing_candidate"
    if candidate.status in {CandidateStatus.CLIPPED, CandidateStatus.MERGED, CandidateStatus.CLEANED}:
        return "retained_candidate"
    return None


def segment_protection_reason(db: Session, segment_id: int, *, own_task_id: int | None = None) -> str | None:
    """重评前后共同核验来源分段，自己的分析租约由调用方单独验证。"""
    for task in db.exec(select(SegmentTask).where(SegmentTask.segment_id == segment_id)).all():
        if task.id == own_task_id:
            continue
        if reason := task_protection_reason(task):
            return reason
    for event in db.exec(select(HighlightEvent).where(HighlightEvent.segment_id == segment_id)).all():
        reason = event_protection_reason(db, event, own_task_id)
        if reason is not None:
            return reason
    return None
