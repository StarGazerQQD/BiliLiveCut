"""Worker 生命周期管理 — 全局状态、子进程跟踪、资源跟踪。"""

from __future__ import annotations

import random
import threading
import time as _time
import uuid
from datetime import UTC, datetime

_WORKER_ID: str = f"worker-{uuid.uuid4().hex[:8]}"

# 关闭标记
_shutting_down: bool = False

# 子进程跟踪
_subprocesses: list = []
_subprocesses_lock: threading.Lock | None = None

# V0.1.13: Task-level resource tracking (ResourceBudget integration)
_task_resources: dict[int, dict[str, int | float]] = {}
_task_resources_lock: threading.Lock | None = None

_RETRY_JITTER_S: float = 5.0


def now_utc() -> datetime:
    """返回 UTC 时间。"""
    return datetime.now(UTC)


def jitter(base: float, jitter_s: float = _RETRY_JITTER_S) -> float:
    """添加随机抖动量。"""
    return base + random.uniform(0, jitter_s)


def track_subprocess(proc) -> None:
    """注册子进程句柄, 供关闭时统一 terminate/kill。"""
    global _subprocesses_lock
    if _subprocesses_lock is None:
        _subprocesses_lock = threading.Lock()
    with _subprocesses_lock:
        _subprocesses.append(proc)


def untrack_subprocess(proc) -> None:
    """从跟踪集中移除已正常结束的子进程。"""
    global _subprocesses_lock
    if _subprocesses_lock is None:
        return
    with _subprocesses_lock:
        try:
            _subprocesses.remove(proc)
        except ValueError:
            pass


def cleanup_subprocesses() -> None:
    """关闭所有被跟踪的子进程: SIGTERM → 等待 → SIGKILL。"""
    import logging

    _logger = logging.getLogger(__name__)
    global _subprocesses_lock
    if _subprocesses_lock is None:
        return
    with _subprocesses_lock:
        procs = list(_subprocesses)
        _subprocesses.clear()
    if not procs:
        return
    _logger.warning("清理 {} 个子进程 (SIGTERM)", len(procs))
    for p in procs:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    _time.sleep(5)
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                _logger.warning("子进程 {} 已被 SIGKILL", p.pid)
        except Exception:
            pass
