# ruff: noqa: D100
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from urllib.parse import unquote
from weakref import WeakKeyDictionary

from filelock import FileLock, Timeout
from pydantic import JsonValue
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.sanitize import sanitize_diagnostic
from app.db.entities import AppSetting
from app.db.session import get_session
from app.jobs.contracts import ACTIVE, TERMINAL, JobAttempt, JobData, JobSuperseded, decode_job, now_iso, validate_job

_PREFIX = "web_job:"


_memory_owners: WeakKeyDictionary[Engine, Lock] = WeakKeyDictionary()
_memory_owners_lock = Lock()


def acquire_execution() -> Callable[[], None]:
    """非阻塞取得数据库级执行所有权；持有到处理器确认退出，不删除锁文件。"""
    from app.db.session import engine

    if engine.url.get_backend_name() != "sqlite":
        raise RuntimeError("Web 作业执行锁当前仅支持 SQLite")
    database = engine.url.database
    if str(engine.url.query.get("uri", "")).lower() in {"1", "true"}:
        if engine.url.query.get("mode") == "memory" or database == "file::memory:":
            raise RuntimeError("Web 作业执行锁不支持共享内存 URI，请使用普通 SQLite 文件数据库")
        if database and database.startswith("file:"):
            database = unquote(database[5:])
    if not database or database == ":memory:":
        with _memory_owners_lock:
            owner = _memory_owners.setdefault(engine, Lock())
        if not owner.acquire(blocking=False):
            raise RuntimeError("当前数据库已有 Web 作业执行实例，不能重复启动或恢复作业")
        return owner.release
    # resolve 使相对路径和符号链接指向同一所有权，操作系统在进程退出时释放锁。
    path = Path(database).resolve()
    owner = FileLock(str(path) + ".web-jobs.lock", thread_local=False)
    try:
        owner.acquire(timeout=0)
    except Timeout:
        raise RuntimeError("当前数据库已有 Web 作业执行实例，不能重复启动或恢复作业") from None
    return owner.release


def _lock(db: Session) -> None:
    # 在首次读取前取得 SQLite 写锁，跨线程和连接共同遵守取消与提交顺序。
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _read(db: Session, job_id: str) -> JobData:
    row = db.get(AppSetting, f"{_PREFIX}{job_id}")
    if row is None:
        raise ValueError("作业不存在")
    return decode_job(row.value)


def _write(db: Session, job: JobData) -> None:
    job["updated_at"] = now_iso()
    validate_job(job)
    key = f"{_PREFIX}{job['id']}"
    row = db.get(AppSetting, key)
    encoded = json.dumps(job, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if row is None:
        row = AppSetting(key=key, value=encoded)
    else:
        row.value = encoded
        row.updated_at = datetime.now(UTC)
    db.add(row)
    db.flush()


def _require(db: Session, attempt: JobAttempt, states: frozenset[str]) -> JobData:
    job = _read(db, attempt.job_id)
    if job["attempt"] != attempt.number or job["status"] not in states:
        raise JobSuperseded(f"作业状态或执行代次已改变: job={attempt.job_id} attempt={attempt.number}")
    return job


def _authorize(job: JobData, actor: str, is_admin: bool) -> None:
    if not is_admin and job["owner"] != actor:
        raise PermissionError("无权操作其他用户的作业")


def get_job(job_id: str) -> JobData | None:
    """读取一个已经提交的作业快照。"""
    with get_session() as db:
        row = db.get(AppSetting, f"{_PREFIX}{job_id}")
        return decode_job(row.value) if row else None


def iter_active_jobs() -> Iterator[JobData]:
    """分页扫描活动记录，不受界面显示上限影响。"""
    after = ""
    while True:
        with get_session() as db:
            rows = db.exec(
                select(AppSetting)
                .where(AppSetting.key.startswith(_PREFIX), AppSetting.key > after)
                .order_by(AppSetting.key)
                .limit(128)
            ).all()
        if not rows:
            return
        after = rows[-1].key
        for row in rows:
            job = decode_job(row.value)
            if job["status"] in ACTIVE:
                yield job


def list_jobs(limit: int = 100, *, owner: str | None = None) -> list[JobData]:
    """按当前权限范围返回最新持久状态。"""
    with get_session() as db:
        rows = db.exec(select(AppSetting).where(AppSetting.key.startswith(_PREFIX))).all()
        jobs = [decode_job(row.value) for row in rows]
    if owner is not None:
        jobs = [job for job in jobs if job["owner"] == owner]
    jobs.sort(key=lambda job: job["updated_at"], reverse=True)
    return jobs[: max(1, min(limit, 500))]


def register(job: JobData) -> JobData:
    """在同一事务内登记并去重，用户和操作类型隔离幂等键。"""
    validate_job(job)
    if job["status"] != "queued" or job["attempt"] != 1 or job["result"] is not None:
        raise ValueError("新作业必须从首个排队代次登记")
    with get_session() as db:
        _lock(db)
        if job["dedup_key"]:
            rows = db.exec(select(AppSetting).where(AppSetting.key.startswith(_PREFIX))).all()
            for row in rows:
                existing = decode_job(row.value)
                if (
                    existing["status"] in ACTIVE
                    and existing["owner"] == job["owner"]
                    and existing["type"] == job["type"]
                    and existing["dedup_key"] == job["dedup_key"]
                ):
                    if existing["payload"] != job["payload"]:
                        raise ValueError("相同作业去重键对应不同请求，请等待原操作结束")
                    return existing
        if db.get(AppSetting, f"{_PREFIX}{job['id']}") is not None:
            raise ValueError("作业标识已经登记")
        _write(db, job)
    return job


def claim(attempt: JobAttempt) -> JobData:
    """只有当前排队代次可以领取一次执行权。"""
    with get_session() as db:
        _lock(db)
        job = _require(db, attempt, frozenset({"queued"}))
        job.update(status="running", progress=1, message="开始执行", started_at=now_iso())
        _write(db, job)
    return job


def report(attempt: JobAttempt, progress: int, message: str) -> None:
    """拒绝旧代次或终态之后的进度写入。"""
    with get_session() as db:
        _lock(db)
        job = _require(db, attempt, frozenset({"running"}))
        job.update(progress=max(job["progress"], min(99, max(0, progress))), message=sanitize_diagnostic(message))
        _write(db, job)


@contextmanager
def result_transaction(attempt: JobAttempt) -> Iterator[Session]:
    """领域写入业务结果的事务；必须在退出前共同写入成功终态。"""
    with get_session() as db:
        _lock(db)
        _require(db, attempt, frozenset({"running"}))
        yield db
        _require(db, attempt, frozenset({"succeeded"}))


def complete(db: Session, attempt: JobAttempt, result: dict[str, JsonValue]) -> None:
    """在领域当前事务内验证提交条件并记录成功，不自行提交。"""
    job = _require(db, attempt, frozenset({"running"}))
    job.update(status="succeeded", progress=100, message="执行完成", result=result, finished_at=now_iso())
    _write(db, job)


def finish(attempt: JobAttempt, result: dict[str, JsonValue]) -> None:
    """完成没有本地业务写入的计算或外部结果核对。"""
    with result_transaction(attempt) as db:
        complete(db, attempt, result)


def fail(attempt: JobAttempt, error: str | None) -> None:
    """记录执行结束；取消意图优先，已提交终态和新代次不可覆盖。"""
    with get_session() as db:
        _lock(db)
        job = _read(db, attempt.job_id)
        if job["attempt"] != attempt.number or job["status"] in TERMINAL:
            return
        cancelled = job["status"] == "cancelling" or error is None
        job.update(
            status="cancelled" if cancelled else "failed",
            message="作业已取消" if cancelled else "执行失败，请查看错误详情后处理",
            error=None if cancelled else sanitize_diagnostic(error),
            finished_at=now_iso(),
        )
        _write(db, job)


def cancel(job_id: str, actor: str, *, is_admin: bool = False) -> JobData:
    """在结果提交共用的写事务内封锁后续提交。"""
    with get_session() as db:
        _lock(db)
        job = _read(db, job_id)
        _authorize(job, actor, is_admin)
        if job["status"] in TERMINAL:
            raise ValueError("作业已经结束")
        if job["status"] != "queued" and not job["cancellable_while_running"]:
            raise ValueError("该操作开始后不能安全取消，请等待结果")
        queued = job["status"] == "queued"
        job.update(
            status="cancelled" if queued else "cancelling",
            message="已取消等待中的作业" if queued else "正在停止当前操作",
            finished_at=now_iso() if queued else None,
        )
        _write(db, job)
    return job


def retry(job_id: str, actor: str, *, is_admin: bool = False) -> JobData:
    """开启新代次，保留作业标识和原权限主体。"""
    with get_session() as db:
        _lock(db)
        job = _read(db, job_id)
        _authorize(job, actor, is_admin)
        if job["status"] not in {"failed", "cancelled"}:
            raise ValueError("只有失败或已取消的作业可以重试")
        job.update(
            status="queued",
            progress=0,
            message="等待重试",
            result=None,
            error=None,
            attempt=job["attempt"] + 1,
            started_at=None,
            finished_at=None,
        )
        _write(db, job)
    return job


def recover(attempt: JobAttempt) -> JobData:
    """启动时清算旧执行；安全操作以新代次恢复，外部效果保持待核对。"""
    with get_session() as db:
        _lock(db)
        job = _require(db, attempt, ACTIVE)
        if job["status"] == "cancelling":
            job.update(status="cancelled", message="服务重启后确认取消", finished_at=now_iso(), recovered=True)
        elif job["status"] == "running" and not job["cancellable_while_running"]:
            job.update(
                status="failed",
                message="服务中断，外部结果需要人工核对",
                error="为防止重复上传，未自动重试；请先核对平台结果",
                finished_at=now_iso(),
            )
        elif job["status"] == "running":
            job.update(
                status="queued",
                progress=0,
                message="服务重启后重新排队",
                recovered=True,
                attempt=job["attempt"] + 1,
                started_at=None,
                finished_at=None,
            )
        _write(db, job)
    return job
