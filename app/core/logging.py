"""日志系统。

基于 loguru 提供结构化、带轮转的日志:

* 控制台彩色输出(便于开发);
* 各进程独占 ``storage/logs/blc-<pid>-<run_id>.log``,自动轮转与压缩;
* 通过 :func:`setup_logging` 在程序入口初始化一次。

业务模块直接 ``from loguru import logger`` 使用即可。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING
from uuid import uuid4

import psutil
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.paths import logs_dir
from app.core.sanitize import safe_exception_summary, sanitize_diagnostic

if TYPE_CHECKING:
    from loguru import Message, Record

_CONFIGURED = False
_DB_CAPACITY = 512
_DB_BATCH_SIZE = 100
_db_lock = Lock()
_db_pending = 0
_db_scheduled = False
_db_dropped = 0
_db_reported_drops = 0
_db_retention_failures = 0
_db_reported_retention_failures = 0
_db_last_drop_report: float | None = None
_db_failure_sample: str | None = None


@dataclass(frozen=True, slots=True)
class _DatabaseLog:
    generation: str
    level: str
    module: str | None
    event: str | None
    message: str
    created_at: datetime
    context_json: str | None


_db_records: deque[_DatabaseLog] = deque()
_FILE_LOG_NAME = re.compile(
    r"blc-(?P<pid>[1-9][0-9]*)-[0-9a-f]{32}"
    r"(?:\.[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}_[0-9]{6}(?:\.[0-9]+)?)?"
    r"\.log(?:\.zip)?"
)


def _retain_file_logs(directory: Path, _rotated_files: list[str]) -> None:
    """清理各进程的过期日志，保留活跃文件、旧版日志和非项目文件。"""
    cutoff = time.time() - 14 * 24 * 60 * 60
    for path in directory.glob("blc-*.log*"):
        match = _FILE_LOG_NAME.fullmatch(path.name)
        if match is None:
            continue
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            if path.suffix != ".zip" and psutil.pid_exists(int(match["pid"])):
                continue
            path.unlink()
        except FileNotFoundError:
            # 独立进程可能同时清理同一归档，已删除即达到清理目标。
            continue
        except PermissionError as exc:
            # 外部查看器或权限可阻止清理；保留文件并直接报告，避免递归写入当前 sink。
            print(f"过期日志清理失败，已保留文件：{safe_exception_summary(exc)}", file=sys.stderr)


class _ApplicationLogHandler(logging.Handler):
    """将流水线使用的标准 logging 接入同一文件和数据库诊断通道。"""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "uvicorn.access" and os.environ.get("BLC_FRONTEND_PORT"):
            # 双端启动由入口代理记录真实客户地址，避免内部转发重复记录。
            return
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, path, protocol, status = record.args
            record.args = (client, method, str(path).split("?", 1)[0], protocol, status)
        message = sanitize_diagnostic(record.getMessage(), limit=32768)
        logger.bind(_stdlib_origin=(record.name, record.funcName, record.lineno)).opt(exception=record.exc_info).log(
            record.levelname, "{}", message
        )
        if record.exc_info and record.exc_info[1]:
            message += "\n" + safe_exception_summary(record.exc_info[1])
        # 后续已有标准处理器（包括控制台）也不能重新输出原始异常/参数。
        record.msg, record.args, record.exc_info, record.exc_text = message, (), None, None


def _safe_record(record: Record) -> None:
    """在所有 sink 之前保留安全上下文和异常链；不渲染局部变量或命令参数。"""
    context = record["extra"]
    if context.get("_diagnostic_formatted"):
        return
    context["_diagnostic_formatted"] = True
    origin = context.pop("_stdlib_origin", None)
    if origin is not None:
        record["name"], record["function"], record["line"] = origin
    message = record["message"]
    if context:
        # 只输出定位任务所需的标识，避免意外序列化整个配置或请求对象。
        fields = ("task_id", "job_id", "session_id", "room_id", "segment_id", "platform", "stage", "operation")
        values = [f"{key}={context[key]}" for key in fields if key in context]
        if values:
            message += "\ncontext: " + " ".join(values)
    exception = record["exception"]
    if exception is not None:
        error = exception.value
        if error is not None:
            message += "\n" + safe_exception_summary(error, limit=8192)
            seen: set[int] = set()
            while error is not None and id(error) not in seen:
                seen.add(id(error))
                for frame in traceback.extract_tb(error.__traceback__):
                    message += f"\n  {frame.filename}:{frame.lineno} in {frame.name}"
                error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        # Loguru 原始异常渲染会绕过正文脱敏（并可能输出 locals）。
        record["exception"] = None
    record["message"] = sanitize_diagnostic(message, limit=32768)


# 初始化文件/数据库 sink 前的启动诊断也必须脱敏。
logger.configure(patcher=_safe_record)


def setup_logging() -> None:
    """初始化全局日志处理器(幂等)。

    多次调用只会生效一次,避免重复添加 handler 导致日志重复输出。
    """
    global _CONFIGURED
    # Uvicorn 在 CLI 初始化之后可能重新配置日志，lifespan 再次调用时必须重接安全通道。
    for name in ("app", "uvicorn", "uvicorn.error", "uvicorn.access"):
        standard_logger = logging.getLogger(name)
        standard_logger.handlers = [_ApplicationLogHandler()]
        standard_logger.disabled = False
        standard_logger.setLevel(logging.DEBUG if name == "app" else logging.INFO)
        standard_logger.propagate = False
    # dictConfig 还会逐个禁用已存在的业务子 logger，仅恢复父 logger 不够。
    for name, existing_logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith("app.") and isinstance(existing_logger, logging.Logger):
            existing_logger.disabled = False
    if _CONFIGURED:
        return

    # 移除 loguru 默认 handler,改用自定义配置。
    logger.remove()
    logger.configure(patcher=_safe_record)

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    # 控制台:彩色,级别取自配置。
    logger.add(sys.stderr, level=settings.log_level, format=fmt, enqueue=True, diagnose=False, backtrace=False)

    # 独立解释器不共享 Loguru 队列；每次启动独占文件，避免 Windows 轮转时互相占用。
    directory = logs_dir()
    _retain_file_logs(directory, [])
    logger.add(
        directory / f"blc-{os.getpid()}-{uuid4().hex}.log",
        level="DEBUG",
        format=fmt,
        rotation="10 MB",
        retention=partial(_retain_file_logs, directory),
        compression="zip",
        encoding="utf-8",
        enqueue=True,  # 本进程线程通过队列交给同一个文件写入者。
        backtrace=False,
        diagnose=False,  # 开发环境也不记录局部变量中的凭据。
    )

    # filter 限制等待及在途条目，只向现有 Loguru worker 投递排空触发。
    logger.add(_DatabaseLogSink(), level="WARNING", enqueue=True, filter=_filter_db_sink)

    _CONFIGURED = True
    logger.debug("日志系统已初始化(env={}, level={})", settings.app_env, settings.log_level)


def _filter_db_sink(record: Record) -> bool:
    """有界接收安全诊断，仅为空闲队列安排一次后台批写。"""
    global _db_pending, _db_scheduled, _db_dropped
    if record["extra"].get("_skip_db"):
        return False
    from app.db.session import database_log_generation

    generation = database_log_generation()
    record["extra"]["_database_generation"] = generation
    if generation is None:
        return False
    from app.db.runtime_logs import RUNTIME_LOG_CONTEXT

    item = _DatabaseLog(
        generation,
        record["level"].name,
        record["name"],
        record["function"],
        record["message"],
        record["time"].astimezone(UTC).replace(tzinfo=None),
        RUNTIME_LOG_CONTEXT if record["level"].name == "WARNING" else None,
    )
    with _db_lock:
        if _db_pending >= _DB_CAPACITY:
            _db_dropped += 1
            return False
        _db_records.append(item)
        _db_pending += 1
        if _db_scheduled:
            return False
        _db_scheduled = True
        return True


def _db_sink(message: Message) -> None:
    """在原有 Loguru worker 排空有限快照，保留 logger.complete 的屏障。"""
    global _db_pending, _db_scheduled
    from app.db.entities import SystemLog
    from app.db.runtime_logs import prune_runtime_logs
    from app.db.session import get_database_log_session

    with _db_lock:
        snapshot = list(_db_records)
        _db_records.clear()
        _db_scheduled = False
    try:
        for offset in range(0, len(snapshot), _DB_BATCH_SIZE):
            batch = snapshot[offset : offset + _DB_BATCH_SIZE]
            for generation in dict.fromkeys(item.generation for item in batch):
                records = [item for item in batch if item.generation == generation]
                try:
                    with get_database_log_session(generation) as db:
                        if db is None:
                            continue
                        db.add_all(
                            [
                                SystemLog(
                                    level=item.level,
                                    module=item.module,
                                    event=item.event,
                                    message=item.message,
                                    created_at=item.created_at,
                                    context_json=item.context_json,
                                )
                                for item in records
                            ]
                        )
                except (SQLAlchemyError, OSError, RuntimeError, ValueError) as exc:
                    _record_database_log_failure(exc, dropped=len(records))
                    continue
                if any(item.context_json is not None for item in records):
                    try:
                        prune_runtime_logs(limit=len(records), generation=generation)
                    except (SQLAlchemyError, OSError, RuntimeError, ValueError) as exc:
                        _record_database_log_failure(exc, retention=True)
    finally:
        with _db_lock:
            _db_pending -= len(snapshot)
        _report_database_log_drops()


def _record_database_log_failure(error: BaseException, *, dropped: int = 0, retention: bool = False) -> None:
    global _db_dropped, _db_retention_failures, _db_failure_sample
    detail = safe_exception_summary(error)
    with _db_lock:
        _db_dropped += dropped
        _db_retention_failures += int(retention)
        _db_failure_sample = detail


class _DatabaseLogSink:
    def write(self, message: Message) -> None:
        _db_sink(message)

    def stop(self) -> None:
        _report_database_log_drops(force=True)


def _report_database_log_drops(*, force: bool = False) -> None:
    global _db_last_drop_report, _db_reported_drops, _db_reported_retention_failures, _db_failure_sample
    with _db_lock:
        now = time.monotonic()
        if (_db_dropped == _db_reported_drops and _db_retention_failures == _db_reported_retention_failures) or (
            not force and _db_last_drop_report is not None and now - _db_last_drop_report < 60
        ):
            return
        count = _db_dropped - _db_reported_drops
        retention_failures = _db_retention_failures - _db_reported_retention_failures
        detail = _db_failure_sample
        _db_reported_drops = _db_dropped
        _db_reported_retention_failures = _db_retention_failures
        _db_failure_sample = None
        _db_last_drop_report = now
    print(
        f"数据库诊断本轮未保留 {count} 条副本、保留清理失败 {retention_failures} 次，"
        f"完整文件日志仍保留。{detail or ''}",
        file=sys.stderr,
    )


def database_log_queue_snapshot() -> dict[str, int]:
    """读取诊断副本容量和丢弃计数，不向日志系统递归投递。"""
    with _db_lock:
        return {
            "capacity": _DB_CAPACITY,
            "pending": _db_pending,
            "queued": len(_db_records),
            "in_flight": _db_pending - len(_db_records),
            "dropped": _db_dropped,
            "retention_failures": _db_retention_failures,
        }
