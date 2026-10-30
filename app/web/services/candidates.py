"""Candidates."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlmodel import select

from app.core.media_usage import media_reference_scope
from app.db.entities import (
    CandidateStatus,
    HighlightCandidate,
    RecordingSession,
    ReviewStatus,
)
from app.db.session import get_session

if TYPE_CHECKING:
    from app.jobs.context import JobContext


@media_reference_scope()
def set_candidate_status(candidate_id: int, status: str, *, reviewed_by: str = "web_admin") -> None:
    """设置候选状态(审核:批准/拒绝),并记录阈值自学习反馈。

    :param candidate_id: 候选 id。
    :param status: 新状态。
    :param reviewed_by: 发起人工决策的审核者。
    :raises ValueError: 候选不存在时。
    """
    with get_session() as db:
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is None:
            raise ValueError(f"候选不存在: id={candidate_id}")
        session_id = cand.session_id
        if status == CandidateStatus.REJECTED:
            from app.pipeline.rejection import reject_candidate_and_outputs

            reject_candidate_and_outputs(
                db,
                candidate_id,
                rejected_by=reviewed_by,
            )
        else:
            cand.status = status
            db.add(cand)

    # V0.1.2:记录阈值自学习反馈。
    if status in (CandidateStatus.APPROVED, CandidateStatus.REJECTED):
        try:
            from app.analysis import threshold_learning as tl

            action = "approved" if status == CandidateStatus.APPROVED else "rejected"
            with get_session() as db:
                session = db.get(RecordingSession, session_id)
                if session is not None:
                    tl.record_feedback(session.room_id, candidate_id, action)
                    # 尝试自动调整阈值。
                    tl.apply_threshold_if_changed(session.room_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("阈值自学习反馈记录失败: {}", exc)
        from app.pipeline.highlight_feedback import record_candidate_review_feedback

        decision = ReviewStatus.APPROVED_SOLO if status == CandidateStatus.APPROVED else ReviewStatus.REJECTED
        record_candidate_review_feedback(
            candidate_id,
            decision=decision,
            reviewed_by=reviewed_by,
        )


@media_reference_scope()
def approve_candidate_sync(
    candidate_id: int,
    *,
    reviewed_by: str = "web_admin",
    progress_callback: Callable[[int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    job_context: JobContext | None = None,
) -> int | None:
    """同步批准候选并出片，供后台作业线程调用。

    审批作为独立输入保存；媒体、文案与执行结果在计算结束后共同提交。
    候选缺少审核事件时拒绝出片。

    :param candidate_id: 候选 id。
    :param reviewed_by: 发起人工批准的审核者。
    :param progress_callback: 可选的作业进度回调。
    :param cancel_check: 可选的取消检查。
    :returns: 生成的 clip_id。
    """
    from app.core.process_control import ProcessCancelledError
    from app.db.entities import HighlightEvent, SegmentTask
    from app.db.entities import TaskStatus as _Ts
    from app.jobs.contracts import JobCancelled
    from app.pipeline.approval import approve_event_and_task
    from app.pipeline.heartbeat import start_heartbeat_thread
    from app.pipeline.highlight_feedback import record_candidate_review_feedback
    from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
    from app.pipeline.lifecycle import _WORKER_ID, finish_execution, now_utc, register_execution
    from app.pipeline.scheduler import settle_cancellation
    from app.pipeline.stage_result import enqueue_next, mark_completed, mark_failed, request_cancellation

    claimed_task_id: int | None = None
    claimed_lease_token: str | None = None
    with get_session() as db:
        # 查找关联 task 和 event
        task = db.exec(
            select(SegmentTask)
            .where(
                SegmentTask.candidate_id == candidate_id,
            )
            .order_by(SegmentTask.created_at.desc())
        ).first()
        event = db.exec(
            select(HighlightEvent).where(
                HighlightEvent.candidate_id == candidate_id,
            )
        ).first()

        if event is None:
            raise ValueError(f"候选数据不完整：缺少审核事件 candidate_id={candidate_id}")
        if task is not None and task.stage in {
            _Ts.TRANSCRIBING,
            _Ts.ANALYZING,
            _Ts.RENDERING,
            _Ts.PUBLISHING,
            _Ts.CANCELLING,
        }:
            raise RuntimeError("任务正在执行或取消中，不能重复人工出片")
        approved = approve_event_and_task(
            task_id=task.id if task is not None else None,
            event_id=event.id,
            approved_by=reviewed_by,
            reason=None,
            source="human",
            review_decision="approved_solo",
            db=db,
        )
        if not approved:
            raise RuntimeError(f"候选审批状态提交失败: candidate_id={candidate_id}")

        if task is not None and task.stage in (_Ts.APPROVED, _Ts.APPROVED_WAITING_RENDER):
            import uuid

            enqueue_next(task, _Ts.QUEUED_FOR_RENDER)
            enqueue_next(task, _Ts.RENDERING)
            claimed_lease_token = uuid.uuid4().hex
            claimed_at = now_utc()
            task.claimed_by = _WORKER_ID
            task.claimed_at = claimed_at
            task.heartbeat_at = claimed_at
            task.lease_token = claimed_lease_token
            task.started_at = claimed_at
            task.attempts += 1
            db.add(task)
            claimed_task_id = task.id

    record_candidate_review_feedback(
        candidate_id,
        decision=ReviewStatus.APPROVED_SOLO,
        reviewed_by=reviewed_by,
    )

    from app.pipeline.orchestrator import produce_clip

    lease = (
        TaskLease(claimed_task_id, _WORKER_ID, claimed_lease_token, _Ts.RENDERING)
        if claimed_task_id is not None and claimed_lease_token is not None
        else None
    )

    def cancelled() -> bool:
        if cancel_check is not None and cancel_check():
            return True
        if lease is not None:
            with get_session() as db:
                return not still_owns_lease(db, lease)
        return False

    if lease is not None:
        register_execution(lease.task_id, lease.lease_token)

    heartbeat_stop: threading.Event | None = None
    try:
        if lease is not None:
            heartbeat_stop = start_heartbeat_thread(lease.task_id, lease.lease_token, _Ts.RENDERING)
        clip = produce_clip(
            candidate_id,
            progress_callback=progress_callback,
            cancel_check=cancelled,
            task_lease=lease,
            output_suffix=f"lease_{lease.lease_token}" if lease is not None else None,
            job_context=job_context,
        )
    except Exception as exc:
        if claimed_task_id is not None:
            with get_session() as db:
                claimed_task = db.get(SegmentTask, claimed_task_id)
                if (
                    claimed_task is not None
                    and claimed_task.stage == _Ts.RENDERING
                    and claimed_task.lease_token == claimed_lease_token
                ):
                    if isinstance(exc, (JobCancelled, ProcessCancelledError, LeaseLostError)):
                        request_cancellation(claimed_task)
                    else:
                        mark_failed(claimed_task, f"人工出片失败: {exc}", permanent=False)
                    db.add(claimed_task)
        raise
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if lease is not None:
            finish_execution(lease.task_id, lease.lease_token)
            settle_cancellation(lease)
    if claimed_task_id is not None:
        with get_session() as db:
            claimed_task = db.get(SegmentTask, claimed_task_id)
            if (
                claimed_task is not None
                and claimed_task.stage == _Ts.RENDERING
                and claimed_task.lease_token == claimed_lease_token
            ):
                if clip is None or clip.id is None:
                    mark_failed(claimed_task, "人工出片未生成成品", permanent=False)
                else:
                    mark_completed(claimed_task)
                    enqueue_next(claimed_task, _Ts.RENDERED, clip_id=clip.id)
                db.add(claimed_task)
    return clip.id if clip else None


def delete_candidate(candidate_id: int) -> None:
    """删除候选。

    :param candidate_id: 候选 id。
    """
    with get_session() as db:
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is not None:
            db.delete(cand)


def list_candidates(limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
    """列出高光候选(按分数降序)。

    :param limit: 数量上限。
    :param status: 可选状态过滤。
    :returns: 候选字典列表。
    """
    with get_session() as db:
        stmt = select(HighlightCandidate).order_by(
            HighlightCandidate.highlight_score.desc()  # type: ignore[attr-defined]
        )
        if status:
            stmt = stmt.where(HighlightCandidate.status == status)
        rows = db.exec(stmt).all()[:limit]
        from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

        sources = source_identities_for_sessions(db, (candidate.session_id for candidate in rows))
    return [
        {
            "id": c.id,
            "session_id": c.session_id,
            "highlight_score": round(c.highlight_score, 3),
            "rule_score": round(c.rule_score, 3),
            "llm_score": round(c.llm_score, 3),
            "status": c.status,
            "reason": c.reason,
            "peak_ts": c.peak_ts.isoformat() if c.peak_ts else None,
            "features": json.loads(c.features_json) if c.features_json else {},
            **sources.get(c.session_id, unknown_source_identity()),
        }
        for c in rows
    ]
