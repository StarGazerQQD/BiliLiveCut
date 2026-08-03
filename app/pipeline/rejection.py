"""统一同步人工拒绝决策到候选、事件、任务和成片。"""

from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger
from sqlmodel import Session, select

from app.db.models import (
    CandidateStatus,
    ClipStatus,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
)
from app.pipeline.stage_result import can_transition, enqueue_next


def reject_candidate_and_outputs(
    db: Session,
    candidate_id: int,
    *,
    rejected_by: str,
    reason: str | None = None,
    review_decision: str = ReviewStatus.REJECTED,
) -> None:
    """在同一事务中拒绝候选并停止其未终结工作流。

    已发布成片代表真实的外部发布结果，不能因事后的候选决策而改写；
    其余关联成片会标记为拒绝，仍在运行或排队的任务会失去租约并取消。

    :param db: 调用方持有的数据库会话。
    :param candidate_id: 被拒绝的候选 ID。
    :param rejected_by: 审核操作者标识。
    :param reason: 审核原因或备注。
    :param review_decision: 明确负向审核决断。
    :raises ValueError: 候选不存在或决断不是明确拒绝时。
    """
    if review_decision not in {ReviewStatus.REJECTED, ReviewStatus.NOT_EXCITING}:
        raise ValueError(f"不能用拒绝流程处理审核决断: {review_decision}")

    candidate = db.get(HighlightCandidate, candidate_id)
    if candidate is None:
        raise ValueError(f"候选不存在: id={candidate_id}")
    candidate.status = CandidateStatus.REJECTED
    db.add(candidate)

    event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
    if event is not None:
        event.review_status = review_decision
        event.review_reason = reason
        event.review_by = rejected_by
        event.updated_at = datetime.now(UTC)
        db.add(event)

    cancelled_tasks = 0
    tasks = db.exec(select(SegmentTask).where(SegmentTask.candidate_id == candidate_id)).all()
    for task in tasks:
        if not can_transition(task.stage, TaskStatus.CANCELLED):
            continue
        enqueue_next(task, TaskStatus.CANCELLED)
        db.add(task)
        cancelled_tasks += 1

    rejected_clips = 0
    clips = db.exec(select(FinalClip).where(FinalClip.candidate_id == candidate_id)).all()
    for clip in clips:
        if clip.status in {ClipStatus.PUBLISHED, ClipStatus.REJECTED}:
            continue
        clip.status = ClipStatus.REJECTED
        db.add(clip)
        rejected_clips += 1

    logger.info(
        "candidate_rejected: candidate={} decision={} actor={} cancelled_tasks={} rejected_clips={}",
        candidate_id,
        review_decision,
        rejected_by,
        cancelled_tasks,
        rejected_clips,
    )


__all__ = ["reject_candidate_and_outputs"]
