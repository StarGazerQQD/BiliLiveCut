"""任务调度器 — 阶段推进函数、重试、任务执行。"""

from __future__ import annotations

import threading
import time as _time_mod
from collections import Counter
from functools import partial
from typing import TYPE_CHECKING

from sqlalchemy import case, func
from sqlmodel import select

from app.analysis.editorial_review_sources import media_validation_scope
from app.analysis.transcription.process_pool import ASRProcessExitError, ASRShutdownError, RemoteASRError
from app.core.asr_failures import permanent_device_error
from app.core.cancellation import OperationCancelledError, OperationShutdownError, cancellation_scope
from app.core.config import settings
from app.core.execution import resource_scope
from app.core.media_usage import media_reference_scope
from app.core.process_control import ProcessShutdownError
from app.core.runtime_settings import configured_entry
from app.core.sanitize import safe_exception_summary
from app.db.entities import (
    HighlightCandidate,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentTask,
    SessionStatus,
    TaskStatus,
    Transcript,
)
from app.db.entities import SegmentStatus as OldStatus
from app.db.notifications import log_after_commit
from app.db.session import get_session
from app.pipeline.heartbeat import clear_heartbeat_if_own, start_heartbeat_thread
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.lifecycle import _WORKER_ID, finish_execution, now_utc, register_execution
from app.pipeline.stage_result import (
    enqueue_next,
    mark_failed,
)
from app.pipeline.stale_recovery import resume_stage
from app.pipeline.task_context import update_event_first_context
from app.pipeline.workers import (
    run_analyze,
    run_publish,
    run_render,
    run_transcribe,
)

if TYPE_CHECKING:
    from sqlmodel import Session


_waiting_counts: dict[str, tuple[dict[str, int], tuple[int, ...]]] = {}
_waiting_counts_lock = threading.Lock()


def _log_waiting_counts(stage: str, counts: dict[str, int], samples: tuple[int, ...] = ()) -> None:
    """只在等待原因或数量变化时记录汇总，保持轮询日志有界。"""
    import logging

    with _waiting_counts_lock:
        previous = _waiting_counts.get(stage, ({}, ()))
        if previous == (counts, samples):
            return
        _waiting_counts[stage] = (dict(counts), samples)
    level = logging.WARNING if counts.get("missing_event") else logging.DEBUG
    logging.getLogger(__name__).log(
        level, "scheduler_waiting stage=%s counts=%s sample_task_ids=%s", stage, dict(sorted(counts.items())), samples
    )


def room_cfg_from_task(task: SegmentTask, db: Session | None = None) -> dict[str, bool | float]:
    """从任务读取房间级自动化开关。

    :param task: SegmentTask 实例。
    :returns: 包含 auto_analyze/auto_render/auto_approve/auto_upload 等键的字典。
    """
    from app.db.entities import LiveRoom, RecordingSession

    if db is None:
        with get_session() as connection:
            return room_cfg_from_task(task, connection)
    session = db.get(RecordingSession, task.session_id)
    if session is None:
        return {"auto_analyze": False, "auto_render": False, "auto_approve": False, "auto_upload": False}
    room = db.get(LiveRoom, session.room_id) if session.room_id else None
    if room is None:
        return {"auto_analyze": False, "auto_render": False, "auto_approve": False, "auto_upload": False}
    return {
        "auto_analyze": bool(room.auto_analyze),
        "auto_render": bool(room.auto_render),
        "auto_approve": bool(room.auto_approve),
        "auto_upload": bool(room.auto_upload),
        "auto_approve_threshold": float(room.auto_approve_threshold),
        "auto_publish_threshold": float(room.auto_publish_threshold),
        "review_threshold": float(room.review_threshold),
    }


def advance_recorded() -> None:
    """先把 RECORDED 任务送入无 ASR 信号检测，再进入转写。"""
    waiting: Counter[str] = Counter()
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.RECORDED)).all()
        for task in tasks:
            seg = db.get(RawSegment, task.segment_id)
            if seg is not None and seg.status == OldStatus.RECORDED:
                cfg = room_cfg_from_task(task, db)
                if not cfg.get("auto_analyze", False):
                    waiting["auto_analyze=off"] += 1
                    continue
                session = db.get(RecordingSession, task.session_id)
                task.priority = (
                    settings.near_live_asr_priority
                    if session is not None
                    and session.status
                    in {
                        SessionStatus.STARTING,
                        SessionStatus.RECORDING,
                        SessionStatus.RECONNECTING,
                        SessionStatus.RECONNECTED,
                    }
                    else settings.background_asr_priority
                )
                task.context_json = update_event_first_context(
                    task.context_json,
                    analysis_pass="detect",
                    asr_mode="pending",
                    asr_evidence_state="pending",
                    attention_windows=[],
                )
                enqueue_next(task, TaskStatus.QUEUED_FOR_ANALYSIS)
                db.add(task)
    _log_waiting_counts(TaskStatus.RECORDED, waiting)


def advance_transcribed() -> None:
    """TRANSCRIBED → QUEUED_FOR_ANALYSIS，并为活动直播保留一个分段的后视窗口。"""
    waiting: Counter[str] = Counter()
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.TRANSCRIBED)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task, db)
            if not cfg.get("auto_analyze", False):
                waiting["auto_analyze=off"] += 1
                continue
            if not _analysis_lookahead_ready(db, task):
                waiting["lookahead"] += 1
                continue
            enqueue_next(task, TaskStatus.QUEUED_FOR_ANALYSIS)
            db.add(task)
    _log_waiting_counts(TaskStatus.TRANSCRIBED, waiting)


def _analysis_lookahead_ready(db: Session, task: SegmentTask) -> bool:
    """判断当前转写是否已具备跨断点分析所需的后续上下文。"""
    segment = db.get(RawSegment, task.segment_id)
    session = db.get(RecordingSession, task.session_id)
    if segment is None or session is None or segment.end_ts is None:
        return True
    following = db.exec(
        select(RawSegment)
        .where(
            RawSegment.session_id == task.session_id,
            RawSegment.seq > segment.seq,
        )
        .order_by(RawSegment.seq.asc())
    ).first()
    if following is not None:
        if following.start_ts is None:
            return True
        from app.analysis.timeline import datetime_epoch

        gap_s = datetime_epoch(following.start_ts) - datetime_epoch(segment.end_ts)
        if gap_s > 1.0:
            return True
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == following.id)).first()
        if transcript is not None:
            return True
        following_task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == following.id)).first()
        return following_task is not None and following_task.stage in {
            TaskStatus.TRANSCRIBED,
            TaskStatus.QUEUED_FOR_ANALYSIS,
            TaskStatus.ANALYZING,
            TaskStatus.CANDIDATE_CREATED,
            TaskStatus.AWAITING_REVIEW,
            TaskStatus.REVIEWED_WAITING_ACTION,
            TaskStatus.APPROVED,
            TaskStatus.APPROVED_WAITING_RENDER,
            TaskStatus.QUEUED_FOR_RENDER,
            TaskStatus.RENDERING,
            TaskStatus.RENDERED,
            TaskStatus.AWAITING_PUBLISH_CONFIRMATION,
            TaskStatus.QUEUED_FOR_PUBLISH,
            TaskStatus.PUBLISHING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    return session.status not in {
        SessionStatus.STARTING,
        SessionStatus.RECORDING,
        SessionStatus.RECONNECTING,
        SessionStatus.RECONNECTED,
    }


@media_reference_scope()
def advance_candidate() -> None:
    """CANDIDATE_CREATED → AWAITING_REVIEW 或自动批准。"""
    import logging

    _logger = logging.getLogger(__name__)
    from app.pipeline.approval import approve_event_and_task

    with get_session() as db:
        sessions = db.exec(
            select(SegmentTask.session_id)
            .join(RecordingSession, RecordingSession.id == SegmentTask.session_id)
            .join(LiveRoom, LiveRoom.id == RecordingSession.room_id)
            .outerjoin(HighlightCandidate, HighlightCandidate.id == SegmentTask.candidate_id)
            .where(
                SegmentTask.stage == TaskStatus.CANDIDATE_CREATED,
                LiveRoom.auto_approve.is_(True),
                func.coalesce(HighlightCandidate.highlight_score, 0.0) >= LiveRoom.auto_approve_threshold,
                SegmentTask.event_id.is_not(None),
            )
            .distinct()
        ).all()
    with media_validation_scope(session_ids=sessions), get_session(operation="advance_candidate") as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.CANDIDATE_CREATED)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task, db)
            auto_approve = bool(cfg.get("auto_approve", False))
            threshold = float(cfg.get("auto_approve_threshold", settings.highlight_auto_approve_threshold))

            if auto_approve:
                candidate = db.get(HighlightCandidate, task.candidate_id) if task.candidate_id else None
                score = candidate.highlight_score if candidate else 0.0
                if score >= threshold and task.event_id is not None:
                    ok = approve_event_and_task(
                        task_id=task.id,
                        event_id=task.event_id,
                        source="auto",
                        review_decision="approved_solo",
                        db=db,
                    )
                    if ok:
                        log_after_commit(
                            db,
                            partial(
                                _logger.info,
                                "auto_approve: task=%s candidate=%s event=%s score=%.2f",
                                task.id,
                                task.candidate_id,
                                task.event_id,
                                score,
                            ),
                        )
                        continue

            enqueue_next(task, TaskStatus.AWAITING_REVIEW)
            db.add(task)


@media_reference_scope()
def advance_awaiting_review() -> None:
    """AWAITING_REVIEW → 自动批准 (如果 auto_approve 开启且分数达标)。"""
    import logging

    _logger = logging.getLogger(__name__)
    from app.pipeline.approval import approve_event_and_task

    waiting: Counter[str] = Counter()
    missing_events: list[int] = []
    approved: list[tuple[int, int | None, int, float]] = []
    with get_session() as db:
        queue = (
            select(
                SegmentTask.id.label("task_id"),
                SegmentTask.session_id,
                SegmentTask.candidate_id,
                SegmentTask.event_id,
                func.coalesce(HighlightCandidate.highlight_score, 0.0).label("score"),
                LiveRoom.auto_approve,
                LiveRoom.auto_approve_threshold,
            )
            .select_from(SegmentTask)
            .outerjoin(RecordingSession, RecordingSession.id == SegmentTask.session_id)
            .outerjoin(LiveRoom, LiveRoom.id == RecordingSession.room_id)
            .outerjoin(HighlightCandidate, HighlightCandidate.id == SegmentTask.candidate_id)
            .where(SegmentTask.stage == TaskStatus.AWAITING_REVIEW)
            .subquery()
        )
        reason = case(
            (queue.c.auto_approve.is_not(True), "auto_approve=off"),
            (queue.c.score < queue.c.auto_approve_threshold, "below_threshold"),
            (queue.c.event_id.is_(None), "missing_event"),
            else_="ready",
        )
        classified = select(queue, reason.label("reason")).subquery()
        counts = dict(db.exec(select(classified.c.reason, func.count()).group_by(classified.c.reason)).all())
        waiting.update({key: count for key, count in counts.items() if key != "ready"})
        if counts.get("missing_event"):
            missing_events = list(
                db.exec(
                    select(classified.c.task_id)
                    .where(classified.c.reason == "missing_event")
                    .order_by(classified.c.task_id)
                    .limit(3)
                ).all()
            )
        ready = []
        if counts.get("ready"):
            ready = db.exec(
                select(
                    classified.c.task_id,
                    classified.c.candidate_id,
                    classified.c.event_id,
                    classified.c.score,
                    classified.c.session_id,
                )
                .where(classified.c.reason == "ready")
                .order_by(classified.c.task_id)
            ).all()
    if ready:
        with (
            media_validation_scope(session_ids=[row.session_id for row in ready]),
            get_session(operation="advance_review") as db,
        ):
            if db.get_bind().dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            current = db.exec(
                select(classified.c.task_id, classified.c.candidate_id, classified.c.event_id, classified.c.score)
                .where(classified.c.reason == "ready", classified.c.task_id.in_([row.task_id for row in ready]))
                .order_by(classified.c.task_id)
            ).all()
            for task_id, candidate_id, event_id, score in current:
                ok = approve_event_and_task(
                    task_id=task_id,
                    event_id=event_id,
                    source="auto",
                    review_decision="approved_solo",
                    db=db,
                )
                if ok:
                    approved.append((task_id, candidate_id, event_id, score))
    for task_id, candidate_id, event_id, score in approved:
        _logger.info("auto_approve: task=%s candidate=%s event=%s score=%.2f", task_id, candidate_id, event_id, score)
    _log_waiting_counts(TaskStatus.AWAITING_REVIEW, waiting, tuple(missing_events))


def advance_approved() -> None:
    """APPROVED → QUEUED_FOR_RENDER 或 APPROVED_WAITING_RENDER。"""
    import logging

    _logger = logging.getLogger(__name__)
    from app.pipeline.approval import assert_event_approved

    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.APPROVED)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task, db)
            auto_render = bool(cfg.get("auto_render", False))

            if auto_render:
                if task.event_id and not assert_event_approved(task.event_id, db):
                    _logger.error(
                        "consistency_error: task=%s event=%s review_status 非 APPROVED, 阻止进入渲染队列。",
                        task.id,
                        task.event_id,
                    )
                    task.last_error = f"data_consistency_error: event {task.event_id} not approved, cannot render"
                    db.add(task)
                    continue
                enqueue_next(task, TaskStatus.QUEUED_FOR_RENDER)
                log_after_commit(
                    db,
                    partial(
                        _logger.info,
                        "auto_render: task=%s candidate=%s -> queued_for_render",
                        task.id,
                        task.candidate_id,
                    ),
                )
            else:
                enqueue_next(task, TaskStatus.APPROVED_WAITING_RENDER)
                log_after_commit(
                    db, partial(_logger.info, "auto_render=off: task=%s -> approved_waiting_render", task.id)
                )
            db.add(task)


def advance_rendered() -> None:
    """推进 RENDERED 阶段任务到 QUEUED_FOR_PUBLISH 或 AWAITING_PUBLISH_CONFIRMATION。"""
    import logging

    from app.core.settings_store import auto_upload_enabled, biliup_enabled

    _logger = logging.getLogger(__name__)
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.RENDERED)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task, db)
            auto_upload = auto_upload_enabled() and biliup_enabled() and bool(cfg.get("auto_upload", False))
            candidate = db.get(HighlightCandidate, task.candidate_id) if task.candidate_id else None
            meets_threshold = candidate is not None and candidate.highlight_score >= float(
                cfg.get("auto_publish_threshold", settings.auto_publish_threshold)
            )

            if auto_upload and meets_threshold:
                enqueue_next(task, TaskStatus.QUEUED_FOR_PUBLISH)
                log_after_commit(
                    db,
                    partial(_logger.info, "auto_upload: task=%s clip=%s -> queued_for_publish", task.id, task.clip_id),
                )
            else:
                enqueue_next(task, TaskStatus.AWAITING_PUBLISH_CONFIRMATION)
                log_after_commit(
                    db,
                    partial(
                        _logger.info,
                        "automatic publish not eligible: task=%s -> awaiting_publish_confirmation",
                        task.id,
                    ),
                )
            db.add(task)


def _continue_without_asr(task: SegmentTask, reason: str) -> None:
    """保留明确不可用的 ASR 证据，让非语义热点分析继续。"""
    task.context_json = update_event_first_context(
        task.context_json,
        analysis_pass="candidate",
        asr_mode="unavailable",
        asr_evidence_state="unavailable",
        asr_unavailable_reason=reason[:500],
    )
    enqueue_next(task, TaskStatus.TRANSCRIBED)


def retry_expired() -> None:
    """TRANSIENT_FAILED 任务超时后重新入队 (或永久失败)。"""
    import logging

    _logger = logging.getLogger(__name__)
    now = now_utc()
    from app.pipeline.stage_result import make_stage_key, release_lease

    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        tasks = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage == TaskStatus.TRANSIENT_FAILED,
                SegmentTask.next_retry_at <= now,
            )
        ).all()
        for task in tasks:
            if task.attempts >= task.max_retries:
                if task.failed_stage == TaskStatus.TRANSCRIBING:
                    reason = task.last_error or "ASR 重试次数超限"
                    _continue_without_asr(task, reason)
                    _logger.warning(
                        "任务 %s ASR 已耗尽重试，按 unavailable 证据继续热点分析",
                        task.id,
                    )
                else:
                    task.stage = TaskStatus.FAILED
                    task.last_error = task.last_error or "重试次数超限"
                    task.completed_at = now
            else:
                res = resume_stage(task.failed_stage)
                task.stage = res
                task.next_retry_at = None
                log_after_commit(db, partial(_logger.info, "任务 %s 重试: %s -> %s", task.id, task.failed_stage, res))
            release_lease(task)
            task.stage_key = make_stage_key(task.segment_id, task.stage)
            db.add(task)


@configured_entry
def execute_task(task_id: int, active_stage_val: str, lease_token: str | None = None) -> None:
    """执行耗时任务, 传递 lease_token 用于条件提交。

    流程:
    1. 启动心跳线程
    2. 构建 TaskLease
    3. 分发到对应阶段 Worker (run_transcribe/analyze/render/publish)
    4. 处理 LeaseLostError 和通用异常
    5. 清理心跳和资源

    :param task_id: SegmentTask ID。
    :param active_stage_val: 活跃阶段 (如 TRANSCRIBING)。
    :param lease_token: 租约令牌。
    """
    import logging

    _logger = logging.getLogger(__name__)

    t0 = _time_mod.time()
    hb_stop: threading.Event | None = None
    lease: TaskLease | None = None
    try:
        hb_stop = start_heartbeat_thread(task_id, lease_token, active_stage_val)
        lease = TaskLease(
            task_id=task_id, worker_id=_WORKER_ID, lease_token=lease_token, expected_stage=active_stage_val
        )
        register_execution(lease.task_id, lease.lease_token)

        def cancelled() -> bool:
            with get_session() as db:
                return not still_owns_lease(db, lease)

        with resource_scope(), cancellation_scope(cancelled, poll_interval_s=0.5):
            if active_stage_val == TaskStatus.TRANSCRIBING:
                run_transcribe(lease)
            elif active_stage_val == TaskStatus.ANALYZING:
                run_analyze(lease)
            elif active_stage_val == TaskStatus.RENDERING:
                run_render(lease)
            elif active_stage_val == TaskStatus.PUBLISHING:
                run_publish(lease)
    except (ASRShutdownError, ProcessShutdownError, OperationShutdownError):
        with get_session() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            task = db.get(SegmentTask, task_id)
            if task is not None and lease is not None and still_owns_lease(db, lease):
                attempts = task.attempts
                enqueue_next(task, resume_stage(active_stage_val))
                # 服务关闭不增加失败次数，也不能清零先前已有的重试计数。
                task.attempts = attempts
                db.add(task)
        _logger.info(
            "execution_shutdown_requeued task=%s stage=%s worker=%s lease=%s",
            task_id,
            active_stage_val,
            _WORKER_ID,
            lease_token,
        )
    except (LeaseLostError, OperationCancelledError):
        with get_session() as db:
            task = db.get(SegmentTask, task_id)
            cancelling = (
                task is not None
                and task.stage == TaskStatus.CANCELLING
                and task.claimed_by == _WORKER_ID
                and task.lease_token == lease_token
            )
        log = _logger.info if cancelling else _logger.warning
        log(
            "%s: task=%s stage=%s worker=%s lease=%s",
            "execution_cancel_requested" if cancelling else "lease_lost_during_execution",
            task_id,
            active_stage_val,
            _WORKER_ID,
            lease_token,
        )
    except Exception as exc:
        _ms = int((_time_mod.time() - t0) * 1000)
        _logger.exception("任务 %s 阶段 %s 失败: %s", task_id, active_stage_val, safe_exception_summary(exc))
        with get_session() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            t = db.get(SegmentTask, task_id)
            if t is not None and lease is not None and still_owns_lease(db, lease):
                from app.pipeline.recovery_budget import consume_recovery, has_saved_asr

                crash_exhausted = isinstance(exc, ASRProcessExitError) and consume_recovery(
                    db, t, safe_exception_summary(exc)
                )
                if crash_exhausted and has_saved_asr(db, t):
                    mark_failed(t, "连续崩溃恢复预算耗尽；已保存原始 ASR 断点，等待显式重试", permanent=True)
                elif active_stage_val == TaskStatus.TRANSCRIBING and (
                    crash_exhausted
                    or permanent_device_error(exc)
                    or isinstance(exc, RemoteASRError)
                    and not exc.retryable
                ):
                    _continue_without_asr(t, safe_exception_summary(exc))
                    _logger.warning("任务 %s ASR 设备确定不可用，按 unavailable 证据继续热点分析", task_id)
                else:
                    mark_failed(t, safe_exception_summary(exc), permanent=False)
                db.add(t)
            elif t is not None:
                _logger.warning("stale_result_discarded: task=%s 已失去租约, 丢弃失败结果", task_id)
    finally:
        if hb_stop is not None:
            hb_stop.set()
        clear_heartbeat_if_own(task_id, lease_token)
        # V0.1.13: Release tracked resources
        from app.pipeline.lifecycle import _task_resources, _task_resources_lock

        if _task_resources_lock is None:
            _task_resources_lock = threading.Lock()
        with _task_resources_lock:
            task_cost = _task_resources.pop((task_id, lease_token), None)
        if task_cost:
            from app.core.resource_budget import release_resources

            release_resources(**task_cost)
        if lease is not None:
            finish_execution(lease.task_id, lease.lease_token)
            settle_cancellation(lease)


def settle_cancellation(lease: TaskLease) -> None:
    """仅由原租约在执行和资源清理结束后结算取消，不覆盖新所有者。"""
    import logging

    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        task = db.get(SegmentTask, lease.task_id)
        if (
            task is None
            or task.stage != TaskStatus.CANCELLING
            or task.claimed_by != lease.worker_id
            or task.lease_token != lease.lease_token
        ):
            return
        cleanup_pending = False
        if lease.expected_stage == TaskStatus.TRANSCRIBING:
            from app.analysis.transcription.process_pool import get_process_pool

            cleanup_pending = get_process_pool().has_active_task(lease.task_id, lease.lease_token)
        if cleanup_pending:
            task.last_error = "取消仍在等待模型进程与 IPC 资源清理"
            db.add(task)
            logging.getLogger(__name__).error("cancellation_cleanup_pending task=%s", lease.task_id)
            return
        enqueue_next(task, TaskStatus.CANCELLED)
        db.add(task)
    logging.getLogger(__name__).info("execution_cancellation_settled task=%s worker=%s", lease.task_id, lease.worker_id)
