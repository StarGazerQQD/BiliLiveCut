"""Candidates."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from loguru import logger
from sqlmodel import select

from app.db.models import (
    CandidateStatus,
    HighlightCandidate,
    RecordingSession,
)
from app.db.session import get_session


def set_candidate_status(candidate_id: int, status: str) -> None:
    """设置候选状态(审核:批准/拒绝),并记录阈值自学习反馈。

    :param candidate_id: 候选 id。
    :param status: 新状态。
    :raises ValueError: 候选不存在时。
    """
    with get_session() as db:
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is None:
            raise ValueError(f"候选不存在: id={candidate_id}")
        cand.status = status
        db.add(cand)

    # V0.1.2:记录阈值自学习反馈。
    if status in (CandidateStatus.APPROVED, CandidateStatus.REJECTED):
        try:
            from app.analysis import threshold_learning as tl

            action = "approved" if status == CandidateStatus.APPROVED else "rejected"
            with get_session() as db:
                session = db.get(RecordingSession, cand.session_id)
                if session is not None:
                    tl.record_feedback(session.room_id, candidate_id, action)
                    # 尝试自动调整阈值。
                    tl.apply_threshold_if_changed(session.room_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("阈值自学习反馈记录失败: {}", exc)


def approve_candidate_sync(
    candidate_id: int,
    *,
    progress_callback: Callable[[int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> int | None:
    """同步批准候选并出片，供后台作业线程调用。

    在同一个 session 中完成审批 + produce_clip,
    fallback 路径也同步更新 Task 状态。

    :param candidate_id: 候选 id。
    :param progress_callback: 可选的作业进度回调。
    :param cancel_check: 可选的取消检查。
    :returns: 生成的 clip_id;失败返回 ``None``。
    """
    from app.db.models import HighlightEvent, SegmentTask
    from app.db.models import TaskStatus as _Ts
    from app.pipeline.approval import approve_event_and_task

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

        # V0.1.12.8: 统一审批, 传入外层 db session
        if task is not None and event is not None:
            approve_event_and_task(
                task_id=task.id,
                event_id=event.id,
                approved_by="web_admin",
                reason=None,
                source="human",
                review_decision="approved_solo",
                db=db,
            )
        else:
            # fallback: 更新 Candidate + Task 状态
            set_candidate_status(candidate_id, CandidateStatus.APPROVED)
            if task is not None:
                task.stage = _Ts.APPROVED
                db.add(task)

    from app.pipeline.orchestrator import produce_clip

    clip = produce_clip(
        candidate_id,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
    )
    return clip.id if clip else None


async def approve_candidate(candidate_id: int) -> int | None:
    """向后兼容的异步批准入口。"""
    return await asyncio.to_thread(approve_candidate_sync, candidate_id)


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
        }
        for c in rows
    ]
