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

from app.core.execution import run_sync, start_thread, wait_for_completion
from app.core.process_control import ProcessCancelledError
from app.core.runtime_settings import configured_entry
from app.core.sanitize import safe_exception_summary
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
        self._handlers: dict[str, JobHandler] = {}
        self._started = False
        self._stopping = False
        self._start_lock = asyncio.Lock()
        self._release_ownership: Callable[[], None] | None = None

    def register(self, job_type: str, handler: JobHandler) -> None:
        """在组合入口注册业务处理器。"""
        self._handlers[job_type] = handler

    async def start(self) -> None:
        """读取持久任务，清算中断状态并调度可恢复操作。"""
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
                recovered.append(await run_sync(partial(job_store.recover, JobAttempt(job["id"], job["attempt"]))))
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

    async def stop(self, *, timeout_s: float = 15.0) -> None:
        """禁止新调度，确认已开始的处理器退出后清理登记。"""
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
            _, pending = await asyncio.wait(tasks, timeout=timeout_s)
            if pending:
                raise RuntimeError(f"{len(pending)} 个 Web 作业未确认退出，关闭未完成")
        self._tasks.clear()
        self._cancel_events.clear()
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
            context = JobContext(attempt.job_id, event, attempt.number)
            execution = start_thread(partial(configured_entry(handler), context, job["payload"]))
            # 等待结束后读取异常，领域异常由任务边界记录，状态写入异常继续向外传播。
            cancelled_wait = await wait_for_completion(
                execution, event.set if job["cancellable_while_running"] else None
            )
            error = execution.exception() if not execution.cancelled() else JobCancelled("处理器已取消")
            if error is not None:
                cancelled = isinstance(error, (JobCancelled, ProcessCancelledError))
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
