"""持久化任务队列 Worker (V0.1.14-alpha 模块拆分)。

V0.1.14: 核心逻辑已按职责拆分到 app.pipeline.* 子模块:
  - stage_result.py  — 状态转换、幂等键、任务标记函数
  - lease.py         — TaskLease / LeaseLostError / still_owns_lease
  - workers/         — 各阶段 compute / commit / run 实现

Worker 主循环、调度、并发槽管理、ResourceBudget 接入保持在本文件。
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import text as sa_text
from sqlmodel import select

from app.db.models import (
    RawSegment,
    SegmentTask,
    TaskStatus,
)
from app.db.models import (
    SegmentStatus as OldStatus,
)
from app.db.session import get_session
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import (
    active_stage,
    enqueue_next,
    mark_failed,
    mark_heartbeat,
)
from app.pipeline.workers import (
    run_analyze,
    run_publish,
    run_render,
    run_transcribe,
)

# ── 并发配置 ──────────────────────────────────────────────────────
MAX_TRANSCRIBING = int(os.environ.get("MAX_TRANSCRIBING", "1"))
MAX_ANALYZING = int(os.environ.get("MAX_ANALYZING", "2"))
MAX_RENDERING = int(os.environ.get("MAX_RENDERING", "2"))
MAX_PUBLISHING = int(os.environ.get("MAX_PUBLISHING", "1"))

_WORKER_SHUTDOWN_TIMEOUT_S = int(os.environ.get("WORKER_SHUTDOWN_TIMEOUT_SECONDS", "30"))

_RETRY_BASE_S = 10
_RETRY_MAX_S = 600
_RETRY_JITTER_S = 5

_HEARTBEAT_INTERVAL_S = 30
_STALE_TIMEOUT_S = 120
_HEARTBEAT_POLL_S = 5

_WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
_logger = logger

# V0.1.13: Task-level resource tracking (ResourceBudget integration)
_task_resources: dict[int, dict[str, int | float]] = {}
_task_resources_lock: threading.Lock | None = None

# V0.1.12.2: Worker 生命周期
_shutting_down: bool = False
# V0.1.12.4: 子进程跟踪
_subprocesses: list = []
_subprocesses_lock: threading.Lock | None = None


# ── 全局单例 Worker ─────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _jitter(base: float, jitter_s: float = _RETRY_JITTER_S) -> float:
    return base + random.uniform(0, jitter_s)


def _resume_stage(failed_stage: str | None) -> str:
    if failed_stage is None:
        return TaskStatus.QUEUED_FOR_TRANS
    mapping = {
        TaskStatus.TRANSCRIBING: TaskStatus.QUEUED_FOR_TRANS,
        TaskStatus.ANALYZING: TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.RENDERING: TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.PUBLISHING: TaskStatus.QUEUED_FOR_PUBLISH,
        TaskStatus.TRANSCRIBED: TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.CANDIDATE_CREATED: TaskStatus.QUEUED_FOR_RENDER,
    }
    return mapping.get(failed_stage, TaskStatus.QUEUED_FOR_TRANS)


# ═══════════════════════════════════════════════════
# 子进程跟踪 (lifecycle)
# ═══════════════════════════════════════════════════


def track_subprocess(proc) -> None:
    """注册子进程句柄, 供关闭时统一 terminate/kill。"""
    global _subprocesses_lock
    if _subprocesses_lock is None:
        _subprocesses_lock = threading.Lock()
    with _subprocesses_lock:
        _subprocesses.append(proc)


def untrack_subprocess(proc) -> None:
    """从跟踪集中移除已正常结束的子进程。"""
    global _subprocesses_lock
    if _subprocesses_lock is None:
        return
    with _subprocesses_lock:
        try:
            _subprocesses.remove(proc)
        except ValueError:
            pass


def _cleanup_subprocesses() -> None:
    """关闭所有被跟踪的子进程: SIGTERM → 等待 → SIGKILL。"""
    import time as _time

    global _subprocesses_lock
    if _subprocesses_lock is None:
        return
    with _subprocesses_lock:
        procs = list(_subprocesses)
        _subprocesses.clear()
    if not procs:
        return
    _logger.warning("清理 {} 个子进程 (SIGTERM)", len(procs))
    for p in procs:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    _time.sleep(5)
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                _logger.warning("子进程 {} 已被 SIGKILL", p.pid)
        except Exception:
            pass


# ═══════════════════════════════════════════════════
# 任务生命周期
# ═══════════════════════════════════════════════════


def create_task(segment_id: int, session_id: int) -> SegmentTask | None:
    """为已完成录制的片段创建任务(幂等)。"""
    from sqlalchemy.exc import IntegrityError

    from app.pipeline.stage_result import make_idempotency_key, make_pipeline_key, make_stage_key

    pipeline_key = make_pipeline_key(segment_id)
    stage_key = make_stage_key(segment_id, "recorded")
    with get_session() as db:
        existing = db.exec(select(SegmentTask).where(SegmentTask.pipeline_key == pipeline_key)).first()
        if existing is not None:
            _logger.debug("pipeline_key 已存在: segment={} task={} stage={}", segment_id, existing.id, existing.stage)
            return None
        old_key = make_idempotency_key(segment_id, "recorded")
        existing_old = db.exec(select(SegmentTask).where(SegmentTask.idempotency_key == old_key)).first()
        if existing_old is not None:
            existing_old.pipeline_key = pipeline_key
            db.add(existing_old)
            _logger.info("后向兼容: 为旧任务 {} 补充 pipeline_key", existing_old.id)
            return None

        task = SegmentTask(
            segment_id=segment_id,
            session_id=session_id,
            stage=TaskStatus.RECORDED,
            pipeline_key=pipeline_key,
            stage_key=stage_key,
            idempotency_key=old_key,
        )
        db.add(task)
        try:
            db.flush()
            db.refresh(task)
            return task
        except IntegrityError:
            db.rollback()
            _logger.info("idempotency_conflict_resolved: segment={} 任务已被并发创建", segment_id)
            return None


# ═══════════════════════════════════════════════════
# 原子领取 (claiming)
# ═══════════════════════════════════════════════════


def _pop_and_claim(queued_stage: str) -> SegmentTask | None:
    """原子领取: 条件 UPDATE + 行数校验 + lease_token。"""
    now = _now()
    act_stage = active_stage(queued_stage)
    lease_token = uuid.uuid4().hex
    with get_session() as db:
        candidate = db.exec(
            select(SegmentTask.id, SegmentTask.segment_id)
            .where(
                SegmentTask.stage == queued_stage,
                (SegmentTask.next_retry_at.is_(None)) | (SegmentTask.next_retry_at <= now),
            )
            .order_by(SegmentTask.priority.asc(), SegmentTask.created_at.asc())
            .limit(1)
        ).first()

        if candidate is None:
            return None

        task_id, segment_id = candidate

        result = db.exec(
            sa_text(
                """UPDATE segment_tasks
               SET stage = :active,
                   claimed_by = :worker_id,
                   claimed_at = :now,
                   heartbeat_at = :now,
                   lease_token = :lease_token,
                   attempts = attempts + 1,
                   started_at = :now,
                   last_error = NULL
               WHERE id = :task_id
                 AND stage = :queued_stage"""
            ),
            params={
                "active": act_stage,
                "worker_id": _WORKER_ID,
                "now": now.isoformat(),
                "lease_token": lease_token,
                "task_id": task_id,
                "queued_stage": queued_stage,
            },
        )

        if result.rowcount != 1:
            _logger.info("原子领取失败 task_id={} 已被其他 Worker 抢占", task_id)
            return None

        task = db.get(SegmentTask, task_id)
        if task is None:
            return None

        _logger.info(
            "原子领取成功 task_id={} stage={} segment={} worker={} lease={}",
            task_id,
            act_stage,
            segment_id,
            _WORKER_ID,
            lease_token[:12],
        )
        return task


# ═══════════════════════════════════════════════════
# 阶段推进
# ═══════════════════════════════════════════════════


def _room_cfg_from_task(task: SegmentTask) -> dict:
    """从任务读取房间级自动化开关。"""
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


def _advance_recorded() -> None:
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.RECORDED)).all()
        for task in tasks:
            seg = db.get(RawSegment, task.segment_id)
            if seg is not None and seg.status == OldStatus.RECORDED:
                cfg = _room_cfg_from_task(task)
                if not cfg.get("auto_analyze", False):
                    _logger.debug("auto_analyze=off, 片段 {} 不自动进入转写队列", task.segment_id)
                    continue
                enqueue_next(task, TaskStatus.QUEUED_FOR_TRANS)
                db.add(task)


def _advance_transcribed() -> None:
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.TRANSCRIBED)).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            if not cfg.get("auto_analyze", False):
                _logger.debug("auto_analyze=off, 片段 {} 不自动创建分析任务", task.segment_id)
                continue
            enqueue_next(task, TaskStatus.QUEUED_FOR_ANALYSIS)
            db.add(task)


def _advance_candidate() -> None:
    from app.db.models import HighlightCandidate
    from app.pipeline.approval import approve_event_and_task

    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.CANDIDATE_CREATED)).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
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
                            "auto_approve: task={} candidate={} event={} score={:.2f}",
                            task.id,
                            task.candidate_id,
                            task.event_id,
                            score,
                        )
                        continue

            enqueue_next(task, TaskStatus.AWAITING_REVIEW)
            db.add(task)


def _advance_awaiting_review() -> None:
    from app.db.models import HighlightCandidate
    from app.pipeline.approval import approve_event_and_task

    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.AWAITING_REVIEW)).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            auto_approve = bool(cfg.get("auto_approve", False))
            threshold = float(cfg.get("auto_approve_threshold", 0.82))

            if not auto_approve:
                _logger.debug("auto_approve=off, task {} 留在 awaiting_review", task.id)
                continue

            candidate = db.get(HighlightCandidate, task.candidate_id) if task.candidate_id else None
            score = candidate.highlight_score if candidate else 0.0
            if score < threshold:
                _logger.debug("候选 {} 分数 {:.2f} < 阈值 {:.2f}, 不自动批准", task.candidate_id, score, threshold)
                continue

            if task.event_id is None:
                _logger.warning("task {} 缺少 event_id, 无法自动批准", task.id)
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
                    "auto_approve: task={} candidate={} event={} score={:.2f}",
                    task.id,
                    task.candidate_id,
                    task.event_id,
                    score,
                )


def _advance_approved() -> None:
    from app.pipeline.approval import assert_event_approved

    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.APPROVED)).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            auto_render = bool(cfg.get("auto_render", False))

            if auto_render:
                if task.event_id and not assert_event_approved(task.event_id):
                    _logger.error(
                        "consistency_error: task={} event={} review_status 非 APPROVED, 阻止进入渲染队列。",
                        task.id,
                        task.event_id,
                    )
                    task.last_error = f"data_consistency_error: event {task.event_id} not approved, cannot render"
                    db.add(task)
                    continue
                enqueue_next(task, TaskStatus.QUEUED_FOR_RENDER)
                _logger.info("auto_render: task={} candidate={} -> queued_for_render", task.id, task.candidate_id)
            else:
                enqueue_next(task, TaskStatus.APPROVED_WAITING_RENDER)
                _logger.info("auto_render=off: task={} -> approved_waiting_render", task.id)
            db.add(task)


def _advance_rendered() -> None:
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.RENDERED)).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            auto_upload = bool(cfg.get("auto_upload", False))

            if auto_upload:
                enqueue_next(task, TaskStatus.QUEUED_FOR_PUBLISH)
                _logger.info("auto_upload: task={} clip={} -> queued_for_publish", task.id, task.clip_id)
            else:
                enqueue_next(task, TaskStatus.AWAITING_PUBLISH_CONFIRMATION)
                _logger.info("auto_upload=off: task={} -> awaiting_publish_confirmation", task.id)
            db.add(task)


# ═══════════════════════════════════════════════════
# 重试
# ═══════════════════════════════════════════════════


def _retry_expired() -> None:
    now = _now()
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
                resume = _resume_stage(task.failed_stage)
                task.stage = resume
                task.next_retry_at = None
                task.claimed_by = None
                task.claimed_at = None
                _logger.info("任务 {} 重试: {} -> {}", task.id, task.failed_stage, resume)
            db.add(task)


def retry_task(task_id: int) -> bool:
    """手动/自动重试统一入口:从 failed_stage 恢复。"""
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return False
        if task.stage not in (TaskStatus.FAILED, TaskStatus.TRANSIENT_FAILED):
            return False
        resume = _resume_stage(task.failed_stage)
        task.stage = resume
        task.attempts = 0
        task.last_error = None
        task.error_is_permanent = False
        task.next_retry_at = None
        task.claimed_by = None
        task.claimed_at = None
        task.heartbeat_at = None
        _logger.info("任务 {} 手动重试: {} -> {}", task_id, task.failed_stage, resume)
        db.add(task)
        return True


def cancel_task(task_id: int) -> bool:
    """取消指定任务。"""
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return False
        enqueue_next(task, TaskStatus.CANCELLED)
        db.add(task)
        return True


# ═══════════════════════════════════════════════════
# 心跳 + Stale 恢复
# ═══════════════════════════════════════════════════


def _recover_stale() -> None:
    stale_threshold = _now() - timedelta(seconds=_STALE_TIMEOUT_S)
    with get_session() as db:
        stale = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_(
                    [
                        TaskStatus.TRANSCRIBING,
                        TaskStatus.ANALYZING,
                        TaskStatus.RENDERING,
                        TaskStatus.PUBLISHING,
                    ]
                ),
                SegmentTask.heartbeat_at.is_not(None),
                SegmentTask.heartbeat_at < stale_threshold,
            )
        ).all()
        for task in stale:
            resume = _resume_stage(task.failed_stage or task.stage)
            task.stage = resume
            task.claimed_by = None
            task.claimed_at = None
            task.heartbeat_at = None
            task.lease_token = None
            task.next_retry_at = None
            db.add(task)
        if stale:
            _logger.warning("Stale 恢复:回退 {} 个心跳超时任务。", len(stale))


def _recover_orphans() -> None:
    from app.pipeline.stage_result import make_idempotency_key, make_pipeline_key, make_stage_key

    _recover_stale()
    with get_session() as db:
        stuck = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_(
                    [
                        TaskStatus.TRANSCRIBING,
                        TaskStatus.ANALYZING,
                        TaskStatus.RENDERING,
                        TaskStatus.PUBLISHING,
                    ]
                ),
                SegmentTask.heartbeat_at.is_(None),
            )
        ).all()
        for task in stuck:
            resume = _resume_stage(task.stage)
            task.stage = resume
            task.started_at = None
            task.next_retry_at = None
            task.claimed_by = None
            db.add(task)
        if stuck:
            _logger.info("恢复:回退 {} 个旧格式中间状态任务。", len(stuck))

        existing_ids = {t.segment_id for t in db.exec(select(SegmentTask.segment_id)).all()}
        orphan_segs = db.exec(
            select(RawSegment).where(
                RawSegment.status == OldStatus.RECORDED,
                ~RawSegment.id.in_(existing_ids) if existing_ids else True,
            )
        ).all()
        for seg in orphan_segs:
            pipeline_key = make_pipeline_key(seg.id)
            stage_key = make_stage_key(seg.id, "recorded")
            t = SegmentTask(
                segment_id=seg.id,
                session_id=seg.session_id,
                stage=TaskStatus.RECORDED,
                pipeline_key=pipeline_key,
                stage_key=stage_key,
                idempotency_key=make_idempotency_key(seg.id, "recorded"),
            )
            db.add(t)
        if orphan_segs:
            _logger.info("恢复:为 {} 个孤立片段创建任务。", len(orphan_segs))


# ═══════════════════════════════════════════════════
# 心跳线程
# ═══════════════════════════════════════════════════


def _start_heartbeat_thread(
    task_id: int,
    lease_token: str | None = None,
    expected_stage: str | None = None,
) -> threading.Event:
    """启动后台心跳线程, 使用租约条件更新 heartbeat_at。"""
    stop = threading.Event()

    def _beat() -> None:
        while not stop.is_set() and not _shutting_down:
            try:
                with get_session() as db:
                    if lease_token and expected_stage:
                        result = db.exec(
                            sa_text(
                                """UPDATE segment_tasks
                                   SET heartbeat_at = :now
                                   WHERE id = :task_id
                                     AND claimed_by = :worker_id
                                     AND lease_token = :lease_token
                                     AND stage = :expected_stage"""
                            ),
                            params={
                                "now": _now().isoformat(),
                                "task_id": task_id,
                                "worker_id": _WORKER_ID,
                                "lease_token": lease_token,
                                "expected_stage": expected_stage,
                            },
                        )
                        if result.rowcount == 0:
                            _logger.warning("lease_lost: task={} 心跳更新失败, 租约已被接管", task_id)
                            break
                    elif lease_token:
                        result = db.exec(
                            sa_text(
                                """UPDATE segment_tasks
                                   SET heartbeat_at = :now
                                   WHERE id = :task_id
                                     AND claimed_by = :worker_id
                                     AND lease_token = :lease_token"""
                            ),
                            params={
                                "now": _now().isoformat(),
                                "task_id": task_id,
                                "worker_id": _WORKER_ID,
                                "lease_token": lease_token,
                            },
                        )
                        if result.rowcount == 0:
                            _logger.warning("lease_lost: task={} 心跳更新失败, 租约已被接管", task_id)
                            break
                    else:
                        t = db.get(SegmentTask, task_id)
                        if t is not None:
                            mark_heartbeat(t)
                            db.add(t)
            except Exception:
                pass
            stop.wait(_HEARTBEAT_POLL_S)

    t = threading.Thread(target=_beat, daemon=True, name=f"hb-{task_id}")
    t.start()
    return stop


def _clear_heartbeat_if_own(task_id: int, lease_token: str | None = None) -> None:
    """条件清除 heartbeat, 必须携带租约。"""
    try:
        with get_session() as db:
            if lease_token:
                result = db.exec(
                    sa_text(
                        """UPDATE segment_tasks
                           SET heartbeat_at = NULL
                           WHERE id = :task_id
                             AND claimed_by = :worker_id
                             AND lease_token = :lease_token"""
                    ),
                    params={
                        "task_id": task_id,
                        "worker_id": _WORKER_ID,
                        "lease_token": lease_token,
                    },
                )
                if result.rowcount == 0:
                    _logger.debug("_clear_heartbeat_if_own: task={} 租约已转移, 跳过清除", task_id)
                    return
                _logger.debug("_clear_heartbeat_if_own: task={} heartbeat 已清除", task_id)
            else:
                t = db.get(SegmentTask, task_id)
                if t is not None and t.stage not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    t.heartbeat_at = None
                    db.add(t)
    except Exception:
        pass


# ═══════════════════════════════════════════════════
# 任务执行
# ═══════════════════════════════════════════════════


def _execute_task(task_id: int, active_stage_val: str, lease_token: str | None = None) -> None:
    """执行耗时任务, 传递 lease_token 用于条件提交。"""
    t0 = time.time()
    hb_stop: threading.Event | None = None
    lease: TaskLease | None = None
    try:
        hb_stop = _start_heartbeat_thread(task_id, lease_token, active_stage_val)
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
        _logger.warning("lease_lost_during_execution: task={}", task_id)
    except Exception as exc:
        _ms = int((time.time() - t0) * 1000)
        _logger.error("任务 {} 阶段 {} 失败: {}", task_id, active_stage_val, exc)
        with get_session() as db:
            t = db.get(SegmentTask, task_id)
            if t is not None and lease is not None and still_owns_lease(db, lease):
                mark_failed(t, f"{type(exc).__name__}: {exc}", permanent=False)
                db.add(t)
            elif t is not None:
                _logger.warning("stale_result_discarded: task={} 已失去租约, 丢弃失败结果", task_id)
    finally:
        if hb_stop is not None:
            hb_stop.set()
        _clear_heartbeat_if_own(task_id, lease_token)
        # V0.1.13: Release tracked resources
        global _task_resources_lock, _task_resources
        if _task_resources_lock is None:
            _task_resources_lock = threading.Lock()
        with _task_resources_lock:
            task_cost = _task_resources.pop(task_id, None)
        if task_cost:
            from app.core.resource_budget import release_resources

            release_resources(**task_cost)


# ═══════════════════════════════════════════════════
# TaskWorker (V0.1.11-alpha: 真正并发)
# ═══════════════════════════════════════════════════


class TaskWorker:
    """持久化任务队列 Worker,支持各阶段真正并发。"""

    def __init__(self) -> None:
        self._transcribing: set[asyncio.Task[None]] = set()
        self._analyzing: set[asyncio.Task[None]] = set()
        self._rendering: set[asyncio.Task[None]] = set()
        self._publishing: set[asyncio.Task[None]] = set()
        self._main_task: asyncio.Task[None] | None = None
        self._running = False
        _logger.info("TaskWorker init worker_id={}", _WORKER_ID)

    async def start(self) -> None:
        """启动 Worker 主循环。"""
        if self._running:
            return
        global _shutting_down
        _shutting_down = False
        self._running = True
        _recover_orphans()
        self._main_task = asyncio.create_task(self._loop())
        _logger.info("TaskWorker started T{}/A{}/R{}", MAX_TRANSCRIBING, MAX_ANALYZING, MAX_RENDERING)

    async def stop(self) -> None:
        """优雅关闭 — 停止领取新任务, 等待当前任务完成或取消。"""
        global _shutting_down
        _shutting_down = True
        self._running = False

        if self._main_task is not None:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

        grace_period = _WORKER_SHUTDOWN_TIMEOUT_S
        for coll_name, coll in [
            ("transcribing", self._transcribing),
            ("analyzing", self._analyzing),
            ("rendering", self._rendering),
            ("publishing", self._publishing),
        ]:
            pending = {t for t in coll if not t.done()}
            if pending:
                _logger.info("优雅关闭: 等待 {} 个 {} 任务完成 (最多 {}s)", len(pending), coll_name, grace_period)
                try:
                    done, _ = await asyncio.wait(pending, timeout=grace_period)
                    _logger.info("优雅关闭: {} 任务正常完成", len(done))
                except TimeoutError:
                    pass
                still_running = {t for t in coll if not t.done()}
                for t in still_running:
                    _logger.warning("优雅关闭: 强制取消 {} 任务", coll_name)
                    t.cancel()

        _logger.info("TaskWorker stopped.")
        _cleanup_subprocesses()

    async def _loop(self) -> None:
        while self._running and not _shutting_down:
            try:
                _retry_expired()
                _recover_stale()
                _advance_recorded()
                _advance_transcribed()
                _advance_candidate()
                _advance_awaiting_review()
                _advance_approved()
                _advance_rendered()
                # V0.1.13: Disk protection
                from app.pipeline.storage_lifecycle import is_safe_for_new_tasks

                disk_safe = is_safe_for_new_tasks()
                if disk_safe:
                    await self._dispatch(TaskStatus.QUEUED_FOR_TRANS, self._transcribing, MAX_TRANSCRIBING)
                    await self._dispatch(TaskStatus.QUEUED_FOR_ANALYSIS, self._analyzing, MAX_ANALYZING)
                    await self._dispatch(TaskStatus.QUEUED_FOR_RENDER, self._rendering, MAX_RENDERING)
                else:
                    _logger.warning("磁盘空间不足, 跳过 transcribe/analyze/render 调度")
                await self._dispatch(TaskStatus.QUEUED_FOR_PUBLISH, self._publishing, MAX_PUBLISHING)
                self._transcribing = {t for t in self._transcribing if not t.done()}
                self._analyzing = {t for t in self._analyzing if not t.done()}
                self._rendering = {t for t in self._rendering if not t.done()}
                self._publishing = {t for t in self._publishing if not t.done()}
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _logger.warning("tick error: {}", exc)
            await asyncio.sleep(2)

    async def _dispatch(
        self,
        queued_stage: str,
        running: set[asyncio.Task[None]],
        max_concurrent: int,
    ) -> None:
        _STAGE_TO_RESOURCE: dict[str, str] = {
            TaskStatus.QUEUED_FOR_TRANS: "asr",
            TaskStatus.QUEUED_FOR_ANALYSIS: "analysis",
            TaskStatus.QUEUED_FOR_RENDER: "render",
            TaskStatus.QUEUED_FOR_PUBLISH: "publish",
        }
        resource_key = _STAGE_TO_RESOURCE.get(queued_stage)
        while self._running and not _shutting_down and len(running) < max_concurrent:
            if resource_key:
                from app.core.resource_budget import acquire_resources, get_task_cost

                cost = acquire_resources(**get_task_cost(resource_key))
                if not cost:
                    _logger.debug("资源不足, 跳过阶段 {} (resource_key={})", queued_stage, resource_key)
                    break
            else:
                cost = {}

            task = _pop_and_claim(queued_stage)
            if task is None:
                if cost:
                    from app.core.resource_budget import release_resources

                    release_resources(**cost)
                break

            if cost:
                global _task_resources_lock
                if _task_resources_lock is None:
                    _task_resources_lock = threading.Lock()
                with _task_resources_lock:
                    _task_resources[task.id] = cost

            act_stage = active_stage(queued_stage)
            lease = task.lease_token
            t = asyncio.create_task(asyncio.to_thread(_execute_task, task.id, act_stage, lease))
            running.add(t)

    @property
    def stats(self) -> dict:
        """当前 Worker 和任务队列统计。"""
        counts = _task_counts()
        counts["worker"] = {
            "worker_id": _WORKER_ID,
            "transcribing": len(self._transcribing),
            "analyzing": len(self._analyzing),
            "rendering": len(self._rendering),
            "publishing": len(self._publishing),
            "max_transcribing": MAX_TRANSCRIBING,
            "max_analyzing": MAX_ANALYZING,
            "max_rendering": MAX_RENDERING,
            "max_publishing": MAX_PUBLISHING,
        }
        return counts


# ═══════════════════════════════════════════════════
# 统计和列表
# ═══════════════════════════════════════════════════


def _task_counts() -> dict:
    with get_session() as db:
        rows = db.exec(select(SegmentTask)).all()
    result: dict = {"total": len(rows)}
    for stage in (
        TaskStatus.RECORDED,
        TaskStatus.QUEUED_FOR_TRANS,
        TaskStatus.TRANSCRIBING,
        TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.ANALYZING,
        TaskStatus.CANDIDATE_CREATED,
        TaskStatus.AWAITING_REVIEW,
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
        TaskStatus.STALE,
    ):
        result[stage] = sum(1 for r in rows if r.stage == stage)
    return result


def list_tasks(limit: int = 50, stage: str | None = None) -> list[dict]:
    """列出任务队列中的任务。"""
    with get_session() as db:
        stmt = select(SegmentTask)
        if stage:
            stmt = stmt.where(SegmentTask.stage == stage)
        stmt = stmt.order_by(SegmentTask.created_at.desc()).limit(limit)
        tasks = db.exec(stmt).all()
    return [_task_to_dict(t) for t in tasks]


def _task_to_dict(t: SegmentTask) -> dict:
    return {
        "id": t.id,
        "segment_id": t.segment_id,
        "session_id": t.session_id,
        "candidate_id": t.candidate_id,
        "event_id": t.event_id,
        "clip_id": t.clip_id,
        "stage": t.stage,
        "failed_stage": t.failed_stage,
        "attempts": t.attempts,
        "max_retries": t.max_retries,
        "next_retry_at": t.next_retry_at.isoformat() if t.next_retry_at else None,
        "last_error": t.last_error,
        "error_is_permanent": t.error_is_permanent,
        "claimed_by": t.claimed_by,
        "claimed_at": t.claimed_at.isoformat() if t.claimed_at else None,
        "heartbeat_at": t.heartbeat_at.isoformat() if t.heartbeat_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "processing_time_ms": t.processing_time_ms,
        "total_elapsed_ms": t.total_elapsed_ms,
    }


# ── 全局单例 ──
# ── 兼容重导出 (V0.1.14: 函数已迁移到子模块) ──
from app.pipeline.stage_result import (  # noqa: E402, F401
    can_transition,
    make_idempotency_key,
    make_pipeline_key,
    make_stage_key,
    mark_active,
)
from app.pipeline.workers.analyze import _ensure_event  # noqa: E402, F401

# 后向兼容: 旧名称
_can_transition = can_transition
_make_idempotency_key = make_idempotency_key
_make_pipeline_key = make_pipeline_key
_make_stage_key = make_stage_key

task_worker: TaskWorker = TaskWorker()
