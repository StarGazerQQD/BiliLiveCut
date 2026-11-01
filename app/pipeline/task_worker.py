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

本文件保留 TaskWorker 主类、调度循环与任务生命周期入口。
"""

from __future__ import annotations

import asyncio
import threading
import time
from functools import partial
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlmodel import select

from app.core.config import settings
from app.core.execution import run_sync as _thread_io
from app.core.media_usage import media_reference_scope
from app.core.sanitize import safe_exception_summary
from app.core.supervisor import remaining_shutdown, shutdown_deadline, shutdown_scope
from app.db.entities import (
    RawSegment,
    RecordingSession,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session

# ── 从子模块导入 ────────────────────────────────────────────────
from app.pipeline.claiming import pop_and_claim, release_unstarted_claim
from app.pipeline.lifecycle import (
    _WORKER_ID,
    cleanup_subprocesses,
    shutdown_event,
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
)
from app.pipeline.stage_result import (
    active_stage,
    make_stage_key,
    request_cancellation,
)
from app.pipeline.stale_recovery import (
    recover_orphans,
    recover_stale,
    resume_stage,
)

# ── 并发配置 ──────────────────────────────────────────────────────


_logger = logger


if TYPE_CHECKING:
    from app.analysis.session_summary import SessionSummaryClaim

# ═══════════════════════════════════════════════════
# 任务生命周期 API
# ═══════════════════════════════════════════════════


@media_reference_scope()
def create_task(segment_id: int, session_id: int) -> SegmentTask | None:
    """为已完成录制的片段创建任务 (幂等)。

    :param segment_id: ``raw_segments`` 主键。
    :param session_id: ``recording_sessions`` 主键。
    :returns: 新创建的 SegmentTask; 幂等命中时返回 None。
    """
    from sqlalchemy.exc import IntegrityError

    from app.pipeline.stage_result import make_pipeline_key, make_stage_key

    pipeline_key = make_pipeline_key(segment_id)
    stage_key = make_stage_key(segment_id, "recorded")
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"无法为不存在的片段创建任务: segment={segment_id}")
        if segment.session_id != session_id:
            raise ValueError(
                f"任务来源不一致: segment={segment_id} 属于 session={segment.session_id},"
                f" 不能登记到 session={session_id}"
            )
        existing = db.exec(select(SegmentTask).where(SegmentTask.pipeline_key == pipeline_key)).first()
        if existing is not None:
            _logger.debug("pipeline_key 已存在: segment={} task={} stage={}", segment_id, existing.id, existing.stage)
            return None
        task = SegmentTask(
            segment_id=segment_id,
            session_id=session_id,
            stage=TaskStatus.RECORDED,
            pipeline_key=pipeline_key,
            stage_key=stage_key,
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


@media_reference_scope()
def retry_task(task_id: int) -> bool:
    """手动重试失败任务。

    :param task_id: SegmentTask ID。
    :returns: 是否成功。
    """
    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        task = db.get(SegmentTask, task_id)
        if task is None:
            return False
        if task.stage not in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TRANSIENT_FAILED):
            return False
        from app.pipeline.recovery_budget import reset_recovery

        reset_recovery(db, task, all_stages=True)
        res = resume_stage(task.failed_stage or task.stage)
        task.stage = res
        task.stage_key = make_stage_key(task.segment_id, res)
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
        from app.analysis.session_summary import request_session_timeline_summary_in_session

        if db.get(RecordingSession, task.session_id) is not None:
            request_session_timeline_summary_in_session(db, task.session_id, reason="task_retry", force=True)
        return True


def cancel_task(task_id: int) -> bool:
    """请求取消任务，活跃执行完成收尾后才进入已取消状态。

    :param task_id: SegmentTask ID。
    :returns: 是否成功。
    """
    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        task = db.get(SegmentTask, task_id)
        if task is None:
            return False
        if task.stage == TaskStatus.CANCELLING:
            return True
        if not request_cancellation(task):
            return False
        db.add(task)
        from app.analysis.session_summary import request_session_timeline_summary_in_session

        if db.get(RecordingSession, task.session_id) is not None:
            request_session_timeline_summary_in_session(db, task.session_id, reason="task_cancelled", force=True)
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
        "processing_time_ms": t.processing_time_ms,
        "total_elapsed_ms": t.total_elapsed_ms,
        "claimed_by": t.claimed_by,
        "candidate_id": t.candidate_id,
        "event_id": t.event_id,
        "clip_id": t.clip_id,
    }


# ═══════════════════════════════════════════════════
# TaskWorker
# ═══════════════════════════════════════════════════


def _observe_execution(future: asyncio.Task[None]) -> None:
    """读取每个后台 Future 的异常，持久化失败也不能成为未观察的线程错误。"""
    if future.cancelled():
        _logger.warning("worker_execution_cancelled task={}", future.get_name())
    elif (exc := future.exception()) is not None:
        _logger.opt(exception=exc).error(
            "worker_execution_failed task={} error={}", future.get_name(), safe_exception_summary(exc)
        )


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
        self._session_summaries: set[asyncio.Task[bool]] = set()
        self._summary_claims: dict[asyncio.Task[bool], SessionSummaryClaim] = {}
        self._main_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._preload_task: asyncio.Task[None] | None = None
        self._running: bool = False
        self._next_summary_scan = 0.0
        self._summary_scan_cursor = 0
        self._next_maintenance = time.monotonic() + 3600
        _logger.info("TaskWorker init worker_id={}", _WORKER_ID)

    async def start(self) -> None:
        """启动 Worker 主循环。"""
        if self._running:
            return
        if any(not task.done() for task in self._executions()):
            raise RuntimeError("Worker 上一次执行尚未物理退出，不能重新启动")
        from app.analysis.transcription.process_pool import reset_process_pool

        reset_process_pool()
        shutdown_event.clear()
        self._running = True
        from app.pipeline.publish_recovery import recover_publish_state

        await _thread_io(recover_publish_state)
        await _thread_io(recover_orphans)
        from app.pipeline.stale_recovery import recover_pending_clips

        await _thread_io(recover_pending_clips)
        from app.analysis.session_summary import recover_running_session_summary_requests

        await _thread_io(recover_running_session_summary_requests)
        self._main_task = asyncio.create_task(self._loop())
        if settings.asr_preload_on_start:
            self._preload_task = asyncio.create_task(self._preload_models())
            self._preload_task.add_done_callback(_observe_execution)
        from app.core.settings_store import asr_task_max_concurrency

        _logger.info(
            "TaskWorker started T{}/A{}/R{}", asr_task_max_concurrency(), settings.max_analyzing, settings.max_rendering
        )

    async def _preload_models(self) -> None:
        """后台预加载失败只记录警告，正常转写仍使用已有回退机制。"""
        from app.analysis.transcription.isolated import preload_isolated_models

        try:
            await _thread_io(preload_isolated_models)
        except (RuntimeError, OSError, ValueError, ImportError) as exc:
            _logger.warning("ASR 预加载失败，任务仍可按配置尝试加载或回退: {}", safe_exception_summary(exc, limit=500))

    def request_stop(self) -> None:
        """只广播停止意图，不在广播期间访问数据库或等待执行。"""
        shutdown_event.set()
        self._running = False
        if self._main_task is not None:
            self._main_task.cancel()

    async def stop(self) -> None:
        """在共同期限内观察真实收尾，超时保留清理执行和业务占位。"""
        self.request_stop()
        with shutdown_scope(settings.worker_shutdown_timeout_seconds):
            if self._stop_task is None or self._stop_task.done():
                self._stop_task = asyncio.create_task(self._finish_stop(), name="worker-cleanup")
                self._stop_task.add_done_callback(_observe_execution)
            task = self._stop_task
            _, pending = await asyncio.wait(
                {task}, timeout=remaining_shutdown(settings.worker_shutdown_timeout_seconds)
            )
            if pending:
                raise ExceptionGroup(
                    "Worker 关闭期间发现异常", [RuntimeError("Worker 执行尚未物理退出，保留资源占用和清理登记")]
                )
            task.result()

    async def _finish_stop(self) -> None:
        """保留真实执行句柄直至数据库、模型与媒体资源完成清理。"""
        deadline = shutdown_deadline(settings.worker_shutdown_timeout_seconds)
        errors: list[Exception] = []
        unfinished: set[asyncio.Task[None] | asyncio.Task[bool]] = set()

        if self._main_task is not None:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # 关闭边界：主循环故障也必须继续释放模型和媒体资源。
                errors.append(exc)
                _logger.opt(exception=exc).error("Worker 主循环异常，继续资源清理")

        grace_period = settings.worker_shutdown_timeout_seconds
        from app.analysis.transcription.process_pool import get_process_pool

        try:
            await _thread_io(partial(get_process_pool().shutdown, grace_period))
        except (RuntimeError, OSError, asyncio.CancelledError):
            # 进程终止异常也必须撤销迟到计算的写入权，但不能声称原生进程已退出。
            from app.analysis.session_summary import interrupt_summary_execution
            from app.pipeline.stale_recovery import release_stopped_computations

            try:
                await asyncio.to_thread(release_stopped_computations, _WORKER_ID)
                for claim in self._summary_claims.values():
                    await asyncio.to_thread(interrupt_summary_execution, claim)
            finally:
                await asyncio.to_thread(cleanup_subprocesses)
            raise
        preload = getattr(self, "_preload_task", None)
        if preload is not None and not preload.done():
            _, pending = await asyncio.wait({preload}, timeout=max(0, deadline - time.monotonic()))
            if pending:
                unfinished.update(pending)
        for coll_name, coll in [
            ("transcribing", self._transcribing),
            ("analyzing", self._analyzing),
            ("rendering", self._rendering),
            ("publishing", self._publishing),
            ("session_summaries", self._session_summaries),
        ]:
            pending = {t for t in coll if not t.done()}
            if pending:
                _logger.info("优雅关闭: 等待 {} 个 {} 任务完成 (最多 {}s)", len(pending), coll_name, grace_period)
                try:
                    done, _ = await asyncio.wait(pending, timeout=max(0, deadline - time.monotonic()))
                    _logger.info("优雅关闭: {} 任务正常完成", len(done))
                except TimeoutError:
                    pass
                still_running = {t for t in coll if not t.done()}
                if still_running and coll_name in {"transcribing", "analyzing"}:
                    from app.pipeline.stale_recovery import release_stopped_computations

                    await asyncio.to_thread(release_stopped_computations, _WORKER_ID)
                if still_running and coll_name == "session_summaries":
                    from app.analysis.session_summary import interrupt_summary_execution

                    for future in still_running:
                        claim = self._summary_claims.get(future)
                        if claim is not None:
                            await asyncio.to_thread(interrupt_summary_execution, claim)
                if still_running:
                    unfinished.update(still_running)

        if unfinished:
            # 调用方已在总期限报告未完成；清理句柄继续保留，直到真实执行归还。
            await asyncio.wait(unfinished)
        await asyncio.to_thread(cleanup_subprocesses)
        if errors:
            raise ExceptionGroup("Worker 关闭期间发现异常", errors)
        _logger.info("TaskWorker stopped.")

    def _executions(self) -> tuple[asyncio.Task[None] | asyncio.Task[bool], ...]:
        tasks = (
            *self._transcribing,
            *self._analyzing,
            *self._rendering,
            *self._publishing,
            *self._session_summaries,
        )
        return (*tasks, *(task for task in (self._main_task, self._preload_task, self._stop_task) if task is not None))

    async def _loop(self) -> None:
        """主调度循环 — 每个 tick 执行阶段推进、stale 恢复、任务分发。"""
        while self._running and not shutdown_event.is_set():
            try:
                from app.analysis.reanalysis import process_pending_session_reanalyses
                from app.analysis.transcription.process_pool import get_process_pool
                from app.core.configuration import reload_configuration
                from app.pipeline.publish_recovery import recover_publish_state

                await _thread_io(reload_configuration)
                await _thread_io(get_process_pool().cleanup_idle)
                if time.monotonic() >= self._next_maintenance:
                    from app.core.settings_store import get_bool
                    from app.pipeline.storage_lifecycle import run_disk_maintenance

                    self._next_maintenance = time.monotonic() + 3600
                    cleanup = await _thread_io(partial(get_bool, "storage_cleanup_enabled"))
                    await _thread_io(partial(run_disk_maintenance, cleanup=cleanup))
                for maintenance in (
                    recover_publish_state,
                    process_pending_session_reanalyses,
                    retry_expired,
                    recover_stale,
                    advance_recorded,
                    advance_transcribed,
                    advance_candidate,
                    advance_awaiting_review,
                    advance_approved,
                    advance_rendered,
                ):
                    await _thread_io(maintenance)
                if time.monotonic() >= self._next_summary_scan:
                    from app.analysis.session_summary import compensate_session_summaries

                    _, self._summary_scan_cursor = await _thread_io(
                        partial(compensate_session_summaries, after_id=self._summary_scan_cursor)
                    )
                    self._next_summary_scan = time.monotonic() + 60
                await self._dispatch_session_summaries()
                # V0.1.13: Disk protection
                from app.pipeline.storage_lifecycle import is_safe_for_new_tasks

                disk_safe = await _thread_io(is_safe_for_new_tasks)
                if disk_safe:
                    from app.core.settings_store import asr_task_max_concurrency

                    await self._dispatch(
                        TaskStatus.QUEUED_FOR_TRANS,
                        self._transcribing,
                        await _thread_io(asr_task_max_concurrency),
                    )
                    await self._dispatch(TaskStatus.QUEUED_FOR_ANALYSIS, self._analyzing, settings.max_analyzing)
                    await self._dispatch(TaskStatus.QUEUED_FOR_RENDER, self._rendering, settings.max_rendering)
                else:
                    _logger.warning("磁盘空间不足, 跳过 transcribe/analyze/render 调度")
                await self._dispatch(TaskStatus.QUEUED_FOR_PUBLISH, self._publishing, settings.max_publishing)
                self._transcribing = {t for t in self._transcribing if not t.done()}
                self._analyzing = {t for t in self._analyzing if not t.done()}
                self._rendering = {t for t in self._rendering if not t.done()}
                self._publishing = {t for t in self._publishing if not t.done()}
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _logger.opt(exception=exc).warning("tick error: {}", safe_exception_summary(exc))
            await asyncio.sleep(2)

    async def _dispatch_session_summaries(self) -> None:
        """串行领取整场总结，避免与分段分析争用 LLM 和数据库写入。"""
        from app.analysis.session_summary import recover_failed_summary_execution

        for future in list(self._session_summaries):
            if not future.done():
                continue
            if future.cancelled():
                error = "整场总结执行被取消"
            else:
                exc = future.exception()
                error = safe_exception_summary(exc) if exc else None
            if error:
                claim = self._summary_claims[future]
                _logger.error(
                    "session_summary_execution_failed session={} request={} error={}",
                    claim.session_id,
                    claim.request_id,
                    error,
                )
                # 数据库仍不可写时让 tick 重试恢复记录，不丢弃 claim 或重复执行 LLM。
                await asyncio.to_thread(recover_failed_summary_execution, claim, error)
            self._session_summaries.remove(future)
            self._summary_claims.pop(future, None)
        if self._session_summaries:
            return
        from app.analysis.session_summary import (
            claim_pending_session_summary,
            execute_session_summary_claim,
        )

        def return_unstarted(claim: SessionSummaryClaim | None) -> None:
            if claim is not None:
                from app.analysis.session_summary import interrupt_summary_execution

                interrupt_summary_execution(claim)

        claim = await _thread_io(claim_pending_session_summary, return_unstarted)
        if claim is None:
            return
        task = asyncio.create_task(
            _thread_io(partial(execute_session_summary_claim, claim)),
            name=f"summary:{claim.session_id}:{claim.request_id}",
        )
        self._session_summaries.add(task)
        self._summary_claims[task] = claim

    async def _dispatch(
        self,
        queued_stage: str,
        running: set[asyncio.Task[None]],
        max_concurrent: int,
    ) -> None:
        _STAGE_TO_RESOURCE: dict[str, str] = {
            TaskStatus.QUEUED_FOR_ANALYSIS: "analysis",
            TaskStatus.QUEUED_FOR_RENDER: "render",
            TaskStatus.QUEUED_FOR_PUBLISH: "publish",
        }
        resource_key = _STAGE_TO_RESOURCE.get(queued_stage)
        while self._running and not shutdown_event.is_set() and len(running) < max_concurrent:
            if resource_key:
                from app.core.resource_budget import acquire_resources, get_task_cost

                cost_spec = get_task_cost(resource_key)  # dict, e.g. {"cpu": 1, "memory_mb": 500}
                acquired = acquire_resources(**cost_spec)  # bool: True = acquired, False = insufficient
                if not acquired:
                    _logger.debug("资源不足, 跳过阶段 {} (resource_key={})", queued_stage, resource_key)
                    break
            else:
                cost_spec = {}
                acquired = True
            try:
                task = await _thread_io(
                    partial(pop_and_claim, queued_stage),
                    partial(release_unstarted_claim, queued_stage=queued_stage),
                )
            except BaseException:
                if acquired and cost_spec:
                    from app.core.resource_budget import release_resources

                    release_resources(**cost_spec)
                raise
            if task is None:
                if acquired and cost_spec:
                    from app.core.resource_budget import release_resources

                    release_resources(**cost_spec)
                break

            if acquired and cost_spec:
                from app.pipeline.lifecycle import _task_resources, _task_resources_lock

                if _task_resources_lock is None:
                    _task_resources_lock = threading.Lock()
                with _task_resources_lock:
                    _task_resources[(task.id, task.lease_token)] = cost_spec

            act_stage = active_stage(queued_stage)
            lease = task.lease_token
            t = asyncio.create_task(
                _thread_io(partial(execute_task, task.id, act_stage, lease)),
                name=f"task:{task.id}:segment:{task.segment_id}:{act_stage}:lease:{lease}",
            )
            t.add_done_callback(_observe_execution)
            running.add(t)

    @property
    def stats(self) -> dict[str, Any]:
        """当前 Worker 和任务队列统计。"""
        from app.core.settings_store import asr_task_max_concurrency

        counts = task_counts()
        counts["worker"] = {
            "worker_id": _WORKER_ID,
            "transcribing": len(self._transcribing),
            "transcribing_limit": asr_task_max_concurrency(),
            "analyzing": len(self._analyzing),
            "rendering": len(self._rendering),
            "publishing": len(self._publishing),
        }
        return counts


# 全局单例
_app: TaskWorker | None = None


def get_worker() -> TaskWorker:
    """获取全局 TaskWorker 单例。

    :returns: TaskWorker 实例。
    """
    global _app
    if _app is None:
        _app = TaskWorker()
    return _app
