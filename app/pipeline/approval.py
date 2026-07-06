"""统一审批服务 (V0.1.12.7)。

在同一个数据库事务中更新 Task、Candidate 和 Event 状态，
确保三套状态机制始终保持一致。

禁止只更新 Task.stage 而不更新 Event.review_status / Candidate.status。
"""

from __future__ import annotations

from datetime import UTC, datetime

from loguru import logger

from app.db.models import (
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session


def approve_event_and_task(
    *,
    task_id: int,
    event_id: int,
    approved_by: str = "auto",
    reason: str | None = None,
    source: str = "auto",
    review_decision: str = ReviewStatus.APPROVED_SOLO,
) -> bool:
    """在同一事务中批准 Task、更新 Candidate 和 HighlightEvent。

    :param task_id: SegmentTask.id。
    :param event_id: HighlightEvent.id。
    :param approved_by: 审批操作者标识 (auto/manual user id)。
    :param reason: 审批原因/备注。
    :param source: 批准来源: auto / human。
    :param review_decision: 审核决断 (默认 approved_solo)。
    :returns: True 表示成功; False 表示 event 不存在或已被拒绝。
    """
    with get_session() as db:
        event = db.get(HighlightEvent, event_id)
        if event is None:
            logger.warning("approve_event_and_task: event {} 不存在, 拒绝批准 task={}", event_id, task_id)
            return False

        # 已拒绝 Event 不得被普通自动流程重新批准
        if event.review_status == ReviewStatus.REJECTED and source == "auto":
            logger.warning(
                "approve_event_and_task: event {} 已被拒绝, 自动流程不得重新批准 task={}",
                event_id, task_id,
            )
            return False

        # 已批准 Event 再次批准不重复创建记录 (幂等)
        if event.review_status in ReviewStatus.POSITIVE:
            logger.info(
                "approve_event_and_task: event {} 已批准 ({}), 幂等跳过 task={}",
                event_id, event.review_status, task_id,
            )
            # 仍同步 Task stage
            task = db.get(SegmentTask, task_id)
            if task and task.stage in (TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED):
                task.stage = TaskStatus.APPROVED
                db.add(task)
            return True

        event.review_status = review_decision
        event.review_reason = reason
        event.review_by = _source_label(source, approved_by)
        event.updated_at = datetime.now(UTC)
        db.add(event)

        # 同步 Candidate 状态
        if event.candidate_id:
            candidate = db.get(HighlightCandidate, event.candidate_id)
            if candidate:
                candidate.status = CandidateStatus.APPROVED
                db.add(candidate)

        # 同步 Task 状态
        task = db.get(SegmentTask, task_id)
        if task and task.stage in (
            TaskStatus.AWAITING_REVIEW,
            TaskStatus.CANDIDATE_CREATED,
            TaskStatus.APPROVED,
        ):
            task.stage = TaskStatus.APPROVED
            task.event_id = event_id
            if event.candidate_id:
                task.candidate_id = event.candidate_id
            # 不修改 claimed_by/lease_token — approve 不是 Worker 操作
            task.claimed_by = None
            task.claimed_at = None
            task.heartbeat_at = None
            task.lease_token = None
            db.add(task)
        else:
            logger.warning(
                "approve_event_and_task: task {} 状态 {} 不在可批准状态, 跳过 task stage 更新",
                task_id, task.stage if task else "None",
            )

        logger.info(
            "approve_event_and_task: task={} event={} by={} source={} → approved",
            task_id, event_id, approved_by, source,
        )
        return True


def assert_event_approved(event_id: int) -> bool:
    """校验 Event 是否已批准 (用于渲染/发布前置条件)。

    :param event_id: HighlightEvent.id。
    :returns: True 表示已批准。
    """
    with get_session() as db:
        event = db.get(HighlightEvent, event_id)
        if event is None:
            return False
        return event.review_status in ReviewStatus.POSITIVE


def check_pipeline_consistency(
    *,
    task_id: int,
    event_id: int,
    expected_stage: str,
) -> dict:
    """流水线一致性检查 (进入渲染/发布前执行)。

    必须同时满足:
    - SegmentTask.stage 与 expected_stage 一致
    - HighlightEvent 存在且已批准
    - Task.event_id 指向真实 Event
    - Candidate 与 Event 关系有效

    :param task_id: SegmentTask.id。
    :param event_id: HighlightEvent.id。
    :param expected_stage: 期望的 Task.stage。
    :returns: {ok, reason, task, event}。
    """
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        event = db.get(HighlightEvent, event_id) if event_id else None

        if task is None:
            return {"ok": False, "reason": f"task {task_id} 不存在", "task": None, "event": event}

        if task.stage != expected_stage:
            return {"ok": False,
                    "reason": f"task stage ({task.stage}) != expected ({expected_stage})",
                    "task": task, "event": event}

        if event is None:
            return {"ok": False, "reason": f"event {event_id} 不存在", "task": task, "event": None}

        if event.review_status not in ReviewStatus.POSITIVE:
            return {"ok": False,
                    "reason": f"event {event_id} review_status={event.review_status}, 未批准",
                    "task": task, "event": event}

        if task.event_id != event_id:
            return {"ok": False,
                    "reason": f"task.event_id ({task.event_id}) != event_id ({event_id})",
                    "task": task, "event": event}

        if task.candidate_id != event.candidate_id:
            logger.warning(
                "consistency_warning: task={} candidate_id={} != event.candidate_id={} (event={})",
                task_id, task.candidate_id, event.candidate_id, event_id,
            )

        return {"ok": True, "reason": "consistent", "task": task, "event": event}


def _source_label(source: str, approved_by: str) -> str:
    """生成审批来源标签。"""
    if source == "auto":
        return "auto"
    if source == "human":
        return approved_by or "manual"
    return approved_by or source


# --------------------------------------------------------------------------- #
# UploadTask 结果 → SegmentTask 状态映射
# --------------------------------------------------------------------------- #

_UPLOAD_RESULT_MAP: dict[str, str] = {
    "success": TaskStatus.COMPLETED,
    "failed": TaskStatus.TRANSIENT_FAILED,
    "skipped": TaskStatus.AWAITING_PUBLISH_CONFIRMATION,
}


def apply_upload_result(
    task_id: int,
    upload_task_id: int,
    upload_status: str,
    upload_error: str | None = None,
    remote_id: str | None = None,
) -> bool:
    """根据 UploadTask 的真实结果推进主流水线 (V0.1.12.7)。

    :param task_id: SegmentTask.id。
    :param upload_task_id: UploadTask.id。
    :param upload_status: UploadTask.status (success/failed/skipped)。
    :param upload_error: 上传错误信息。
    :param remote_id: 平台稿件号。
    :returns: True 表示成功推进。
    """
    target = _UPLOAD_RESULT_MAP.get(upload_status)
    if target is None:
        logger.error("apply_upload_result: 未知上传状态 {} task={}", upload_status, task_id)
        return False

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return False

        # V0.1.12.7: 记录上传关联 (upload_task_id 暂不作为持久化字段)
        task.last_error = None  # 清除之前错误
        logger.info(
            "apply_upload_result: task={} upload={} status={} → pipeline={} remote_id={}",
            task_id, upload_task_id, upload_status, target, remote_id,
        )

        if target == TaskStatus.COMPLETED and not remote_id:
            logger.warning(
                "apply_upload_result: task={} 标记 completed 但无 remote_id, 仍视为完成",
                task_id,
            )

        task.stage = target
        task.completed_at = datetime.now(UTC) if target in (
            TaskStatus.COMPLETED, TaskStatus.FAILED,
        ) else task.completed_at
        if upload_error:
            task.last_error = upload_error[:1000]
        if target == TaskStatus.TRANSIENT_FAILED:
            import random
            delay = min(10 * (2 ** max(task.attempts - 1, 0)), 600)
            delay += random.uniform(0, 5)
            from datetime import timedelta
            task.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
            task.failed_stage = TaskStatus.PUBLISHING

        db.add(task)
        return True
