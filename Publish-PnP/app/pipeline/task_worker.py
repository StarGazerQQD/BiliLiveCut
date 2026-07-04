"""持久化任务队列 Worker(V0.1.6)。

设计原则:
- 每个阶段独立执行、独立重试（不阻塞录制事件循环）
- GPU 转写（Whisper）和 FFmpeg 渲染分别控制并发数
- 崩溃恢复：应用重启后扫描未完成任务并恢复
- 幂等键防重复处理
- 区分临时失败（指数退避重试）与永久失败（标记 failed）
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections import Counter
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import or_
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


MAX_TRANSCRIBING = 1
MAX_RENDERING = 2
_RETRY_BASE_S = 10
_RETRY_MAX_S = 300


def _now() -> datetime:
    return datetime.now(UTC)


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
    TaskStatus.APPROVED: {TaskStatus.COMPLETED},
    TaskStatus.TRANSIENT_FAILED: {TaskStatus.QUEUED_FOR_TRANS, TaskStatus.QUEUED_FOR_ANALYSIS, TaskStatus.QUEUED_FOR_RENDER, TaskStatus.FAILED},
}


_STAGES_NEEDING_WORKERS = {
    TaskStatus.QUEUED_FOR_TRANS,
    TaskStatus.QUEUED_FOR_ANALYSIS,
    TaskStatus.QUEUED_FOR_RENDER,
}


def _can_transition(current: str, target: str) -> bool:
    return target in _VALID_TRANSITIONS.get(current, set())


def _make_idempotency_key(segment_id: int, stage: str) -> str:
    return f"{segment_id}:{stage}"


def create_task(segment_id: int, session_id: int) -> SegmentTask | None:
    """为已完成录制的片段创建任务（幂等）。"""
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
    clip_id: int | None = None,
) -> None:
    """推进到下一阶段，自动设置幂等键并校验。"""
    current = task.stage
    if not _can_transition(current, next_stage):
        raise ValueError(f"非法转换: {current} → {next_stage}")
    task.stage = next_stage
    task.idempotency_key = _make_idempotency_key(task.segment_id, next_stage)
    task.attempts = 0
    task.last_error = None
    task.error_is_permanent = False
    task.next_retry_at = None
    if next_stage in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
        task.completed_at = _now()
        if task.created_at:
            task.total_elapsed_ms = int((_now() - task.created_at).total_seconds() * 1000)
    if candidate_id is not None:
        task.candidate_id = candidate_id
    if clip_id is not None:
        task.clip_id = clip_id


def mark_active(task: SegmentTask) -> None:
    task.attempts += 1
    task.started_at = _now()
    task.last_error = None


def mark_completed(task: SegmentTask, processing_ms: int | None = None) -> None:
    if processing_ms is None and task.started_at is not None:
        processing_ms = int((_now() - task.started_at).total_seconds() * 1000)
    task.processing_time_ms = processing_ms
    task.completed_at = _now()


def mark_failed(task: SegmentTask, error: str, permanent: bool = False) -> None:
    task.last_error = error[:1000]
    task.error_is_permanent = permanent
    if permanent:
        task.stage = TaskStatus.FAILED
        task.completed_at = _now()
        # H3: 任务永久失败时触发通知。
        try:
            from app.notify.webhook import notify_task_failed
            notify_task_failed(task.id, task.stage, error[:200])
        except Exception:
            pass
    else:
        delay = min(_RETRY_BASE_S * (2 ** (task.attempts - 1)), _RETRY_MAX_S) if task.attempts > 0 else _RETRY_BASE_S
        task.next_retry_at = _now() + timedelta(seconds=delay)
        task.stage = TaskStatus.TRANSIENT_FAILED


# ---- DB helpers ---- #

def _pop_one(stage: str) -> SegmentTask | None:
    now = _now()
    with get_session() as db:
        task = db.exec(
            select(SegmentTask)
            .where(
                SegmentTask.stage == stage,
                (SegmentTask.next_retry_at.is_(None)) | (SegmentTask.next_retry_at <= now),
            )
            .order_by(SegmentTask.priority.asc(), SegmentTask.created_at.asc())
            .limit(1)
        ).first()
        if task is not None:
            mark_active(task)
            db.add(task)
        return task


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
                key = task.idempotency_key or ""
                if "trans" in key:
                    task.stage = TaskStatus.QUEUED_FOR_TRANS
                elif "analysis" in key:
                    task.stage = TaskStatus.QUEUED_FOR_ANALYSIS
                elif "render" in key:
                    task.stage = TaskStatus.QUEUED_FOR_RENDER
                else:
                    task.stage = TaskStatus.QUEUED_FOR_TRANS
                task.next_retry_at = None
            db.add(task)


def _recover_orphans() -> None:
    with get_session() as db:
        # 仅回退超过 30 分钟的中间状态任务,避免误伤刚启动的任务。
        stale_cutoff = utcnow() - timedelta(minutes=30)
        stuck = db.exec(
            select(SegmentTask).where(
                SegmentTask.stage.in_([
                    TaskStatus.TRANSCRIBING,
                    TaskStatus.ANALYZING,
                    TaskStatus.RENDERING,
                ]),
                or_(
                    SegmentTask.started_at == None,
                    SegmentTask.started_at < stale_cutoff,
                ),
            )
        ).all()
        for task in stuck:
            if task.stage == TaskStatus.TRANSCRIBING:
                task.stage = TaskStatus.QUEUED_FOR_TRANS
            elif task.stage == TaskStatus.ANALYZING:
                task.stage = TaskStatus.QUEUED_FOR_ANALYSIS
            else:
                task.stage = TaskStatus.QUEUED_FOR_RENDER
            task.started_at = None
            task.next_retry_at = None
            db.add(task)
        if stuck:
            logger.info("崩溃恢复:回退 {} 个中间状态任务。", len(stuck))

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
            logger.info("崩溃恢复:为 {} 个孤立片段创建任务。", len(orphan_segs))


# ---- Worker ----

class TaskWorker:
    def __init__(self) -> None:
        self._transcribing = 0
        self._rendering = 0
        self._analyzing = 0
        self._task_ref: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        _recover_orphans()
        self._task_ref = asyncio.create_task(self._loop())
        logger.info("TaskWorker 已启动。")

    async def stop(self) -> None:
        self._running = False
        if self._task_ref is not None:
            self._task_ref.cancel()
            try:
                await self._task_ref
            except (asyncio.CancelledError, Exception):
                pass
            self._task_ref = None
        logger.info("TaskWorker 已停止。")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("TaskWorker tick 异常: {}", exc)
            await asyncio.sleep(3)

    async def _tick(self) -> None:
        _retry_expired()
        _advance_recorded()
        _advance_transcribed()
        _advance_candidate()

        if self._transcribing < MAX_TRANSCRIBING:
            await self._dispatch_one(TaskStatus.QUEUED_FOR_TRANS, "transcribing")
        if self._analyzing < 2:
            await self._dispatch_one(TaskStatus.QUEUED_FOR_ANALYSIS, "analyzing")
        if self._rendering < MAX_RENDERING:
            await self._dispatch_one(TaskStatus.QUEUED_FOR_RENDER, "rendering")

    async def _dispatch_one(self, queued_stage: str, kind: str) -> None:
        task = _pop_one(queued_stage)
        if task is None:
            return
        if kind == "transcribing":
            self._transcribing += 1
        elif kind == "analyzing":
            self._analyzing += 1
        elif kind == "rendering":
            self._rendering += 1
        try:
            await asyncio.to_thread(_execute_task, task.id, queued_stage)
        except Exception as exc:
            logger.error("任务 {} 执行异常: {}", task.id, exc)
        finally:
            if kind == "transcribing":
                self._transcribing -= 1
            elif kind == "analyzing":
                self._analyzing -= 1
            elif kind == "rendering":
                self._rendering -= 1

    def stats(self) -> dict:
        counts = _task_counts()
        counts["worker"] = {
            "transcribing": self._transcribing,
            "analyzing": self._analyzing,
            "rendering": self._rendering,
            "max_transcribing": MAX_TRANSCRIBING,
            "max_rendering": MAX_RENDERING,
        }
        return counts


def _execute_task(task_id: int, stage: str) -> None:
    t0 = time.time()
    try:
        if stage == TaskStatus.QUEUED_FOR_TRANS:
            _run_transcribe(task_id)
        elif stage == TaskStatus.QUEUED_FOR_ANALYSIS:
            _run_analyze(task_id)
        elif stage == TaskStatus.QUEUED_FOR_RENDER:
            _run_render(task_id)
    except Exception as exc:
        ms = int((time.time() - t0) * 1000)
        with get_session() as db:
            t = db.get(SegmentTask, task_id)
            if t is not None:
                mark_failed(t, f"{type(exc).__name__}: {exc}", permanent=False)
                db.add(t)


def _run_transcribe(task_id: int) -> None:
    t0 = time.time()
    from app.analysis.transcribe import transcribe_segment

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return
        mark_active(task)
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
        mark_active(task)
        db.add(task)

    candidate = score_segment(task.segment_id)

    ms = int((time.time() - t0) * 1000)
    cid = candidate.id if candidate else None
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task:
            mark_completed(task, ms)
            if cid is not None:
                enqueue_next(task, TaskStatus.CANDIDATE_CREATED, candidate_id=cid)
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
            task.stage = TaskStatus.FAILED
            task.last_error = "渲染任务缺少 candidate_id"
            task.error_is_permanent = True
            db.add(task)
            return
        mark_active(task)
        db.add(task)

    produce_clip(cid, auto_upload=False)

    ms = int((time.time() - t0) * 1000)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task:
            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.AWAITING_REVIEW)
            db.add(task)


def _task_counts() -> dict:
    with get_session() as db:
        rows = db.exec(select(SegmentTask.stage)).all()
    counter = Counter(r[0] for r in rows)
    result: dict = {"total": len(rows)}
    for stage in (
        TaskStatus.RECORDED, TaskStatus.QUEUED_FOR_TRANS, TaskStatus.TRANSCRIBING,
        TaskStatus.QUEUED_FOR_ANALYSIS, TaskStatus.ANALYZING,
        TaskStatus.QUEUED_FOR_RENDER, TaskStatus.RENDERING,
        TaskStatus.AWAITING_REVIEW, TaskStatus.APPROVED,
        TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    ):
        result[stage] = counter.get(stage, 0)
    return result


def list_tasks(limit: int = 50, stage: str | None = None) -> list[dict]:
    with get_session() as db:
        stmt = select(SegmentTask)
        if stage:
            stmt = stmt.where(SegmentTask.stage == stage)
        stmt = stmt.order_by(SegmentTask.created_at.desc()).limit(limit)
        tasks = db.exec(stmt).all()
    return [
        {
            "id": t.id, "segment_id": t.segment_id, "session_id": t.session_id,
            "candidate_id": t.candidate_id, "clip_id": t.clip_id,
            "stage": t.stage, "priority": t.priority,
            "attempts": t.attempts, "max_retries": t.max_retries,
            "last_error": t.last_error,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "processing_time_ms": t.processing_time_ms,
            "total_elapsed_ms": t.total_elapsed_ms,
        }
        for t in tasks
    ]


def retry_task(task_id: int) -> bool:
    with get_session() as db:
        t = db.get(SegmentTask, task_id)
        if t is None or t.stage not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False
        t.stage = TaskStatus.QUEUED_FOR_TRANS
        t.attempts = 0
        t.last_error = None
        t.error_is_permanent = False
        t.next_retry_at = None
        t.started_at = None
        t.completed_at = None
        t.processing_time_ms = None
        db.add(t)
    return True


def cancel_task(task_id: int) -> bool:
    with get_session() as db:
        t = db.get(SegmentTask, task_id)
        if t is None or t.stage in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False
        t.stage = TaskStatus.CANCELLED
        t.completed_at = _now()
        if t.created_at:
            t.total_elapsed_ms = int((_now() - t.created_at).total_seconds() * 1000)
        db.add(t)
    return True


task_worker = TaskWorker()
