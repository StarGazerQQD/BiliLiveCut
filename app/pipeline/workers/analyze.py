"""分析阶段 Worker — compute/commit 分离 (V0.1.14)."""

from __future__ import annotations

import time

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.db.models import HighlightCandidate, HighlightEvent, ReviewStatus, SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_heartbeat


def analyze_compute(task_id: int) -> dict:
    """仅执行分析计算, 不创建 Event 和写入 Task 状态。

    调用 score_segment 进行精彩片段评分, 收集候选 ID。

    :param task_id: SegmentTask ID。
    :returns: {"candidate_id": int | None}
    """
    from app.analysis.highlight import score_segment

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return {"candidate_id": None}
        segment_id = task.segment_id
    candidate = score_segment(segment_id)
    cid = candidate.id if candidate else None
    return {"candidate_id": cid}


def _ensure_event(candidate_id: int) -> int | None:
    """确保每个 HighlightCandidate 有唯一 HighlightEvent (幂等)。"""
    import logging

    _logger = logging.getLogger(__name__)

    with get_session() as db:
        existing = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
        if existing is not None:
            return existing.id
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is None:
            return None
        event = HighlightEvent(
            candidate_id=candidate_id,
            session_id=cand.session_id,
            raw_start_ts=cand.start_ts,
            raw_end_ts=cand.end_ts,
            rule_score=cand.rule_score,
            llm_score=cand.llm_score,
            highlight_score=cand.highlight_score,
            features_json=cand.features_json,
            reason=cand.reason,
            review_status=ReviewStatus.PENDING,
            review_by="auto",
        )
        db.add(event)
        try:
            db.flush()
            db.refresh(event)
            _logger.info("auto event: eid=%s cid=%s", event.id, candidate_id)
            return event.id
        except IntegrityError:
            db.rollback()
            _logger.info("idempotency_conflict_resolved: event cid=%s 已被并发创建", candidate_id)

    with get_session() as db:
        existing = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
        if existing is not None:
            return existing.id
        _logger.error("IntegrityError 后无法找到已有 Event: candidate_id=%s", candidate_id)
        return None


def commit_highlight(lease: TaskLease, compute_result: dict, ms: int) -> None:
    """单事务提交分析结果, 先校验租约, 再创建 Event (幂等)。

    :param lease: 任务租约。
    :param compute_result: analyze_compute 的输出。
    :param ms: 处理耗时 (毫秒)。
    """
    import logging

    _logger = logging.getLogger(__name__)
    try:
        with get_session() as db:
            if not still_owns_lease(db, lease):
                raise LeaseLostError()
            task = db.get(SegmentTask, lease.task_id)
            if task is None:
                return
            cid = compute_result.get("candidate_id")
            event_id: int | None = None
            if cid is not None:
                event_id = _ensure_event(cid)
            mark_completed(task, ms)
            if cid is not None:
                enqueue_next(task, TaskStatus.CANDIDATE_CREATED, candidate_id=cid, event_id=event_id)
            else:
                enqueue_next(task, TaskStatus.COMPLETED)
            db.add(task)
    except LeaseLostError:
        _logger.warning("stale_result_discarded: highlight task=%s 已失去租约", lease.task_id)


def run_analyze(lease: TaskLease) -> None:
    """执行分析阶段: 计算与提交分离。

    :param lease: 任务租约。
    """
    t0 = time.time()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return
        mark_heartbeat(task)
        db.add(task)
    compute_result = analyze_compute(lease.task_id)
    ms_val = int((time.time() - t0) * 1000)
    commit_highlight(lease, compute_result, ms_val)
