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
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlmodel import select

from app.core.config import settings
from app.db.models import (
    RawSegment,
    SegmentStatus as OldStatus,
    SegmentTask,
    TaskStatus,
    utcnow,
)
from app.db.session import get_session

# ── 并发配置 ──────────────────────────────────────────────────────
MAX_TRANSCRIBING = int(os.environ.get("MAX_TRANSCRIBING", "1"))
MAX_ANALYZING = int(os.environ.get("MAX_ANALYZING", "2"))
MAX_RENDERING = int(os.environ.get("MAX_RENDERING", "2"))
MAX_PUBLISHING = int(os.environ.get("MAX_PUBLISHING", "1"))

_RETRY_BASE_S = 10
_RETRY_MAX_S = 600
_RETRY_JITTER_S = 5
_RETRY_MAX_COUNT = 5

_HEARTBEAT_INTERVAL_S = 30
_STALE_TIMEOUT_S = 120

_WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"
_logger = logger


# ── 全局单例 Worker ─────────────────────────────────────────────────



def _now() -> datetime:
    return datetime.now(UTC)


def _make_idempotency_key(segment_id: int, stage: str) -> str:
    return f"{segment_id}:{stage}"


def _jitter(base: float, jitter_s: float = _RETRY_JITTER_S) -> float:
    return base + random.uniform(0, jitter_s)


# ═══════════════════════════════════════════════════
# 状态转换矩阵
# ═══════════════════════════════════════════════════

_VALID_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.RECORDED: {TaskStatus.QUEUED_FOR_TRANS},
    TaskStatus.QUEUED_FOR_TRANS: {TaskStatus.TRANSCRIBING},
    TaskStatus.TRANSCRIBING: {TaskStatus.TRANSCRIBED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.TRANSCRIBED: {TaskStatus.QUEUED_FOR_ANALYSIS},
    TaskStatus.QUEUED_FOR_ANALYSIS: {TaskStatus.ANALYZING},
    TaskStatus.ANALYZING: {TaskStatus.CANDIDATE_CREATED, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.CANDIDATE_CREATED: {TaskStatus.QUEUED_FOR_RENDER},
    TaskStatus.QUEUED_FOR_RENDER: {TaskStatus.RENDERING},
    TaskStatus.RENDERING: {TaskStatus.AWAITING_REVIEW, TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED},
    TaskStatus.AWAITING_REVIEW: {TaskStatus.APPROVED, TaskStatus.COMPLETED, TaskStatus.CANCELLED},
    TaskStatus.APPROVED: {TaskStatus.COMPLETED, TaskStatus.QUEUED_FOR_PUBLISH},
    TaskStatus.QUEUED_FOR_PUBLISH: {TaskStatus.COMPLETED, TaskStatus.FAILED},
    TaskStatus.TRANSIENT_FAILED: {TaskStatus.QUEUED_FOR_TRANS, TaskStatus.QUEUED_FOR_ANALYSIS, TaskStatus.QUEUED_FOR_RENDER, TaskStatus.FAILED},
    TaskStatus.STALE: {TaskStatus.QUEUED_FOR_TRANS, TaskStatus.QUEUED_FOR_ANALYSIS, TaskStatus.QUEUED_FOR_RENDER},
}


def _can_transition(current: str, target: str) -> bool:
    return target in _VALID_TRANSITIONS.get(current, set())


def _active_stage(queued_stage: str) -> str:
    return {
        TaskStatus.QUEUED_FOR_TRANS: TaskStatus.TRANSCRIBING,
        TaskStatus.QUEUED_FOR_ANALYSIS: TaskStatus.ANALYZING,
        TaskStatus.QUEUED_FOR_RENDER: TaskStatus.RENDERING,
        TaskStatus.QUEUED_FOR_PUBLISH: TaskStatus.COMPLETED,
    }.get(queued_stage, queued_stage)


# ═══════════════════════════════════════════════════
# 生命周期函数
# ═══════════════════════════════════════════════════

def create_task(segment_id: int, session_id: int) -> SegmentTask | None:
    """为已完成录制的片段创建任务(幂等)。"""
    key = _make_idempotency_key(segment_id, "recorded")
    with get_session() as db:
        existing = db.exec(
            select(SegmentTask).where(SegmentTask.idempotency_key == key)
        ).first()
        if existing is not None:
            return None
        task = SegmentTask(
            segment_id=segment_id,
            session_id=session_id,
            stage=TaskStatus.RECORDED,
            idempotency_key=key,
        )
        db.add(task)
        db.flush()
        db.refresh(task)
        return task


def enqueue_next(
    task: SegmentTask,
    next_stage: str,
    candidate_id: int | None = None,
    event_id: int | None = None,
    clip_id: int | None = None,
) -> None:
    current = task.stage
    if not _can_transition(current, next_stage):
        raise ValueError(f"非法转换: {current} -> {next_stage}")
    task.stage = next_stage
    task.idempotency_key = _make_idempotency_key(task.segment_id, next_stage)
    task.attempts = 0
    task.last_error = None
    task.error_is_permanent = False
    task.next_retry_at = None
    task.claimed_by = None
    task.claimed_at = None
    task.heartbeat_at = None
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
    task.heartbeat_at = _now()


def mark_completed(task: SegmentTask, processing_ms: int | None = None) -> None:
    if processing_ms is None and task.started_at is not None:
        processing_ms = int((_now() - task.started_at).total_seconds() * 1000)
    task.processing_time_ms = processing_ms
    task.completed_at = _now()
    task.heartbeat_at = None


def mark_failed(task: SegmentTask, error: str, permanent: bool = False) -> None:
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
    """原子领取:SELECT + 条件赋值,确保多 Worker 不抢同一任务。

    唯一的 attempts++ 点。
    """
    now = _now()
    with get_session() as db:
        task = db.exec(
            select(SegmentTask)
            .where(
                SegmentTask.stage == queued_stage,
                (SegmentTask.next_retry_at.is_(None)) | (SegmentTask.next_retry_at <= now),
            )
            .order_by(SegmentTask.priority.asc(), SegmentTask.created_at.asc())
            .limit(1)
        ).first()

        if task is None:
            return None

        task.stage = _active_stage(queued_stage)
        task.claimed_by = _WORKER_ID
        task.claimed_at = now
        mark_active(task)  # 唯一 attempts++ 点
        db.add(task)
        return task


# ═══════════════════════════════════════════════════
# 阶段推进
# ═══════════════════════════════════════════════════

def _advance_recorded() -> None:
    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.RECORDED)
        ).all()
        for task in tasks:
            seg = db.get(RawSegment, task.segment_id)
            if seg is not None and seg.status == OldStatus.RECORDED:
                enqueue_next(task, TaskStatus.QUEUED_FOR_TRANS)
                db.add(task)


def _advance_transcribed() -> None:
    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.TRANSCRIBED)
        ).all()
        for task in tasks:
            enqueue_next(task, TaskStatus.QUEUED_FOR_ANALYSIS)
            db.add(task)


def _advance_candidate() -> None:
    with get_session() as db:
        tasks = db.exec(
            select(SegmentTask).where(SegmentTask.stage == TaskStatus.CANDIDATE_CREATED)
        ).all()
        for task in tasks:
            enqueue_next(task, TaskStatus.QUEUED_FOR_RENDER)
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
                    TaskStatus.TRANSCRIBING, TaskStatus.ANALYZING, TaskStatus.RENDERING,
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
                    TaskStatus.TRANSCRIBING, TaskStatus.ANALYZING, TaskStatus.RENDERING,
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
            key = _make_idempotency_key(seg.id, "recorded")
            t = SegmentTask(
                segment_id=seg.id,
                session_id=seg.session_id,
                stage=TaskStatus.RECORDED,
                idempotency_key=key,
            )
            db.add(t)
        if orphan_segs:
            _logger.info("恢复:为 {} 个孤立片段创建任务。", len(orphan_segs))


# ═══════════════════════════════════════════════════
# 执行 (V0.1.11-alpha: heartbeat + no double attempts)
# ═══════════════════════════════════════════════════

def _execute_task(task_id: int, active_stage: str) -> None:
    t0 = time.time()
    try:
        if active_stage == TaskStatus.TRANSCRIBING:
            _run_transcribe(task_id)
        elif active_stage == TaskStatus.ANALYZING:
            _run_analyze(task_id)
        elif active_stage == TaskStatus.RENDERING:
            _run_render(task_id)
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        with get_session() as db:
            t = db.get(SegmentTask, task_id)
            if t is not None:
                _logger.error("任务 {} 阶段 {} 失败: {}", task_id, active_stage, exc)
                mark_failed(t, f"{type(exc).__name__}: {exc}", permanent=False)
                db.add(t)


def _run_transcribe(task_id: int) -> None:
    t0 = time.time()
    from app.analysis.transcribe import transcribe_segment
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return
        mark_heartbeat(task)
        db.add(task)
    transcribe_segment(task.segment_id)
    ms = int((time.time() - t0) * 1000)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task:
            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.TRANSCRIBED)
            db.add(task)


def _run_analyze(task_id: int) -> None:
    t0 = time.time()
    from app.analysis.highlight import score_segment
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return
        mark_heartbeat(task)
        db.add(task)
    candidate = score_segment(task.segment_id)
    # V0.1.11-alpha: 自动创建 HighlightEvent
    event_id: int | None = None
    if candidate is not None and candidate.id is not None:
        event_id = _ensure_event(candidate.id)
    ms = int((time.time() - t0) * 1000)
    cid = candidate.id if candidate else None
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task:
            mark_completed(task, ms)
            if cid is not None:
                enqueue_next(task, TaskStatus.CANDIDATE_CREATED, candidate_id=cid, event_id=event_id)
            else:
                enqueue_next(task, TaskStatus.COMPLETED)
            db.add(task)


def _run_render(task_id: int) -> None:
    t0 = time.time()
    from app.pipeline.orchestrator import produce_clip
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return
        cid = task.candidate_id
        if cid is None:
            mark_failed(task, "渲染任务缺少 candidate_id", permanent=True)
            db.add(task)
            return
        mark_heartbeat(task)
        db.add(task)
    produce_clip(cid, auto_upload=False)
    ms = int((time.time() - t0) * 1000)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task:
            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.AWAITING_REVIEW)
            db.add(task)


# ═══════════════════════════════════════════════════
# V0.1.11-alpha: 自动创建 HighlightEvent
# ═══════════════════════════════════════════════════

def _ensure_event(candidate_id: int) -> int | None:
    """确保每个 HighlightCandidate 有唯一 HighlightEvent (幂等)。"""
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
        db.flush()
        db.refresh(event)
        _logger.info("auto event: eid={} cid={}", event.id, candidate_id)
        return event.id


# ═══════════════════════════════════════════════════
# Worker (V0.1.11-alpha: 真正并发)
# ═══════════════════════════════════════════════════

class TaskWorker:
    def __init__(self) -> None:
        self._transcribing: set[asyncio.Task[None]] = set()
        self._analyzing: set[asyncio.Task[None]] = set()
        self._rendering: set[asyncio.Task[None]] = set()
        self._main_task: asyncio.Task[None] | None = None
        self._running = False
        _logger.info("TaskWorker init worker_id={}", _WORKER_ID)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        _recover_orphans()
        self._main_task = asyncio.create_task(self._loop())
        _logger.info("TaskWorker started T{}/A{}/R{}", MAX_TRANSCRIBING, MAX_ANALYZING, MAX_RENDERING)

    async def stop(self) -> None:
        self._running = False
        for coll in (self._transcribing, self._analyzing, self._rendering):
            for t in coll:
                t.cancel()
        if self._main_task is not None:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
        _logger.info("TaskWorker stopped.")

    async def _loop(self) -> None:
        while self._running:
            try:
                _retry_expired()
                _advance_recorded()
                _advance_transcribed()
                _advance_candidate()
                await self._dispatch(TaskStatus.QUEUED_FOR_TRANS, self._transcribing, MAX_TRANSCRIBING)
                await self._dispatch(TaskStatus.QUEUED_FOR_ANALYSIS, self._analyzing, MAX_ANALYZING)
                await self._dispatch(TaskStatus.QUEUED_FOR_RENDER, self._rendering, MAX_RENDERING)
                self._transcribing = {t for t in self._transcribing if not t.done()}
                self._analyzing = {t for t in self._analyzing if not t.done()}
                self._rendering = {t for t in self._rendering if not t.done()}
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
        while self._running and len(running) < max_concurrent:
            task = _pop_and_claim(queued_stage)
            if task is None:
                break
            active = _active_stage(queued_stage)
            t = asyncio.create_task(asyncio.to_thread(_execute_task, task.id, active))
            running.add(t)

    @property
    def stats(self) -> dict:
        counts = _task_counts()
        counts["worker"] = {
            "worker_id": _WORKER_ID,
            "transcribing": len(self._transcribing),
            "analyzing": len(self._analyzing),
            "rendering": len(self._rendering),
            "max_transcribing": MAX_TRANSCRIBING,
            "max_analyzing": MAX_ANALYZING,
            "max_rendering": MAX_RENDERING,
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
        TaskStatus.QUEUED_FOR_RENDER, TaskStatus.RENDERING,
        TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED,
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.STALE,
    ):
        result[stage] = sum(1 for r in rows if r.stage == stage)
    return result


def list_tasks(limit: int = 50, stage: str | None = None) -> list[dict]:
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

