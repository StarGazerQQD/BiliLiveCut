"""可持久恢复的 Web 长耗时作业管理器。"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlmodel import select

from app.core.process_control import ProcessCancelledError
from app.db.models import AppSetting
from app.db.session import get_session

JobHandler = Callable[["JobContext", dict[str, Any]], dict[str, Any] | None]
_JOB_PREFIX = "web_job:"
_TERMINAL = {"succeeded", "failed", "cancelled"}
_ACTIVE = {"queued", "running", "cancelling"}


class JobCancelled(RuntimeError):
    """作业收到取消请求。"""


@dataclass(frozen=True, slots=True)
class JobContext:
    """传给同步作业处理器的进度与取消接口。"""

    job_id: str
    _cancel_event: threading.Event

    def report(self, progress: int, message: str) -> None:
        """持久化当前进度和用户可读状态。"""
        _update_job(self.job_id, progress=max(0, min(progress, 100)), message=message)

    def cancelled(self) -> bool:
        """返回是否已经收到取消请求。"""
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        """收到取消请求时中断处理器。"""
        if self.cancelled():
            raise JobCancelled("用户取消了作业")


class WebJobManager:
    """管理后台线程作业，并把状态持久化到 ``app_settings``。"""

    def __init__(self, max_concurrency: int = 2) -> None:
        self._max_concurrency = max_concurrency
        self._semaphore: asyncio.Semaphore | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._handlers: dict[str, JobHandler] = {}
        self._started = False

    def register(self, job_type: str, handler: JobHandler) -> None:
        """注册一种作业处理器。"""
        self._handlers[job_type] = handler

    async def start(self) -> None:
        """启动管理器并恢复上次未完成的作业。"""
        if self._started:
            return
        self._started = True
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        from app.web.services.job_handlers import register_job_handlers

        register_job_handlers(self)
        for job in list_jobs(limit=500):
            if job["status"] == "running" and not job.get("cancellable_while_running", True):
                _update_job(
                    job["id"],
                    status="failed",
                    message="服务中断，外部结果需要人工核对",
                    error="为防止重复上传，未自动重试；请先核对平台结果",
                    finished_at=_now_iso(),
                )
                continue
            if job["status"] in {"running", "cancelling"}:
                _update_job(
                    job["id"],
                    status="queued",
                    message="服务重启后重新排队",
                    recovered=True,
                )
            if job["status"] in _ACTIVE:
                self._schedule(job["id"])

    async def stop(self) -> None:
        """请求活动作业停止，并等待可取消处理器释放资源。"""
        if not self._started:
            return
        for job_id, event in tuple(self._cancel_events.items()):
            job = get_job(job_id)
            if job is not None and not job.get("cancellable_while_running", True):
                continue
            event.set()
            _update_job(job_id, status="cancelling", message="服务关闭，正在停止")
        tasks = tuple(self._tasks.values())
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
            except TimeoutError:
                logger.warning("Web 作业关闭等待超时，{} 个线程仍在收尾", len(self._tasks))
        self._tasks.clear()
        self._cancel_events.clear()
        self._started = False

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        label: str,
        owner: str,
        dedup_key: str | None = None,
        cancellable_while_running: bool = True,
    ) -> dict[str, Any]:
        """创建作业；相同去重键已有活动作业时直接返回原作业。"""
        if job_type not in self._handlers:
            if not self._started:
                await self.start()
            if job_type not in self._handlers:
                raise ValueError(f"未知 Web 作业类型: {job_type}")
        if dedup_key:
            for existing in list_jobs(limit=500):
                if existing.get("dedup_key") == dedup_key and existing["status"] in _ACTIVE:
                    return existing
        now = _now_iso()
        job = {
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
        }
        _save_job(job)
        self._schedule(job["id"])
        return job

    def cancel(self, job_id: str, actor: str, *, is_admin: bool = False) -> dict[str, Any]:
        """取消排队或运行中的作业。"""
        job = get_job(job_id)
        if job is None:
            raise ValueError("作业不存在")
        _require_owner(job, actor, is_admin)
        if job["status"] in _TERMINAL:
            raise ValueError("作业已经结束")
        if job["status"] in {"running", "cancelling"} and not job.get("cancellable_while_running", True):
            raise ValueError("该操作开始后不能安全取消，请等待结果")
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
            return _update_job(job_id, status="cancelling", message="正在停止当前操作")
        return _update_job(
            job_id,
            status="cancelled",
            progress=job.get("progress", 0),
            message="已取消等待中的作业",
            finished_at=_now_iso(),
        )

    def retry(self, job_id: str, actor: str, *, is_admin: bool = False) -> dict[str, Any]:
        """原地重试失败或取消的作业。"""
        job = get_job(job_id)
        if job is None:
            raise ValueError("作业不存在")
        _require_owner(job, actor, is_admin)
        if job["status"] not in {"failed", "cancelled"}:
            raise ValueError("只有失败或已取消的作业可以重试")
        job.update(
            {
                "status": "queued",
                "progress": 0,
                "message": "等待重试",
                "result": None,
                "error": None,
                "attempt": int(job.get("attempt", 1)) + 1,
                "updated_at": _now_iso(),
                "started_at": None,
                "finished_at": None,
            }
        )
        _save_job(job)
        self._schedule(job_id)
        return job

    def _schedule(self, job_id: str) -> None:
        if job_id in self._tasks and not self._tasks[job_id].done():
            return
        task = asyncio.create_task(self._execute(job_id), name=f"web-job-{job_id[:8]}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _task, key=job_id: self._tasks.pop(key, None))

    async def _execute(self, job_id: str) -> None:
        assert self._semaphore is not None
        async with self._semaphore:
            job = get_job(job_id)
            if job is None or job["status"] != "queued":
                return
            handler = self._handlers.get(job["type"])
            if handler is None:
                _update_job(job_id, status="failed", error="作业处理器不可用", finished_at=_now_iso())
                return
            cancel_event = threading.Event()
            self._cancel_events[job_id] = cancel_event
            _update_job(
                job_id,
                status="running",
                progress=1,
                message="开始执行",
                started_at=_now_iso(),
            )
            context = JobContext(job_id, cancel_event)
            try:
                result = await asyncio.to_thread(handler, context, job["payload"])
                context.check_cancelled()
            except (JobCancelled, ProcessCancelledError) as exc:
                _update_job(
                    job_id,
                    status="cancelled",
                    message=str(exc),
                    finished_at=_now_iso(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Web 作业失败: id={} type={}", job_id, job["type"])
                _update_job(
                    job_id,
                    status="failed",
                    message="执行失败，可重试",
                    error=str(exc)[:2000],
                    finished_at=_now_iso(),
                )
            else:
                _update_job(
                    job_id,
                    status="succeeded",
                    progress=100,
                    message="执行完成",
                    result=result or {},
                    finished_at=_now_iso(),
                )
            finally:
                self._cancel_events.pop(job_id, None)


def get_job(job_id: str) -> dict[str, Any] | None:
    """读取一个持久化 Web 作业。"""
    with get_session() as db:
        row = db.get(AppSetting, f"{_JOB_PREFIX}{job_id}")
    return _decode_job(row.value) if row else None


def list_jobs(limit: int = 100, *, owner: str | None = None) -> list[dict[str, Any]]:
    """按更新时间倒序列出 Web 作业。"""
    with get_session() as db:
        rows = db.exec(select(AppSetting).where(AppSetting.key.startswith(_JOB_PREFIX))).all()
    jobs = [_decode_job(row.value) for row in rows]
    if owner is not None:
        jobs = [job for job in jobs if job.get("owner") == owner]
    jobs.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    return jobs[: max(1, min(limit, 500))]


def _save_job(job: dict[str, Any]) -> None:
    job["updated_at"] = _now_iso()
    value = json.dumps(job, ensure_ascii=False, separators=(",", ":"), default=str)
    with get_session() as db:
        key = f"{_JOB_PREFIX}{job['id']}"
        row = db.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=value)
        else:
            row.value = value
            row.updated_at = datetime.now(UTC)
        db.add(row)


def _update_job(job_id: str, **changes: Any) -> dict[str, Any]:
    job = get_job(job_id)
    if job is None:
        raise ValueError("作业不存在")
    job.update(changes)
    _save_job(job)
    return job


def _decode_job(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Web 作业记录损坏")
    return value


def _require_owner(job: dict[str, Any], actor: str, is_admin: bool) -> None:
    if not is_admin and job.get("owner") != actor:
        raise PermissionError("无权操作其他用户的作业")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


web_job_manager = WebJobManager()
