"""任务调度器 — 阶段推进函数、重试、任务执行。"""

from __future__ import annotations

import threading
import time as _time_mod
from typing import TYPE_CHECKING

from sqlmodel import select

from app.db.models import (
    HighlightCandidate,
    RawSegment,
    SegmentTask,
    TaskStatus,
)
from app.db.models import SegmentStatus as OldStatus
from app.db.session import get_session
from app.pipeline.heartbeat import clear_heartbeat_if_own, start_heartbeat_thread
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.lifecycle import _WORKER_ID, now_utc
from app.pipeline.stage_result import (
    enqueue_next,
    mark_failed,
)
from app.pipeline.stale_recovery import resume_stage
from app.pipeline.workers import (
    run_analyze,
    run_publish,
    run_render,
    run_transcribe,
)

if TYPE_CHECKING:
    pass


def room_cfg_from_task(task: SegmentTask) -> dict[str, bool | float]:
    """从任务读取房间级自动化开关。

    :param task: SegmentTask 实例。
    :returns: 包含 auto_analyze/auto_render/auto_approve/auto_upload 等键的字典。
    """
    from app.db.models import LiveRoom, RecordingSession

    with get_session() as db:
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
            "review_threshold": float(room.review_threshold),
        }


def advance_recorded() -> None:
    """推进 RECORDED 阶段任务到 QUEUED_FOR_TRANS (如果 auto_analyze 开启)。"""
    import logging

    _logger = logging.getLogger(__name__)
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.RECORDED)).all()
        for task in tasks:
            seg = db.get(RawSegment, task.segment_id)
            if seg is not None and seg.status == OldStatus.RECORDED:
                cfg = room_cfg_from_task(task)
                if not cfg.get("auto_analyze", False):
                    _logger.debug("auto_analyze=off, 片段 %s 不自动进入转写队列", task.segment_id)
                    continue
                enqueue_next(task, TaskStatus.QUEUED_FOR_TRANS)
                db.add(task)


def advance_transcribed() -> None:
    """TRANSCRIBED → QUEUED_FOR_ANALYSIS (如果 auto_analyze 开启)。"""
    import logging

    _logger = logging.getLogger(__name__)
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.TRANSCRIBED)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task)
            if not cfg.get("auto_analyze", False):
                _logger.debug("auto_analyze=off, 片段 %s 不自动创建分析任务", task.segment_id)
                continue
            enqueue_next(task, TaskStatus.QUEUED_FOR_ANALYSIS)
            db.add(task)


def advance_candidate() -> None:
    """CANDIDATE_CREATED → AWAITING_REVIEW 或自动批准。"""
    import logging

    _logger = logging.getLogger(__name__)
    from app.pipeline.approval import approve_event_and_task

    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.CANDIDATE_CREATED)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task)
            auto_approve = bool(cfg.get("auto_approve", False))
            threshold = float(cfg.get("auto_approve_threshold", 0.82))

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
                        _logger.info(
                            "auto_approve: task=%s candidate=%s event=%s score=%.2f",
                            task.id,
                            task.candidate_id,
                            task.event_id,
                            score,
                        )
                        continue

            enqueue_next(task, TaskStatus.AWAITING_REVIEW)
            db.add(task)


def advance_awaiting_review() -> None:
    """AWAITING_REVIEW → 自动批准 (如果 auto_approve 开启且分数达标)。"""
    import logging

    _logger = logging.getLogger(__name__)
    from app.pipeline.approval import approve_event_and_task

    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.AWAITING_REVIEW)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task)
            auto_approve = bool(cfg.get("auto_approve", False))
            threshold = float(cfg.get("auto_approve_threshold", 0.82))

            if not auto_approve:
                _logger.debug("auto_approve=off, task %s 留在 awaiting_review", task.id)
                continue

            candidate = db.get(HighlightCandidate, task.candidate_id) if task.candidate_id else None
            score = candidate.highlight_score if candidate else 0.0
            if score < threshold:
                _logger.debug("候选 %s 分数 %.2f < 阈值 %.2f, 不自动批准", task.candidate_id, score, threshold)
                continue
            if task.event_id is None:
                _logger.warning("task %s 缺少 event_id, 无法自动批准", task.id)
                continue

            ok = approve_event_and_task(
                task_id=task.id,
                event_id=task.event_id,
                source="auto",
                review_decision="approved_solo",
                db=db,
            )
            if ok:
                _logger.info(
                    "auto_approve: task=%s candidate=%s event=%s score=%.2f",
                    task.id,
                    task.candidate_id,
                    task.event_id,
                    score,
                )


def advance_approved() -> None:
    """APPROVED → QUEUED_FOR_RENDER 或 APPROVED_WAITING_RENDER。"""
    import logging

    _logger = logging.getLogger(__name__)
    from app.pipeline.approval import assert_event_approved

    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.APPROVED)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task)
            auto_render = bool(cfg.get("auto_render", False))

            if auto_render:
                if task.event_id and not assert_event_approved(task.event_id):
                    _logger.error(
                        "consistency_error: task=%s event=%s review_status 非 APPROVED, 阻止进入渲染队列。",
                        task.id,
                        task.event_id,
                    )
                    task.last_error = f"data_consistency_error: event {task.event_id} not approved, cannot render"
                    db.add(task)
                    continue
                enqueue_next(task, TaskStatus.QUEUED_FOR_RENDER)
                _logger.info("auto_render: task=%s candidate=%s -> queued_for_render", task.id, task.candidate_id)
            else:
                enqueue_next(task, TaskStatus.APPROVED_WAITING_RENDER)
                _logger.info("auto_render=off: task=%s -> approved_waiting_render", task.id)
            db.add(task)


def advance_rendered() -> None:
    """推进 RENDERED 阶段任务到 QUEUED_FOR_PUBLISH 或 AWAITING_PUBLISH_CONFIRMATION。"""
    import logging

    _logger = logging.getLogger(__name__)
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.RENDERED)).all()
        for task in tasks:
            cfg = room_cfg_from_task(task)
            auto_upload = bool(cfg.get("auto_upload", False))

            if auto_upload:
                enqueue_next(task, TaskStatus.QUEUED_FOR_PUBLISH)
                _logger.info("auto_upload: task=%s clip=%s -> queued_for_publish", task.id, task.clip_id)
            else:
                enqueue_next(task, TaskStatus.AWAITING_PUBLISH_CONFIRMATION)
                _logger.info("auto_upload=off: task=%s -> awaiting_publish_confirmation", task.id)
            db.add(task)


def retry_expired() -> None:
    """TRANSIENT_FAILED 任务超时后重新入队 (或永久失败)。"""
    import logging

    _logger = logging.getLogger(__name__)
    now = now_utc()
    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage == TaskStatus.TRANSIENT_FAILED,
                SegmentTask.next_retry_at <= now,
            )
        ).all()
        for task in tasks:
            if task.attempts >= task.max_retries:
                task.stage = TaskStatus.FAILED
                task.last_error = task.last_error or "重试次数超限"
                task.completed_at = now
            else:
                res = resume_stage(task.failed_stage)
                task.stage = res
                task.next_retry_at = None
                task.claimed_by = None
                task.claimed_at = None
                _logger.info("任务 %s 重试: %s -> %s", task.id, task.failed_stage, res)
            db.add(task)


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

        if active_stage_val == TaskStatus.TRANSCRIBING:
            run_transcribe(lease)
        elif active_stage_val == TaskStatus.ANALYZING:
            run_analyze(lease)
        elif active_stage_val == TaskStatus.RENDERING:
            run_render(lease)
        elif active_stage_val == TaskStatus.PUBLISHING:
            run_publish(lease)
    except LeaseLostError:
        _logger.warning("lease_lost_during_execution: task=%s", task_id)
    except Exception as exc:
        _ms = int((_time_mod.time() - t0) * 1000)
        _logger.error("任务 %s 阶段 %s 失败: %s", task_id, active_stage_val, exc)
        with get_session() as db:
            t = db.get(SegmentTask, task_id)
            if t is not None and lease is not None and still_owns_lease(db, lease):
                mark_failed(t, f"{type(exc).__name__}: {exc}", permanent=False)
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
            task_cost = _task_resources.pop(task_id, None)
        if task_cost:
            from app.core.resource_budget import release_resources

            release_resources(**task_cost)
