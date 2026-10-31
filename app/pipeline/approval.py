"""统一审批服务。

在同一个数据库事务中更新 Task、Candidate 和 Event 状态，
确保三套状态机制始终保持一致。

V0.1.12.8: approve_event_and_task 接受可选 db session 参数，
调用方可传入外层 session 以确保与外层操作在同一事务中提交。

禁止只更新 Task.stage 而不更新 Event.review_status / Candidate.status。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from functools import partial

from loguru import logger
from sqlmodel import Session

from app.analysis.editorial_review_sources import automatic_review_is_current, media_validation_scope
from app.core.media_usage import media_reference_scope
from app.db.entities import (
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
)
from app.db.notifications import log_after_commit
from app.db.session import get_session
from app.pipeline.stage_result import enqueue_next


@contextmanager
def _approval_session(db: Session | None, event_id: int, *, validate_media: bool) -> Iterator[Session]:
    if db is not None:
        # 外部事务由调用方在创建会话前取得媒体门，此处不倒置锁顺序。
        yield db
    else:
        with media_reference_scope():
            ids: list[int] = []
            if validate_media:
                with get_session(operation="approve_event_source") as source_db:
                    event = source_db.get(HighlightEvent, event_id)
                    if event is not None:
                        ids.append(event.session_id)
            validation = media_validation_scope(session_ids=ids) if validate_media else nullcontext()
            with validation, get_session(operation="approve_event") as owned:
                yield owned


def approve_event_and_task(
    *,
    task_id: int | None,
    event_id: int,
    approved_by: str = "auto",
    reason: str | None = None,
    source: str = "auto",
    review_decision: str = ReviewStatus.APPROVED_SOLO,
    db: Session | None = None,
) -> bool:
    """在同一事务中批准 Task、更新 Candidate 和 HighlightEvent。

    V0.1.12.8: 接受可选 db session。若调用方已持有 session,
    传入后所有更新在同一事务中完成, 消除双写风险。

    :param task_id: SegmentTask.id；多爆点候选没有独立任务时可为 ``None``。
    :param event_id: HighlightEvent.id。
    :param approved_by: 审批操作者标识 (auto/manual user id)。
    :param reason: 审批原因/备注。
    :param source: 批准来源: auto / human。
    :param review_decision: 审核决断 (默认 approved_solo)。
    :param db: 可选的外部 session (V0.1.12.8)。
    :returns: True 表示成功; False 表示 event 不存在或已被拒绝。
    """
    with _approval_session(db, event_id, validate_media=source == "auto") as db:
        event = db.get(HighlightEvent, event_id)
        if event is None:
            logger.warning("approve_event_and_task: event {} 不存在, 拒绝批准 task={}", event_id, task_id)
            return False

        # 当前 Schema 要求每个 Event 必须关联真实 Candidate；无论事件处于
        # 哪个状态，都先验证关系，禁止幂等分支掩盖损坏记录。
        candidate = db.get(HighlightCandidate, event.candidate_id)
        if candidate is None or candidate.session_id != event.session_id:
            logger.error(
                "approve_event_and_task: event {} 关联的 candidate {} 不存在",
                event_id,
                event.candidate_id,
            )
            return False

        task = db.get(SegmentTask, task_id) if task_id is not None else None
        segment = db.get(RawSegment, task.segment_id) if task is not None else None
        event_segment = db.get(RawSegment, event.segment_id) if event.segment_id is not None else None
        if (
            task_id is not None
            and (
                task is None
                or task.session_id != event.session_id
                or task.candidate_id not in (None, candidate.id)
                or task.event_id not in (None, event.id)
                or task.candidate_id is None
                and task.event_id is None
                and task.segment_id != event.segment_id
                or segment is None
                or segment.session_id != event.session_id
            )
            or event.segment_id is not None
            and (event_segment is None or event_segment.session_id != event.session_id)
        ):
            logger.error("审批来源关联不一致: task={} event={}", task_id, event_id)
            return False

        # 已拒绝 Event 不得被普通自动流程重新批准
        if source == "auto" and (
            event.review_status == ReviewStatus.REJECTED or candidate.status == CandidateStatus.REJECTED
        ):
            logger.warning(
                "approve_event_and_task: event {} 已被拒绝, 自动流程不得重新批准 task={}",
                event_id,
                task_id,
            )
            return False

        if source == "auto" and not automatic_review_is_current(db, event, candidate):
            logger.info("编辑判断或当前来源未满足自动批准条件: event_id={}", event_id)
            return False

        label = _source_label(source, approved_by)
        if (
            event.review_status not in ReviewStatus.POSITIVE
            or source == "human"
            and (event.review_by != label or event.review_status != review_decision or event.review_reason != reason)
        ):
            event.review_status = review_decision
            event.review_reason = reason
            event.review_by = label
            event.updated_at = datetime.now(UTC)
            db.add(event)

        if candidate.status != CandidateStatus.CLIPPED:
            candidate.status = CandidateStatus.APPROVED
            db.add(candidate)

        # 同步 Task 状态
        if task is not None:
            task.candidate_id, task.event_id = candidate.id, event.id
            db.add(task)
        if task and task.stage in (
            TaskStatus.AWAITING_REVIEW,
            TaskStatus.CANDIDATE_CREATED,
            TaskStatus.REVIEWED_WAITING_ACTION,
            TaskStatus.APPROVED,
        ):
            if task.stage != TaskStatus.APPROVED:
                enqueue_next(
                    task,
                    TaskStatus.APPROVED,
                    candidate_id=event.candidate_id,
                    event_id=event_id,
                )
            db.add(task)
        elif task_id is not None:
            logger.warning(
                "approve_event_and_task: task {} 状态 {} 不在可批准状态, 跳过 task stage 更新",
                task_id,
                task.stage if task else "None",
            )

        log_after_commit(
            db,
            partial(
                logger.info,
                "approve_event_and_task: task={} event={} by={} source={} → approved",
                task_id,
                event_id,
                approved_by,
                source,
            ),
        )
        return True


def assert_event_approved(event_id: int, db: Session | None = None) -> bool:
    """校验 Event 是否已批准 (用于渲染/发布前置条件)。

    :param event_id: HighlightEvent.id。
    :returns: True 表示已批准。
    """
    if db is None:
        with get_session() as connection:
            return assert_event_approved(event_id, connection)
    event = db.get(HighlightEvent, event_id)
    return event is not None and event.review_status in ReviewStatus.POSITIVE


def render_approval_error(db: Session, task: SegmentTask) -> str | None:
    """人工批准可进入渲染；机器批准还须保有当前有效的完整证据。"""
    from app.pipeline.lease import approved_task_candidate

    try:
        candidate = approved_task_candidate(db, task)
    except ValueError as exc:
        return str(exc)
    event = db.get(HighlightEvent, task.event_id)
    assert event is not None
    if event.review_by == "auto" and not automatic_review_is_current(db, event, candidate):
        return "自动批准的编辑判断或来源已失效，需要人工复核"
    return None


def automatic_publish_error(db: Session, task: SegmentTask) -> str | None:
    """自动发布在当前事务复验全局及房间授权、阈值和编辑来源。"""
    from app.core.settings_store import current_bool
    from app.pipeline.lease import approved_task_candidate

    try:
        candidate = approved_task_candidate(db, task)
    except ValueError as exc:
        return str(exc)
    recording = db.get(RecordingSession, task.session_id)
    room = db.get(LiveRoom, recording.room_id) if recording is not None else None
    if not current_bool(db, "biliup_enabled") or not current_bool(db, "auto_upload"):
        return "全局自动上传授权已关闭"
    if room is None or not room.authorized or not room.auto_upload:
        return "房间未授权自动上传"
    if candidate.highlight_score < room.auto_publish_threshold:
        return "当前候选未达到自动发布阈值"
    event = db.get(HighlightEvent, task.event_id)
    assert event is not None
    if not automatic_review_is_current(db, event, candidate):
        return "当前编辑判断不满足自动发布条件，请人工确认上传"
    return None


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
            return {
                "ok": False,
                "reason": f"task stage ({task.stage}) != expected ({expected_stage})",
                "task": task,
                "event": event,
            }

        if event is None:
            return {"ok": False, "reason": f"event {event_id} 不存在", "task": task, "event": None}

        if event.review_status not in ReviewStatus.POSITIVE:
            return {
                "ok": False,
                "reason": f"event {event_id} review_status={event.review_status}, 未批准",
                "task": task,
                "event": event,
            }

        if task.event_id != event_id:
            return {
                "ok": False,
                "reason": f"task.event_id ({task.event_id}) != event_id ({event_id})",
                "task": task,
                "event": event,
            }

        if task.candidate_id != event.candidate_id:
            logger.warning(
                "consistency_warning: task={} candidate_id={} != event.candidate_id={} (event={})",
                task_id,
                task.candidate_id,
                event.candidate_id,
                event_id,
            )

        return {"ok": True, "reason": "consistent", "task": task, "event": event}


def _source_label(source: str, approved_by: str) -> str:
    """生成审批来源标签。"""
    if source == "auto":
        return "auto"
    if source == "human":
        return "human:auto" if approved_by == "auto" else approved_by or "manual"
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
            task_id,
            upload_task_id,
            upload_status,
            target,
            remote_id,
        )

        if target == TaskStatus.COMPLETED and not remote_id:
            logger.warning(
                "apply_upload_result: task={} 标记 completed 但无 remote_id, 仍视为完成",
                task_id,
            )

        task.stage = target
        task.completed_at = (
            datetime.now(UTC)
            if target
            in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
            )
            else task.completed_at
        )
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
