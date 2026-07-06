"""持久化任务队列 Worker (V0.1.11-alpha 重构)。

V0.1.11-alpha 核心变更:
- 真正并发:各阶段独立 dispatch,不阻塞其他阶段
- 原子领取:条件 UPDATE + 受影响行数校验
- attempts 只增一次: mark_active 仅在 _pop_and_claim 成功时调用
- failed_stage: 精确记录失败阶段,重试时从对应队列恢复
- 心跳 + stale 恢复: 长任务定期 heartbeat,重启后识别 stale 重新入队
- 重试退避含随机抖动
- Worker ID: 每个实例生成唯一 worker_id
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import text as sa_text
from sqlmodel import select

from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error, is_retryable
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

# ── 并发配置 ──────────────────────────────────────────────────────
MAX_TRANSCRIBING = int(os.environ.get("MAX_TRANSCRIBING", "1"))
MAX_ANALYZING = int(os.environ.get("MAX_ANALYZING", "2"))
MAX_RENDERING = int(os.environ.get("MAX_RENDERING", "2"))
MAX_PUBLISHING = int(os.environ.get("MAX_PUBLISHING", "1"))

_WORKER_SHUTDOWN_TIMEOUT_S = int(os.environ.get("WORKER_SHUTDOWN_TIMEOUT_SECONDS", "30"))
_SUBPROCESS_TERMINATE_TIMEOUT_S = int(os.environ.get("SUBPROCESS_TERMINATE_TIMEOUT_SECONDS", "10"))

_RETRY_BASE_S = 10
_RETRY_MAX_S = 600
_RETRY_JITTER_S = 5
_RETRY_MAX_COUNT = 5

_HEARTBEAT_INTERVAL_S = 30
_STALE_TIMEOUT_S = 120
_HEARTBEAT_POLL_S = 5  # V0.1.12.2: 心跳线程轮询间隔

_WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
_logger = logger

# V0.1.13: Task-level resource tracking (ResourceBudget integration)
_task_resources: dict[int, dict[str, int | float]] = {}
_task_resources_lock = None  # lazily initialized

# V0.1.12.2: Worker 生命周期
_shutting_down: bool = False
# V0.1.12.4: 子进程跟踪 (用于优雅关闭时清理孤儿 FFmpeg 进程)
_subprocesses: list = []
_subprocesses_lock = None  # lazily initialized


# ── 全局单例 Worker ─────────────────────────────────────────────────



def _now() -> datetime:
    return datetime.now(UTC)


def _make_pipeline_key(segment_id: int) -> str:
    """流程级幂等键: 创建后永不修改。"""
    return f"pipeline:{segment_id}"


def _make_stage_key(segment_id: int, stage: str) -> str:
    """阶段级幂等键: enqueue_next 时更新。"""
    return f"stage:{segment_id}:{stage}"


def _make_idempotency_key(segment_id: int, stage: str) -> str:
    """[后向兼容] 旧幂等键。"""
    return f"{segment_id}:{stage}"


def _jitter(base: float, jitter_s: float = _RETRY_JITTER_S) -> float:
    return base + random.uniform(0, jitter_s)


def track_subprocess(proc) -> None:
    """注册子进程句柄, 供关闭时统一 terminate/kill。

    :param proc: subprocess.Popen 实例。
    """
    global _subprocesses_lock
    import threading
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
# 状态转换矩阵
# ═══════════════════════════════════════════════════

# V0.1.12.10: 尝试添加远程结果未知状态到 PUBLISHING 的合法转换
try:
    _PUBLISHING_EXTRA: set[str] = {
        TaskStatus.REMOTE_RESULT_UNKNOWN,
        TaskStatus.RECONCILIATION_REQUIRED,
    }
except AttributeError:
    _logger.warning(
        "TaskStatus 缺少 REMOTE_RESULT_UNKNOWN/RECONCILIATION_REQUIRED, "
        "PUBLISHING 将保持现有转换"
    )
    _PUBLISHING_EXTRA = set()

_VALID_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.RECORDED: {TaskStatus.QUEUED_FOR_TRANS},
    TaskStatus.QUEUED_FOR_TRANS: {TaskStatus.TRANSCRIBING},
    TaskStatus.TRANSCRIBING: {TaskStatus.TRANSCRIBED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.TRANSCRIBED: {TaskStatus.QUEUED_FOR_ANALYSIS},
    TaskStatus.QUEUED_FOR_ANALYSIS: {TaskStatus.ANALYZING},
    TaskStatus.ANALYZING: {TaskStatus.CANDIDATE_CREATED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    # V0.1.12.5: 审核先于渲染
    TaskStatus.CANDIDATE_CREATED: {TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED},
    TaskStatus.AWAITING_REVIEW: {TaskStatus.APPROVED, TaskStatus.COMPLETED, TaskStatus.CANCELLED},
    TaskStatus.APPROVED: {TaskStatus.APPROVED_WAITING_RENDER, TaskStatus.QUEUED_FOR_RENDER},
    TaskStatus.APPROVED_WAITING_RENDER: {TaskStatus.QUEUED_FOR_RENDER, TaskStatus.CANCELLED},
    TaskStatus.QUEUED_FOR_RENDER: {TaskStatus.RENDERING},
    TaskStatus.RENDERING: {TaskStatus.RENDERED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    # V0.1.12.5: 发布阶段独立
    TaskStatus.RENDERED: {TaskStatus.AWAITING_PUBLISH_CONFIRMATION, TaskStatus.QUEUED_FOR_PUBLISH},
    TaskStatus.AWAITING_PUBLISH_CONFIRMATION: {TaskStatus.QUEUED_FOR_PUBLISH, TaskStatus.CANCELLED},
    TaskStatus.QUEUED_FOR_PUBLISH: {TaskStatus.PUBLISHING},
    TaskStatus.PUBLISHING: {TaskStatus.COMPLETED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED} | _PUBLISHING_EXTRA,
    # 重试/恢复
    TaskStatus.TRANSIENT_FAILED: {
        TaskStatus.QUEUED_FOR_TRANS, TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.QUEUED_FOR_RENDER, TaskStatus.QUEUED_FOR_PUBLISH, TaskStatus.FAILED,
    },  # noqa: E501
    TaskStatus.STALE: {
        TaskStatus.QUEUED_FOR_TRANS, TaskStatus.QUEUED_FOR_ANALYSIS,
        TaskStatus.QUEUED_FOR_RENDER, TaskStatus.QUEUED_FOR_PUBLISH,
    },  # noqa: E501
}


def _can_transition(current: str, target: str) -> bool:
    return target in _VALID_TRANSITIONS.get(current, set())


def _active_stage(queued_stage: str) -> str:
    return {
        TaskStatus.QUEUED_FOR_TRANS: TaskStatus.TRANSCRIBING,
        TaskStatus.QUEUED_FOR_ANALYSIS: TaskStatus.ANALYZING,
        TaskStatus.QUEUED_FOR_RENDER: TaskStatus.RENDERING,
        TaskStatus.QUEUED_FOR_PUBLISH: TaskStatus.PUBLISHING,
    }.get(queued_stage, queued_stage)


# ═══════════════════════════════════════════════════
# 生命周期函数
# ═══════════════════════════════════════════════════

def create_task(segment_id: int, session_id: int) -> SegmentTask | None:
    """为已完成录制的片段创建任务(幂等, V0.1.12.7: IntegrityError 吸收)。

    检查 pipeline_key 唯一性,已存在任务(任何阶段,包括 completed)时返回 None。
    并发冲突时返回 None (调用方可认为任务已存在)。
    """
    from sqlalchemy.exc import IntegrityError

    pipeline_key = _make_pipeline_key(segment_id)
    stage_key = _make_stage_key(segment_id, "recorded")
    with get_session() as db:
        # V0.1.12.5: 先查 pipeline_key (流程级幂等)
        existing = db.exec(
            select(SegmentTask).where(SegmentTask.pipeline_key == pipeline_key)
        ).first()
        if existing is not None:
            _logger.debug("pipeline_key 已存在: segment={} task={} stage={}", segment_id, existing.id, existing.stage)
            return None
        # 后向兼容: 检查旧 idempotency_key
        old_key = _make_idempotency_key(segment_id, "recorded")
        existing_old = db.exec(
            select(SegmentTask).where(SegmentTask.idempotency_key == old_key)
        ).first()
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
            # 并发: 同 pipeline_key 的任务已被另一个线程创建
            db.rollback()
            _logger.info("idempotency_conflict_resolved: segment={} 任务已被并发创建", segment_id)
            return None


def enqueue_next(
    task: SegmentTask,
    next_stage: str,
    candidate_id: int | None = None,
    event_id: int | None = None,
    clip_id: int | None = None,
) -> None:
    """将任务推进到下一阶段,重置 attempts 并计算完成时间 (V0.1.12.5: pipeline_key + stage_key)。

    pipeline_key 仅在首次创建时设置,此后永不修改。
    stage_key 随阶段更新,用于阶段级幂等。
    """
    current = task.stage
    if not _can_transition(current, next_stage):
        raise ValueError(f"非法转换: {current} -> {next_stage}")
    task.stage = next_stage
    # V0.1.12.5: stage_key 随阶段变化, pipeline_key 不变
    task.stage_key = _make_stage_key(task.segment_id, next_stage)
    # 后向兼容: 同时更新旧 idempotency_key
    task.idempotency_key = _make_idempotency_key(task.segment_id, next_stage)
    task.attempts = 0
    task.last_error = None
    task.error_is_permanent = False
    task.next_retry_at = None
    task.claimed_by = None
    task.claimed_at = None
    task.heartbeat_at = None
    task.lease_token = None
    if next_stage in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
        task.completed_at = _now()
        if task.created_at:
            task.total_elapsed_ms = int((_now() - task.created_at).total_seconds() * 1000)
    if candidate_id is not None:
        task.candidate_id = candidate_id
    if event_id is not None:
        task.event_id = event_id
    if clip_id is not None:
        task.clip_id = clip_id


def mark_active(task: SegmentTask) -> None:
    """唯一 attempts++ 点:仅在 _pop_and_claim 成功后调用。"""
    task.attempts += 1
    task.started_at = _now()
    task.heartbeat_at = _now()
    task.last_error = None


def mark_heartbeat(task: SegmentTask) -> None:
    """更新任务心跳时间,防止被 stale recovery 误判。"""
    task.heartbeat_at = _now()


def mark_completed(task: SegmentTask, processing_ms: int | None = None) -> None:
    """标记任务完成,记录总耗时。"""
    if processing_ms is None and task.started_at is not None:
        processing_ms = int((_now() - task.started_at).total_seconds() * 1000)
    task.processing_time_ms = processing_ms
    task.completed_at = _now()
    task.heartbeat_at = None


def mark_failed(task: SegmentTask, error: str, permanent: bool = False) -> None:
    """标记任务失败,记录失败阶段和错误信息。"""
    task.last_error = error[:1000]
    task.error_is_permanent = permanent
    task.failed_stage = task.stage  # V0.1.11-alpha:记录失败阶段
    task.heartbeat_at = None
    if permanent:
        task.stage = TaskStatus.FAILED
        task.completed_at = _now()
        try:
            from app.notify.webhook import notify_task_failed
            notify_task_failed(task.id, task.failed_stage, error[:200])
        except Exception:
            pass
    else:
        delay = _jitter(
            min(_RETRY_BASE_S * (2 ** max(task.attempts - 1, 0)), _RETRY_MAX_S)
        )
        task.next_retry_at = _now() + timedelta(seconds=delay)
        task.stage = TaskStatus.TRANSIENT_FAILED


# ═══════════════════════════════════════════════════
# 原子领取 (V0.1.11-alpha)
# ═══════════════════════════════════════════════════

def _pop_and_claim(queued_stage: str) -> SegmentTask | None:
    """原子领取: 条件 UPDATE + 行数校验 + lease_token (V0.1.12.5)。

    流程:
    1. SELECT 一个符合条件的 candidate task
    2. 条件 UPDATE WHERE id=? AND stage=queued_stage (防止竞态)
    3. 只有 rowcount==1 时才算领取成功
    4. lease_token (UUIDv4) 用于后续条件提交时校验所有权
    5. 再 SELECT 取回完整对象
    """
    now = _now()
    active_stage = _active_stage(queued_stage)
    lease_token = uuid.uuid4().hex
    with get_session() as db:
        # Step 1: SELECT candidate
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

        # Step 2: 原子条件 UPDATE (只有 stage 仍为 queued_stage 时才更新)
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
                "active": active_stage,
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

        # Step 3: 取回完整对象 (commit 后数据已落库)
        task = db.get(SegmentTask, task_id)
        if task is None:
            return None

        _logger.info("原子领取成功 task_id={} stage={} segment={} worker={} lease={}",
                     task_id, active_stage, segment_id, _WORKER_ID, lease_token[:12])
        return task


# ═══════════════════════════════════════════════════
# 阶段推进
# ═══════════════════════════════════════════════════

def _room_cfg_from_task(task: SegmentTask) -> dict:
    """从任务读取房间级自动化开关 (V0.1.12.2 新增)。

    :returns: auto_analyze/auto_render/auto_approve/auto_upload 开关。
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


def _advance_recorded() -> None:
    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.RECORDED)
        ).all()
        for task in tasks:
            seg = db.get(RawSegment, task.segment_id)
            if seg is not None and seg.status == OldStatus.RECORDED:
                # V0.1.12.2: auto_analyze=false 时不推进到转写
                cfg = _room_cfg_from_task(task)
                if not cfg.get("auto_analyze", False):
                    _logger.debug("auto_analyze=off, 片段 {} 不自动进入转写队列", task.segment_id)
                    continue
                enqueue_next(task, TaskStatus.QUEUED_FOR_TRANS)
                db.add(task)


def _advance_transcribed() -> None:
    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.TRANSCRIBED)
        ).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            if not cfg.get("auto_analyze", False):
                _logger.debug("auto_analyze=off, 片段 {} 不自动创建分析任务", task.segment_id)
                continue
            enqueue_next(task, TaskStatus.QUEUED_FOR_ANALYSIS)
            db.add(task)


def _advance_candidate() -> None:
    """CANDIDATE_CREATED → AWAITING_REVIEW 或 APPROVED (V0.1.12.7: 统一审批事务)。

    auto_approve=true 且分数达标 → 调用统一审批服务 (更新 Task+Event+Candidate)
    否则 → AWAITING_REVIEW
    """
    from app.pipeline.approval import approve_event_and_task

    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.CANDIDATE_CREATED)
        ).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            auto_approve = bool(cfg.get("auto_approve", False))
            threshold = float(cfg.get("auto_approve_threshold", 0.82))

            if auto_approve:
                from app.db.models import HighlightCandidate
                candidate = db.get(HighlightCandidate, task.candidate_id) if task.candidate_id else None
                score = candidate.highlight_score if candidate else 0.0
                if score >= threshold and task.event_id is not None:
                    # V0.1.12.8: 传入外层 db session, 同一事务
                    ok = approve_event_and_task(
                        task_id=task.id,
                        event_id=task.event_id,
                        source="auto",
                        review_decision="approved_solo",
                        db=db,
                    )
                    if ok:
                        _logger.info("auto_approve: task={} candidate={} event={} score={:.2f}",  # noqa: E501
                                     task.id, task.candidate_id, task.event_id, score)
                        continue

            enqueue_next(task, TaskStatus.AWAITING_REVIEW)
            db.add(task)


def _advance_awaiting_review() -> None:
    """V0.1.12.7: auto_approve 使用统一审批事务 (更新 Task+Event+Candidate)。
    
    auto_approve=off → 保留在 awaiting_review 等人工批准。
    """
    from app.pipeline.approval import approve_event_and_task

    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.AWAITING_REVIEW)
        ).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            auto_approve = bool(cfg.get("auto_approve", False))
            threshold = float(cfg.get("auto_approve_threshold", 0.82))

            if not auto_approve:
                _logger.debug("auto_approve=off, task {} 留在 awaiting_review", task.id)
                continue

            from app.db.models import HighlightCandidate
            candidate = db.get(HighlightCandidate, task.candidate_id) if task.candidate_id else None
            score = candidate.highlight_score if candidate else 0.0
            if score < threshold:
                _logger.debug("候选 {} 分数 {:.2f} < 阈值 {:.2f}, 不自动批准",
                              task.candidate_id, score, threshold)
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
                _logger.info("auto_approve: task={} candidate={} event={} score={:.2f}",
                             task.id, task.candidate_id, task.event_id, score)


def _advance_approved() -> None:
    """APPROVED → APPROVED_WAITING_RENDER 或 QUEUED_FOR_RENDER (V0.1.12.7)。

    auto_render=true → QUEUED_FOR_RENDER
    auto_render=false → APPROVED_WAITING_RENDER (等待手动渲染)

    V0.1.12.7: 进入渲染队列前检查 Event 真实 review_status。
    """
    from app.pipeline.approval import assert_event_approved

    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.APPROVED)
        ).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            auto_render = bool(cfg.get("auto_render", False))

            if auto_render:
                # V0.1.12.7: 检查 Event 真实批准状态
                if task.event_id and not assert_event_approved(task.event_id):
                    _logger.error(
                        "consistency_error: task={} event={} review_status 非 APPROVED, "
                        "阻止进入渲染队列。需人工修复。",
                        task.id, task.event_id,
                    )
                    task.last_error = f"data_consistency_error: event {task.event_id} not approved, cannot render"
                    db.add(task)
                    continue
                enqueue_next(task, TaskStatus.QUEUED_FOR_RENDER)
                _logger.info("auto_render: task={} candidate={} → queued_for_render", task.id, task.candidate_id)
            else:
                enqueue_next(task, TaskStatus.APPROVED_WAITING_RENDER)
                _logger.info("auto_render=off: task={} → approved_waiting_render", task.id)
            db.add(task)


def _advance_rendered() -> None:
    """Advance RENDERED tasks to publish queue or awaiting confirmation (V0.1.12.5).

    auto_upload=true → QUEUED_FOR_PUBLISH
    auto_upload=false → AWAITING_PUBLISH_CONFIRMATION (等待手动发布)
    """
    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.RENDERED)
        ).all()
        for task in tasks:
            cfg = _room_cfg_from_task(task)
            auto_upload = bool(cfg.get("auto_upload", False))

            if auto_upload:
                enqueue_next(task, TaskStatus.QUEUED_FOR_PUBLISH)
                _logger.info("auto_upload: task={} clip={} → queued_for_publish", task.id, task.clip_id)
            else:
                enqueue_next(task, TaskStatus.AWAITING_PUBLISH_CONFIRMATION)
                _logger.info("auto_upload=off: task={} → awaiting_publish_confirmation", task.id)
            db.add(task)


# ═══════════════════════════════════════════════════
# 重试 (V0.1.11-alpha: 从 failed_stage 恢复)
# ═══════════════════════════════════════════════════

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
# 心跳 + Stale 恢复 (V0.1.11-alpha)
# ═══════════════════════════════════════════════════

def _recover_stale() -> None:
    stale_threshold = _now() - timedelta(seconds=_STALE_TIMEOUT_S)
    with get_session() as db:
        stale = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_([
                    TaskStatus.TRANSCRIBING, TaskStatus.ANALYZING,
                    TaskStatus.RENDERING, TaskStatus.PUBLISHING,
                ]),
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
    _recover_stale()
    with get_session() as db:
        stuck = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_([
                    TaskStatus.TRANSCRIBING, TaskStatus.ANALYZING,
                    TaskStatus.RENDERING, TaskStatus.PUBLISHING,
                ]),
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

        existing_ids = {
            t.segment_id for t in db.exec(select(SegmentTask.segment_id)).all()
        }
        orphan_segs = db.exec(
            select(RawSegment).where(
                RawSegment.status == OldStatus.RECORDED,
                ~RawSegment.id.in_(existing_ids) if existing_ids else True,
            )
        ).all()
        for seg in orphan_segs:
            pipeline_key = _make_pipeline_key(seg.id)
            stage_key = _make_stage_key(seg.id, "recorded")
            t = SegmentTask(
                segment_id=seg.id,
                session_id=seg.session_id,
                stage=TaskStatus.RECORDED,
                pipeline_key=pipeline_key,
                stage_key=stage_key,
                idempotency_key=_make_idempotency_key(seg.id, "recorded"),
            )
            db.add(t)
        if orphan_segs:
            _logger.info("恢复:为 {} 个孤立片段创建任务。", len(orphan_segs))


# ═══════════════════════════════════════════════════
# 执行 (V0.1.12.2: 周期性心跳 + 优雅关闭感知)
# ═══════════════════════════════════════════════════

def _start_heartbeat_thread(
    task_id: int,
    lease_token: str | None = None,
    expected_stage: str | None = None,
) -> threading.Event:
    """启动后台心跳线程, 使用租约条件更新 heartbeat_at (V0.1.12.9 强化)。

    每次心跳使用条件 SQL: WHERE id=? AND claimed_by=? AND lease_token=? AND stage=?
    如果 rowcount==0 说明已失去租约, 停止心跳。

    :param task_id: SegmentTask ID。
    :param lease_token: 租约令牌 (V0.1.12.7 — 条件校验)。
    :param expected_stage: 期望的阶段状态 (V0.1.12.9 — 阶段校验)。
    :returns: stop_event — 设置后心跳线程退出。
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.is_set() and not _shutting_down:
            try:
                with get_session() as db:
                    if lease_token and expected_stage:
                        # V0.1.12.9: 条件心跳 — 校验租约 + 阶段
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
                        # V0.1.12.7: 条件心跳 — 校验租约 (向后兼容旧调用方)
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


def _still_has_lease(task: SegmentTask, worker_id: str, lease_token: str | None, expected_stage: str) -> bool:
    """校验 Worker 是否仍持有任务的租约 (V0.1.12.9 强化)。

    必须同时验证: claimed_by, lease_token, stage, 且不是 stale。
    """
    if lease_token is None:
        return True
    return (
        task.claimed_by == worker_id
        and task.lease_token == lease_token
        and task.stage == expected_stage
        and task.stage != TaskStatus.STALE
    )


# ═══════════════════════════════════════════════════
# 分离 compute / commit 阶段 (V0.1.13-alpha)
# ═══════════════════════════════════════════════════

def _transcribe_compute(task_id: int) -> dict:
    """仅执行转写计算, 不写入 Task 状态。

    调用 transcribe_segment 进行语音识别, 收集计算结果。
    DB 写入 (Transcript, RawSegment) 由 transcribe_segment 内部完成,
    本函数只负责编排, 不直接操作 Task 状态。

    :param task_id: SegmentTask ID。
    :returns: {"segment_id": int}
    """
    from app.analysis.transcribe import transcribe_segment
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return {"segment_id": -1}
        segment_id = task.segment_id
    transcribe_segment(segment_id)
    return {"segment_id": segment_id}


def _commit_transcript(lease: TaskLease, compute_result: dict, ms: int) -> None:
    """单事务提交转写结果, 先校验租约。

    在单个短事务内:
    1. 验证租约有效性
    2. 标记任务完成并推进到 TRANSCRIBED

    :param lease: 任务租约。
    :param compute_result: _transcribe_compute 的输出。
    :param ms: 处理耗时 (毫秒)。
    """
    try:
        with get_session() as db:
            if not still_owns_lease(db, lease):
                raise LeaseLostError()
            task = db.get(SegmentTask, lease.task_id)
            if task is None:
                return
            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.TRANSCRIBED)
            db.add(task)
    except LeaseLostError:
        _logger.warning("stale_result_discarded: transcript task={} 已失去租约", lease.task_id)


def _analyze_compute(task_id: int) -> dict:
    """仅执行分析计算, 不创建 Event 和写入 Task 状态。

    调用 score_segment 进行精彩片段评分, 收集候选 ID。
    HighlightEvent 的创建移至 _commit_highlight 阶段。

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


def _commit_highlight(lease: TaskLease, compute_result: dict, ms: int) -> None:
    """单事务提交分析结果, 先校验租约, 再创建 Event (幂等)。

    在单个短事务内:
    1. 验证租约有效性
    2. 如有 candidate_id, 幂等创建/获取 HighlightEvent
    3. 标记任务完成并推进到 CANDIDATE_CREATED 或 COMPLETED

    :param lease: 任务租约。
    :param compute_result: _analyze_compute 的输出。
    :param ms: 处理耗时 (毫秒)。
    """
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
        _logger.warning("stale_result_discarded: highlight task={} 已失去租约", lease.task_id)


def _clear_heartbeat_if_own(task_id: int, lease_token: str | None = None) -> None:
    """条件清除 heartbeat, 必须携带租约 (V0.1.12.7)。

    如果 rowcount==0 说明租约已被其他 Worker 接管 → 不做任何修改。
    禁止在 finally 中无条件清除其他 Worker 的 heartbeat。
    """
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


def _execute_task(task_id: int, active_stage: str, lease_token: str | None = None) -> None:
    """执行耗时任务, 传递 lease_token 用于条件提交 (V0.1.13-alpha: TaskLease)。

    所有耗时任务结果提交前必须校验租约。
    V0.1.13-alpha: TRANSCRIBING/ANALYZING 阶段采用 compute/commit 分离模式。
    """
    t0 = time.time()
    hb_stop: threading.Event | None = None
    lease: TaskLease | None = None
    try:
        # V0.1.12.9: 心跳现在传入 lease_token + expected_stage 用于条件校验
        hb_stop = _start_heartbeat_thread(task_id, lease_token, active_stage)
        lease = TaskLease(task_id=task_id, worker_id=_WORKER_ID, lease_token=lease_token, expected_stage=active_stage)

        if active_stage == TaskStatus.TRANSCRIBING:
            _run_transcribe(lease)
        elif active_stage == TaskStatus.ANALYZING:
            _run_analyze(lease)
        elif active_stage == TaskStatus.RENDERING:
            _run_render(task_id, lease_token)
        elif active_stage == TaskStatus.PUBLISHING:
            _run_publish(task_id, lease_token)
    except LeaseLostError:
        _logger.warning("lease_lost_during_execution: task={}", task_id)
    except Exception as exc:
        _ms = int((time.time() - t0) * 1000)
        _logger.error("任务 {} 阶段 {} 失败: {}", task_id, active_stage, exc)
        # V0.1.12.7: 失败时也校验租约后再标记
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
        # V0.1.12.7: 条件清除 heartbeat —— 必须校验租约
        _clear_heartbeat_if_own(task_id, lease_token)
        # V0.1.13: Release tracked resources for this task
        global _task_resources_lock, _task_resources
        import threading
        if _task_resources_lock is None:
            _task_resources_lock = threading.Lock()
        with _task_resources_lock:
            task_cost = _task_resources.pop(task_id, None)
        if task_cost:
            from app.core.resource_budget import release_resources
            release_resources(**task_cost)


def _run_transcribe(lease: TaskLease) -> None:
    """执行转写阶段: 计算与提交分离 (V0.1.13-alpha)。

    先执行计算 (transcribe_segment), 再校验租约后提交 Task 状态。

    :param lease: 任务租约。
    """
    t0 = time.time()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return
        mark_heartbeat(task)
        db.add(task)
    compute_result = _transcribe_compute(lease.task_id)
    ms = int((time.time() - t0) * 1000)
    _commit_transcript(lease, compute_result, ms)


def _run_analyze(lease: TaskLease) -> None:
    """执行分析阶段: 计算与提交分离 (V0.1.13-alpha)。

    先执行计算 (score_segment), 再在单事务内校验租约、创建 Event、提交 Task 状态。

    :param lease: 任务租约。
    """
    t0 = time.time()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return
        mark_heartbeat(task)
        db.add(task)
    compute_result = _analyze_compute(lease.task_id)
    ms = int((time.time() - t0) * 1000)
    _commit_highlight(lease, compute_result, ms)


def _is_render_error_permanent(exc: Exception) -> bool:
    """从渲染异常中提取 FFmpeg 错误类型, 判断是否永久失败。

    优先从 RuntimeError 消息中解析错误类型名称,
    否则回退到使用 stderr 重新分类。

    :param exc: 渲染过程中捕获的异常。
    :returns: True 表示永久失败(不可重试, 如 DISK_FULL/PERMISSION_DENIED)。
    """
    msg = str(exc)
    # 尝试从 RuntimeError 消息中提取错误类型, 格式: "...[ERROR_TYPE]: ..."
    m = re.search(r"\[([A-Z_]+)\]", msg)
    if m:
        type_name = m.group(1)
        try:
            error_type = FfmpegErrorType[type_name]
            return not is_retryable(error_type)
        except KeyError:
            pass
    # 回退: 尝试从 stderr 部分重新分类
    if isinstance(exc, RuntimeError):
        stderr_marker = "]: "
        idx = msg.find(stderr_marker)
        if idx != -1:
            extracted_stderr = msg[idx + len(stderr_marker):]
        else:
            extracted_stderr = msg
        error_type = classify_ffmpeg_error(-1, extracted_stderr)
        if error_type != FfmpegErrorType.UNKNOWN:
            return not is_retryable(error_type)
    # 无法归类, 默认为瞬时错误(可重试)
    return False


def _render_compute(task_id: int) -> dict:
    """纯渲染计算 — 无数据库写入 (V0.1.12.10: compute/commit 分离)。

    从 DB 读取 candidate_id, 调用 produce_clip 生成剪辑,
    返回结果 dict。所有 DB 写入在 _commit_render 中完成。

    :returns: {"clip_id", "file_path", "duration_s"} 或 {"error", "permanent"}。
    """
    from app.pipeline.orchestrator import produce_clip

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None or task.candidate_id is None:
            return {"error": "task or candidate_id missing", "permanent": True}
        cid = task.candidate_id

    try:
        clip = produce_clip(cid, auto_upload=False)
    except Exception as exc:
        permanent = _is_render_error_permanent(exc)
        return {"error": f"RenderError: {exc}", "permanent": permanent}

    if clip is None:
        return {"error": "clip rendering returned no result", "permanent": False}

    from pathlib import Path as _Path
    out_exists = clip.file_path and _Path(clip.file_path).exists()
    out_size_ok = out_exists and _Path(clip.file_path).stat().st_size > 1024
    if not out_exists:
        return {"error": "output file missing", "permanent": False}
    if not out_size_ok:
        detail = (
            f"output too small ({_Path(clip.file_path).stat().st_size} bytes)"
            if clip.file_path
            else "no output path"
        )
        return {"error": f"RenderFailedError: {detail}", "permanent": False}

    if clip.duration_s is not None and clip.duration_s < 1.0:
        return {"error": f"output duration too short ({clip.duration_s:.1f}s)", "permanent": False}

    return {
        "clip_id": clip.id,
        "file_path": clip.file_path,
        "duration_s": clip.duration_s,
    }


def _commit_render(lease: TaskLease, compute_result: dict, ms: int) -> None:
    """提交渲染结果 — 单一事务 + 租约校验 (V0.1.12.10)。

    在单个 DB 事务中: 校验租约 → 根据结果标记失败或完成。
    """
    with get_session() as db:
        if not still_owns_lease(db, lease):
            _logger.warning("stale_result_discarded: task={} 已失去租约, 丢弃渲染结果", lease.task_id)
            return

        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return

        if "error" in compute_result:
            mark_failed(
                task,
                compute_result["error"],
                permanent=compute_result.get("permanent", False),
            )
            db.add(task)
            return

        mark_completed(task, ms)
        enqueue_next(task, TaskStatus.RENDERED, clip_id=compute_result.get("clip_id"))
        db.add(task)


def _run_render(lease: TaskLease) -> None:
    """渲染阶段入口 — heartbeat → compute → commit (V0.1.12.10 重构)。"""
    t0 = time.time()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None or task.candidate_id is None:
            return
        mark_heartbeat(task)
        db.add(task)
    compute_result = _render_compute(lease.task_id)
    ms = int((time.time() - t0) * 1000)
    _commit_render(lease, compute_result, ms)


def _publish_compute(task_id: int) -> dict:
    """纯发布计算 — 无数据库状态写入 (V0.1.12.10: compute/commit 分离)。

    验证前置条件 (Event 已批准、FinalClip 存在、文件完整),
    然后调用 enqueue_and_upload 执行上传。
    超时或状态不确定时返回 {"remote_result_unknown": True}。

    :returns: upload task info dict, error dict, 或 remote_result_unknown dict。
    """
    from pathlib import Path as _Path

    from app.db.models import FinalClip, HighlightEvent, ReviewStatus

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return {"error": "task not found", "permanent": True}
        clip_id = task.clip_id
        event_id = task.event_id
        if clip_id is None:
            return {"error": "任务缺少 clip_id", "permanent": True}

        # 只读校验前置条件
        event = db.get(HighlightEvent, event_id) if event_id else None
        if event is None or event.review_status not in ReviewStatus.POSITIVE:
            return {"error": "Event 未批准或不存在", "permanent": True}

        clip = db.get(FinalClip, clip_id)
        if clip is None:
            return {"error": f"FinalClip {clip_id} 不存在", "permanent": True}

        if not clip.file_path or not _Path(clip.file_path).exists():
            return {"error": "输出文件缺失", "permanent": True}

    try:
        from app.publishing.uploader import enqueue_and_upload
        upload_task = enqueue_and_upload(clip_id)
    except Exception as exc:
        error_msg = str(exc).lower()
        if "timeout" in error_msg or "timed out" in error_msg:
            return {"remote_result_unknown": True}
        return {"error": f"PublishError: {exc}", "permanent": False}

    if upload_task is None:
        return {"error": "upload_task 为空", "permanent": False}

    ustatus = upload_task.status
    if ustatus is None or ustatus == "":
        return {"remote_result_unknown": True}

    return {
        "upload_task_id": upload_task.id or 0,
        "upload_status": ustatus,
        "upload_error": upload_task.last_error,
        "remote_id": upload_task.remote_id,
    }


def _commit_publish(lease: TaskLease, compute_result: dict) -> None:
    """提交发布结果 — 单一事务 + 租约校验 (V0.1.12.10)。

    在单个 DB 事务中: 校验租约 → 根据结果标记失败、完成或记录远程结果未知。
    """
    with get_session() as db:
        if not still_owns_lease(db, lease):
            _logger.warning("stale_result_discarded: task={} 已失去租约, 丢弃发布结果", lease.task_id)
            return

        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return

        if compute_result.get("remote_result_unknown"):
            _logger.warning(
                "remote_result_unknown: task={} 上传结果未知, 保持 PUBLISHING 状态",
                lease.task_id,
            )
            return

        if "error" in compute_result:
            mark_failed(
                task,
                compute_result["error"],
                permanent=compute_result.get("permanent", False),
            )
            db.add(task)
            return

        from app.pipeline.approval import apply_upload_result
        apply_upload_result(
            task_id=lease.task_id,
            upload_task_id=compute_result.get("upload_task_id", 0),
            upload_status=compute_result.get("upload_status", ""),
            upload_error=compute_result.get("upload_error"),
            remote_id=compute_result.get("remote_id"),
        )


def _run_publish(lease: TaskLease) -> None:
    """发布阶段入口 — heartbeat → compute → commit (V0.1.12.10 重构)。"""
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return
        if task.clip_id is None:
            mark_failed(task, "PublishError: 任务缺少 clip_id", permanent=True)
            db.add(task)
            return
        mark_heartbeat(task)
        db.add(task)
    compute_result = _publish_compute(lease.task_id)
    _commit_publish(lease, compute_result)


# ═══════════════════════════════════════════════════
# V0.1.11-alpha: 自动创建 HighlightEvent
# ═══════════════════════════════════════════════════

def _ensure_event(candidate_id: int) -> int | None:
    """确保每个 HighlightCandidate 有唯一 HighlightEvent (幂等, V0.1.12.7: IntegrityError 吸收)。

    V0.1.12.7: 使用 IntegrityError 处理并发冲突, 异常后重新查询已有 Event。
    """
    from sqlalchemy.exc import IntegrityError

    from app.db.models import HighlightCandidate, HighlightEvent, ReviewStatus

    with get_session() as db:
        existing = db.exec(
            select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)
        ).first()
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
            _logger.info("auto event: eid={} cid={}", event.id, candidate_id)
            return event.id
        except IntegrityError:
            # 并发冲突: 另一个 Worker 已创建, 回滚后重新查询
            db.rollback()
            _logger.info("idempotency_conflict_resolved: event cid={} 已被并发创建, 查询已有记录", candidate_id)
            # 在新 session 中查询 (当前 session 已回滚)
            pass

    # 重新查询
    with get_session() as db:
        existing = db.exec(
            select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)
        ).first()
        if existing is not None:
            return existing.id
        _logger.error("IntegrityError 后无法找到已有 Event: candidate_id={}", candidate_id)
        return None


# ═══════════════════════════════════════════════════
# Worker (V0.1.11-alpha: 真正并发)
# ═══════════════════════════════════════════════════

class TaskWorker:
    """持久化任务队列 Worker,支持各阶段真正并发 (V0.1.11-alpha)。"""

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
        """V0.1.12.2: 优雅关闭 — 停止领取新任务, 等待当前任务完成或取消。

        1. 停止领取新任务 (shutting_down=True)
        2. 取消所有 pending 的 dispatch task
        3. 等待主循环退出
        4. 清理未完成任务的状态
        """
        global _shutting_down
        _shutting_down = True
        self._running = False

        # 取消主循环
        if self._main_task is not None:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

        # V0.1.12.5: 等待已启动的任务安全完成 (可配置超时)
        grace_period = _WORKER_SHUTDOWN_TIMEOUT_S
        for coll_name, coll in [("transcribing", self._transcribing),
                                 ("analyzing", self._analyzing),
                                 ("rendering", self._rendering),
                                 ("publishing", self._publishing)]:
            pending = {t for t in coll if not t.done()}
            if pending:
                _logger.info("优雅关闭: 等待 {} 个 {} 任务完成 (最多 {}s)",
                            len(pending), coll_name, grace_period)
                try:
                    done, _ = await asyncio.wait(pending, timeout=grace_period)
                    _logger.info("优雅关闭: {} 任务正常完成", len(done))
                except TimeoutError:
                    pass
                # 取消仍未完成的任务
                still_running = {t for t in coll if not t.done()}
                for t in still_running:
                    _logger.warning("优雅关闭: 强制取消 {} 任务", coll_name)
                    t.cancel()

        _logger.info("TaskWorker stopped.")
        # V0.1.12.4: 清理孤儿子进程
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
                # V0.1.13: Disk protection — skip heavy tasks when disk is low
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
        # V0.1.13: ResourceBudget integration — map stages to resource keys
        _STAGE_TO_RESOURCE: dict[str, str] = {
            TaskStatus.QUEUED_FOR_TRANS: "asr",
            TaskStatus.QUEUED_FOR_ANALYSIS: "analysis",
            TaskStatus.QUEUED_FOR_RENDER: "render",
            TaskStatus.QUEUED_FOR_PUBLISH: "publish",
        }
        resource_key = _STAGE_TO_RESOURCE.get(queued_stage)
        while self._running and not _shutting_down and len(running) < max_concurrent:
            # V0.1.13: Check resource budget before claiming a task
            if resource_key:
                from app.core.resource_budget import acquire_resources, get_task_cost
                cost = get_task_cost(resource_key)
                if not acquire_resources(**cost):
                    _logger.debug(
                        "资源不足, 跳过阶段 {} (resource_key={})",
                        queued_stage, resource_key,
                    )
                    break
            else:
                cost = {}

            task = _pop_and_claim(queued_stage)
            if task is None:
                # Release resources if pop failed
                if cost:
                    from app.core.resource_budget import release_resources
                    release_resources(**cost)
                break

            # V0.1.13: Track resources per task for later release
            if cost:
                global _task_resources_lock
                import threading
                if _task_resources_lock is None:
                    _task_resources_lock = threading.Lock()
                with _task_resources_lock:
                    _task_resources[task.id] = cost

            active = _active_stage(queued_stage)
            lease = task.lease_token
            t = asyncio.create_task(asyncio.to_thread(_execute_task, task.id, active, lease))
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
        TaskStatus.RECORDED, TaskStatus.QUEUED_FOR_TRANS, TaskStatus.TRANSCRIBING,
        TaskStatus.QUEUED_FOR_ANALYSIS, TaskStatus.ANALYZING,
        TaskStatus.CANDIDATE_CREATED, TaskStatus.AWAITING_REVIEW,
        TaskStatus.APPROVED, TaskStatus.APPROVED_WAITING_RENDER,
        TaskStatus.QUEUED_FOR_RENDER, TaskStatus.RENDERING, TaskStatus.RENDERED,
        TaskStatus.AWAITING_PUBLISH_CONFIRMATION,
        TaskStatus.QUEUED_FOR_PUBLISH, TaskStatus.PUBLISHING,
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.STALE,
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
        "id": t.id, "segment_id": t.segment_id, "session_id": t.session_id,
        "candidate_id": t.candidate_id, "event_id": t.event_id, "clip_id": t.clip_id,
        "stage": t.stage, "failed_stage": t.failed_stage,
        "attempts": t.attempts, "max_retries": t.max_retries,
        "next_retry_at": t.next_retry_at.isoformat() if t.next_retry_at else None,
        "last_error": t.last_error, "error_is_permanent": t.error_is_permanent,
        "claimed_by": t.claimed_by,
        "claimed_at": t.claimed_at.isoformat() if t.claimed_at else None,
        "heartbeat_at": t.heartbeat_at.isoformat() if t.heartbeat_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "processing_time_ms": t.processing_time_ms, "total_elapsed_ms": t.total_elapsed_ms,
    }

# ── 全局单例 ──
task_worker: TaskWorker = TaskWorker()

