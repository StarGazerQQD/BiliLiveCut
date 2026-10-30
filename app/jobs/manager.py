# ruff: noqa: D100
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from functools import partial
from uuid import uuid4

from loguru import logger
from pydantic import JsonValue
from sqlalchemy.exc import SQLAlchemyError

from app.core.cancellation import OperationCancelledError, cancellation_scope
from app.core.execution import run_sync, start_thread, wait_for_completion
from app.core.process_control import ProcessCancelledError
from app.core.runtime_settings import configured_entry
from app.core.sanitize import safe_exception_summary
from app.core.supervisor import remaining_shutdown, shutdown_scope
from app.db import job_store
from app.jobs.context import JobContext
from app.jobs.contracts import (
    FORMAT_VERSION,
    JobAttempt,
    JobCancelled,
    JobData,
    JobSuperseded,
    now_iso,
    validate_payload,
)

JobHandler = Callable[[JobContext, dict[str, JsonValue]], dict[str, JsonValue] | None]


class JobManager:
    """调度已注册的领域处理器，不决定业务结果或执行 SQL。"""

    def __init__(self, max_concurrency: int = 2) -> None:
        if max_concurrency < 1:
            raise ValueError("作业执行并发必须为正整数")
        self._max_concurrency = max_concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._cancellable_jobs: set[str] = set()
        self._stop_task: asyncio.Task[None] | None = None
        self._handlers: dict[str, JobHandler] = {}
        self._recoveries: dict[str, Callable[[JobAttempt], JobData]] = {}
        self._started = False
        self._stopping = False
        self._start_lock = asyncio.Lock()
        self._release_ownership: Callable[[], None] | None = None

    def register(
        self, job_type: str, handler: JobHandler, *, recover: Callable[[JobAttempt], JobData] | None = None
    ) -> None:
        """在组合入口注册业务处理器。"""
        self._handlers[job_type] = handler
        if recover is not None:
            self._recoveries[job_type] = recover
        else:
            self._recoveries.pop(job_type, None)

    async def start(self) -> None:
        """读取持久任务，清算中断状态并调度可恢复操作。"""
        if self._stopping and self._started:
            raise RuntimeError("Web 作业尚未确认关闭，不能重新启动")
        if self._started:
            return
        async with self._start_lock:
            if not self._started:
                await self._start()

    async def _start(self) -> None:
        release = job_store.acquire_execution()
        try:
            jobs = await run_sync(lambda: list(job_store.iter_active_jobs()))
            recovered = []
            for job in jobs:
                recover = self._recoveries.get(job["type"], job_store.recover)
                recovered.append(await run_sync(partial(recover, JobAttempt(job["id"], job["attempt"]))))
        except BaseException:
            release()
            raise
        self._release_ownership = release
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._stopping = False
        self._started = True
        for job in recovered:
            if job["status"] == "queued":
                self._schedule(job)

    def request_stop(self) -> None:
        """先停止接单并通知可取消执行，广播不等待数据库锁。"""
        self._stopping = True
        for job_id in tuple(self._cancellable_jobs):
            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()

    async def stop(self, *, timeout_s: float = 15.0) -> None:
        """总期限包含持久取消请求和处理器收尾，超时保留所有权。"""
        self.request_stop()
        with shutdown_scope(timeout_s):
            if self._stop_task is None or self._stop_task.done():
                self._stop_task = asyncio.create_task(self._finish_stop(timeout_s), name="web-jobs-cleanup")
                self._stop_task.add_done_callback(self._observe_stop)
            task = self._stop_task
            _, pending = await asyncio.wait({task}, timeout=remaining_shutdown(timeout_s))
            if pending:
                raise RuntimeError("Web 作业未确认退出，关闭未完成，保留执行所有权")
            task.result()

    @staticmethod
    def _observe_stop(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.opt(exception=error).error("Web 作业清理未完成")

    async def _finish_stop(self, timeout_s: float) -> None:
        async with self._start_lock:
            if not self._started:
                return
            self._stopping = True
        for job_id in tuple(self._tasks):
            job = await asyncio.to_thread(job_store.get_job, job_id)
            if job is None:
                continue
            if job["status"] in {"running", "cancelling"} and job["cancellable_while_running"]:
                try:
                    await asyncio.to_thread(self.cancel, job_id, job["owner"])
                except ValueError:
                    # 完成事务可先于停机请求；已完成的业务结果保持终态。
                    current = await asyncio.to_thread(job_store.get_job, job_id)
                    if current is None or current["status"] not in {"succeeded", "failed", "cancelled"}:
                        raise
        tasks = tuple(self._tasks.values())
        if tasks:
            # stop 的观察期限独立于物理收尾；超时后仍持有此句柄和数据库所有权。
            await asyncio.wait(tasks)
        self._tasks.clear()
        self._cancel_events.clear()
        self._cancellable_jobs.clear()
        if self._release_ownership is not None:
            self._release_ownership()
            self._release_ownership = None
        self._started = False
        self._start_lock = asyncio.Lock()

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, JsonValue],
        *,
        label: str,
        owner: str,
        dedup_key: str | None = None,
        cancellable_while_running: bool = True,
    ) -> JobData:
        """原子登记作业；重复请求仅在同一用户和操作内共享结果。"""
        if self._stopping:
            raise RuntimeError("Web 作业管理器正在关闭，不能接受新作业")
        if not self._started:
            await self.start()
        if job_type not in self._handlers:
            raise ValueError(f"未知 Web 作业类型: {job_type}")
        validate_payload(job_type, payload)
        now = now_iso()
        job: JobData = {
            "version": FORMAT_VERSION,
            "id": uuid4().hex,
            "type": job_type,
            "label": label,
            "owner": owner,
            "payload": payload,
            "dedup_key": dedup_key,
            "cancellable_while_running": cancellable_while_running,
            "status": "queued",
            "progress": 0,
            "message": "等待执行",
            "result": None,
            "error": None,
            "attempt": 1,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "recovered": False,
        }
        registered = await asyncio.to_thread(job_store.register, job)
        self._schedule(registered)
        return registered

    def cancel(self, job_id: str, actor: str, *, is_admin: bool = False) -> JobData:
        """事务内封锁结果提交后通知处理器退出。"""
        job = job_store.cancel(job_id, actor, is_admin=is_admin)
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
        return job

    async def retry(self, job_id: str, actor: str, *, is_admin: bool = False) -> JobData:
        """仅在前一个执行已经退出时开启新代次。"""
        asyncio.get_running_loop()
        if self._stopping:
            raise RuntimeError("Web 作业管理器正在关闭，不能重试")
        if not self._started:
            raise ValueError("作业管理器尚未启动")
        execution = self._tasks.get(job_id)
        if execution is not None and not execution.done():
            raise ValueError("上一次执行尚未退出，请等待资源释放后重试")
        job = await asyncio.to_thread(job_store.retry, job_id, actor, is_admin=is_admin)
        self._schedule(job)
        return job

    def _schedule(self, job: JobData) -> None:
        if self._stopping:
            return
        job_id = job["id"]
        if job_id in self._tasks and not self._tasks[job_id].done():
            return
        attempt = JobAttempt(job_id, job["attempt"])
        task = asyncio.create_task(self._execute(attempt), name=f"web-job-{job_id[:8]}")
        self._tasks[job_id] = task

        def remove_finished(finished: asyncio.Task[None]) -> None:
            if not finished.cancelled():
                error = finished.exception()
                if error is not None:
                    logger.opt(exception=error).error("作业执行或状态保存失败 job_id={}", job_id)
            if self._tasks.get(job_id) is finished:
                self._tasks.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._cancellable_jobs.discard(job_id)

        task.add_done_callback(remove_finished)

    async def _execute(self, attempt: JobAttempt) -> None:
        assert self._semaphore is not None
        async with self._semaphore:
            if self._stopping:
                return
            try:
                job = await run_sync(partial(job_store.claim, attempt), lambda _job: job_store.fail(attempt, None))
            except JobSuperseded:
                return
            except (OSError, SQLAlchemyError) as exc:
                await asyncio.to_thread(job_store.fail, attempt, safe_exception_summary(exc, limit=2000))
                raise
            if self._stopping:
                # 认领期间可能开始停机；处理器尚未启动，没有需要核对的外部效果。
                await asyncio.to_thread(job_store.fail, attempt, None)
                return
            handler = self._handlers.get(job["type"])
            if handler is None:
                await asyncio.to_thread(job_store.fail, attempt, "作业处理器不可用")
                return
            event = threading.Event()
            self._cancel_events[attempt.job_id] = event
            if job["cancellable_while_running"]:
                self._cancellable_jobs.add(attempt.job_id)
            context = JobContext(attempt.job_id, event, attempt.number)

            def execute() -> dict[str, JsonValue] | None:
                with cancellation_scope(context.cancelled):
                    context.check_cancelled()
                    return configured_entry(handler)(context, job["payload"])

            execution = start_thread(execute)
            # 等待结束后读取异常，领域异常由任务边界记录，状态写入异常继续向外传播。
            cancelled_wait = await wait_for_completion(
                execution, event.set if job["cancellable_while_running"] else None
            )
            error = execution.exception() if not execution.cancelled() else JobCancelled("处理器已取消")
            if error is not None:
                cancelled = isinstance(error, (JobCancelled, ProcessCancelledError)) or (
                    job["cancellable_while_running"] and isinstance(error, OperationCancelledError)
                )
                await asyncio.to_thread(
                    job_store.fail, attempt, None if cancelled else safe_exception_summary(error, limit=2000)
                )
                if not cancelled:
                    logger.opt(exception=error).error("作业处理器失败 job_id={}", attempt.job_id)
                return
            result = execution.result() or {}
            current = await asyncio.to_thread(job_store.get_job, attempt.job_id)
            if current is not None and current["status"] == "succeeded" and current["attempt"] == attempt.number:
                return
            try:
                await asyncio.to_thread(job_store.finish, attempt, result)
            except JobSuperseded:
                await asyncio.to_thread(job_store.fail, attempt, None)
            if cancelled_wait:
                raise asyncio.CancelledError
