"""Worker 生命周期管理 — 全局状态、子进程跟踪、资源跟踪。"""

from __future__ import annotations

import random
import subprocess
import threading
import uuid
from datetime import UTC, datetime

from app.analysis.transcription.process_tree import ProcessTree
from app.core.cancellation import shutdown_event as shutdown_event
from app.core.sanitize import safe_exception_summary

_WORKER_ID: str = f"worker-{uuid.uuid4().hex[:8]}"

# 子进程跟踪 — 锁在模块初始化时立即创建, 避免延迟初始化竞态
_subprocesses: dict[subprocess.Popen[str] | subprocess.Popen[bytes], ProcessTree] = {}
_subprocesses_lock: threading.Lock = threading.Lock()

# V0.1.13: Task-level resource tracking (ResourceBudget integration)
_task_resources: dict[tuple[int, str | None], dict[str, int | float]] = {}
_task_resources_lock: threading.Lock = threading.Lock()
_executions: set[tuple[int, str]] = set()
_executions_lock = threading.Lock()

_RETRY_JITTER_S: float = 5.0


def register_execution(task_id: int, lease_token: str) -> None:
    """登记本实例仍在执行的租约，取消不能在同步调用收尾前结算。"""
    with _executions_lock:
        _executions.add((task_id, lease_token))


def finish_execution(task_id: int, lease_token: str) -> None:
    """执行及资源清理结束后撤销本地登记，不影响同任务的新租约。"""
    with _executions_lock:
        _executions.discard((task_id, lease_token))


def execution_active(task_id: int, lease_token: str) -> bool:
    """确认原执行是否仍在本实例中，包括尚未返回的同步 LLM/媒体调用。"""
    with _executions_lock:
        return (task_id, lease_token) in _executions


def now_utc() -> datetime:
    """返回 UTC 时间。"""
    return datetime.now(UTC)


def jitter(base: float, jitter_s: float = _RETRY_JITTER_S) -> float:
    """添加随机抖动量。"""
    return base + random.uniform(0, jitter_s)


def track_subprocess(proc: subprocess.Popen[str] | subprocess.Popen[bytes], tree: ProcessTree) -> None:
    """登记命令及其独占进程树，关闭时同时确认后代与管道收尾。"""
    with _subprocesses_lock:
        _subprocesses[proc] = tree


def untrack_subprocess(proc: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
    """从跟踪集中移除已正常结束的子进程。"""
    with _subprocesses_lock:
        _subprocesses.pop(proc, None)


def cleanup_subprocesses() -> None:
    """收尾本实例登记的命令树；失败保留登记并上报，不能声称已释放。"""
    import logging

    from app.core.process_control import terminate_owned_process

    _logger = logging.getLogger(__name__)
    with _subprocesses_lock:
        procs = list(_subprocesses.items())
    if not procs:
        return
    _logger.warning("清理 %s 个独占命令进程树", len(procs))
    errors: list[Exception] = []
    for proc, tree in procs:
        try:
            # 输出仅由命令调用者读取，关闭线程不能同时消费同一 PIPE。
            terminate_owned_process(proc, tree, drain_output=False)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            errors.append(exc)
            _logger.error("子进程树清理失败 pid=%s: %s", proc.pid, safe_exception_summary(exc))
        else:
            untrack_subprocess(proc)
    if errors:
        raise ExceptionGroup("受监管命令进程树清理失败", errors)
