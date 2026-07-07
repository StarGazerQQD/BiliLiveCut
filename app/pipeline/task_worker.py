"""持久化任务队列 Worker (v模块拆分)。

核心职责已按模块拆分:
  - claiming.py      — 原子任务领取
  - heartbeat.py     — 心跳线程管理
  - stale_recovery.py — Stale 任务恢复
  - lifecycle.py     — 全局状态、子进程/资源跟踪
  - scheduler.py     — 阶段推进、重试、任务执行
  - stage_result.py  — 状态转换、幂等键、任务标记
  - lease.py         — TaskLease / LeaseLostError / still_owns_lease
  - workers/         — 各阶段 compute / commit / run 实现

本文件保留: TaskWorker 主类、调度循环、任务生命周期入口、兼容门面。
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from loguru import logger
from sqlmodel import select

from app.db.models import (
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session

# ── 从子模块导入 ────────────────────────────────────────────────
from app.pipeline.claiming import pop_and_claim
from app.pipeline.heartbeat import clear_heartbeat_if_own, start_heartbeat_thread
from app.pipeline.lifecycle import (
    _WORKER_ID,
    cleanup_subprocesses,
    now_utc,
)
from app.pipeline.lifecycle import (
    _shutting_down as _shutting_down,
)
from app.pipeline.scheduler import (
    advance_approved,
    advance_awaiting_review,
    advance_candidate,
    advance_recorded,
    advance_rendered,
    advance_transcribed,
    execute_task,
    retry_expired,
    room_cfg_from_task,
)
from app.pipeline.stage_result import (
    active_stage,
    enqueue_next,
    mark_failed,
)
from app.pipeline.stale_recovery import (
    recover_orphans,
    recover_stale,
    resume_stage,
)

# ── 并发配置 ──────────────────────────────────────────────────────
MAX_TRANSCRIBING: int = int(os.environ.get("MAX_TRANSCRIBING", "1"))
MAX_ANALYZING: int = int(os.environ.get("MAX_ANALYZING", "2"))
MAX_RENDERING: int = int(os.environ.get("MAX_RENDERING", "2"))
MAX_PUBLISHING: int = int(os.environ.get("MAX_PUBLISHING", "1"))

_WORKER_SHUTDOWN_TIMEOUT_S: int = int(os.environ.get("WORKER_SHUTDOWN_TIMEOUT_SECONDS", "30"))

_HEARTBEAT_INTERVAL_S: int = 30
_STALE_TIMEOUT_S: int = 120

_logger = logger

# ═══════════════════════════════════════════════════
# 任务生命周期 API
# ═══════════════════════════════════════════════════


def create_task(segment_id: int, session_id: int) -> SegmentTask | None:
    """为已完成录制的片段创建任务 (幂等)。

    :param segment_id: ``raw_segments`` 主键。
    :param session_id: ``recording_sessions`` 主键。
    :returns: 新创建的 SegmentTask; 幂等命中时返回 None。
    """
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


def retry_task(task_id: int) -> bool:
    """手动重试失败任务。

    :param task_id: SegmentTask ID。
    :returns: 是否成功。
    """
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return False
        if task.stage not in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TRANSIENT_FAILED):
            return False
        res = resume_stage(task.failed_stage or task.stage)
        task.stage = res
        task.attempts = 0
        task.last_error = None
        task.error_is_permanent = False
        task.next_retry_at = None
        task.claimed_by = None
        task.claimed_at = None
        task.heartbeat_at = None
        task.lease_token = None
        task.completed_at = None
        db.add(task)
        return True


def cancel_task(task_id: int) -> bool:
    """取消任务。

    :param task_id: SegmentTask ID。
    :returns: 是否成功。
    """
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return False
        enqueue_next(task, TaskStatus.CANCELLED)
        db.add(task)
        return True


def task_counts() -> dict[str, int]:
    """统计各阶段任务数量。

    :returns: stage → count 字典。
    """
    with get_session() as db:
        rows = db.exec(select(SegmentTask.stage, SegmentTask.id)).all()
        counts: dict[str, int] = {}
        for stage, _ in rows:
            counts[stage] = counts.get(stage, 0) + 1
        return counts


def list_tasks(limit: int = 50, stage: str | None = None) -> list[dict[str, Any]]:
    """查询任务列表。

    :param limit: 返回最大条数。
    :param stage: 可选按阶段过滤。
    :returns: 任务字典列表。
    """
    with get_session() as db:
        q = select(SegmentTask).order_by(SegmentTask.created_at.desc())
        if stage:
            q = q.where(SegmentTask.stage == stage)
        tasks = db.exec(q.limit(limit)).all()
        return [_task_to_dict(t) for t in tasks]


def _task_to_dict(t: SegmentTask) -> dict[str, Any]:
    """将 SegmentTask 序列化为字典。"""
    return {
        "id": t.id,
        "segment_id": t.segment_id,
        "session_id": t.session_id,
        "stage": t.stage,
        "attempts": t.attempts,
        "max_retries": t.max_retries,
        "last_error": t.last_error,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "claimed_by": t.claimed_by,
        "candidate_id": t.candidate_id,
        "event_id": t.event_id,
        "clip_id": t.clip_id,
    }


# ═══════════════════════════════════════════════════
# TaskWorker
# ═══════════════════════════════════════════════════


class TaskWorker:
    """持久化任务队列 Worker, 支持各阶段真正并发。

    对外入口: start() → 启动调度循环; stop() → 优雅关闭。
    内部调度循环调用 scheduler/claiming/heartbeat/stale_recovery 子模块。
    """

    def __init__(self) -> None:
        self._transcribing: set[asyncio.Task[None]] = set()
        self._analyzing: set[asyncio.Task[None]] = set()
        self._rendering: set[asyncio.Task[None]] = set()
        self._publishing: set[asyncio.Task[None]] = set()
        self._main_task: asyncio.Task[None] | None = None
        self._running: bool = False
        _logger.info("TaskWorker init worker_id={}", _WORKER_ID)

    async def start(self) -> None:
        """启动 Worker 主循环。"""
        if self._running:
            return
        global _shutting_down
        _shutting_down = False
        self._running = True
        recover_orphans()
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
        cleanup_subprocesses()

    async def _loop(self) -> None:
        """主调度循环 — 每个 tick 执行阶段推进、stale 恢复、任务分发。"""
        while self._running and not _shutting_down:
            try:
                retry_expired()
                recover_stale()
                advance_recorded()
                advance_transcribed()
                advance_candidate()
                advance_awaiting_review()
                advance_approved()
                advance_rendered()
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
            task = pop_and_claim(queued_stage)
            if task is None:
                if cost:
                    from app.core.resource_budget import release_resources

                    release_resources(**cost)
                break

            if cost:
                from app.pipeline.lifecycle import _task_resources, _task_resources_lock

                if _task_resources_lock is None:
                    _task_resources_lock = threading.Lock()
                with _task_resources_lock:
                    _task_resources[task.id] = cost

            act_stage = active_stage(queued_stage)
            lease = task.lease_token
            t = asyncio.create_task(asyncio.to_thread(execute_task, task.id, act_stage, lease))
            running.add(t)

    @property
    def stats(self) -> dict[str, Any]:
        """当前 Worker 和任务队列统计。"""
        counts = task_counts()
        counts["worker"] = {
            "worker_id": _WORKER_ID,
            "transcribing": len(self._transcribing),
            "analyzing": len(self._analyzing),
            "rendering": len(self._rendering),
            "publishing": len(self._publishing),
        }
        return counts


# ═══════════════════════════════════════════════════
# 后向兼容导出
# ═══════════════════════════════════════════════════

# 任务生命周期 API (可直接从原 task_worker 路径导入)
_task_counts = task_counts
_RETRY_BASE_S: int = 10
_RETRY_MAX_S: int = 600
_RETRY_JITTER_S: float = 5.0
_HEARTBEAT_POLL_S: int = 5

# 阶段推进 (旧名称兼容)
_room_cfg_from_task = room_cfg_from_task
_advance_recorded = advance_recorded
_advance_transcribed = advance_transcribed
_advance_candidate = advance_candidate
_advance_awaiting_review = advance_awaiting_review
_advance_approved = advance_approved
_advance_rendered = advance_rendered
_retry_expired = retry_expired

# 领取
_pop_and_claim = pop_and_claim

# 心跳
_start_heartbeat_thread = start_heartbeat_thread
_clear_heartbeat_if_own = clear_heartbeat_if_own

# 恢复
_resume_stage = resume_stage
_recover_stale = recover_stale
_recover_orphans = recover_orphans

# 生命周期
_now = now_utc
_cleanup_subprocesses = cleanup_subprocesses

# 执行
_execute_task = execute_task

# stage_result 兼容 (被测试和外部引用)
from app.pipeline.stage_result import (  # noqa: E402, F401, I001
    can_transition,
    make_idempotency_key,
    make_pipeline_key,
    make_stage_key,
    mark_active,
    mark_failed as _mark_failed_for_export,
)
from app.pipeline.workers.analyze import _ensure_event  # noqa: E402, F401, I001

# 后向兼容: 旧名称
_can_transition = can_transition
_make_idempotency_key = make_idempotency_key
_make_pipeline_key = make_pipeline_key
_make_stage_key = make_stage_key

# old mark_failed exports for backward compat
mark_failed = _mark_failed_for_export  # noqa: F811

# 全局单例
_app: TaskWorker | None = None
task_worker: TaskWorker | None = None  # 后向兼容: 模块级实例


def get_worker() -> TaskWorker:
    """获取全局 TaskWorker 单例。

    :returns: TaskWorker 实例。
    """
    global _app
    if _app is None:
        _app = TaskWorker()
    return _app


# 后向兼容: 模块级实例引用 get_worker 的返回值
def _ensure_instance() -> TaskWorker:
    global task_worker
    if task_worker is None:
        task_worker = get_worker()
    return task_worker


task_worker = _ensure_instance()
