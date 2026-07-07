"""Worker 生命周期管理 — 全局状态、子进程跟踪、资源跟踪。"""

from __future__ import annotations

import random
import sys as _sys
import threading
import time as _time
import uuid
from datetime import UTC, datetime

_WORKER_ID: str = f"worker-{uuid.uuid4().hex[:8]}"

# 关闭标记 — 使用 threading.Event, 确保所有模块读取同一共享状态
shutdown_event: threading.Event = threading.Event()

# 子进程跟踪 — 锁在模块初始化时立即创建, 避免延迟初始化竞态
_subprocesses: list = []
_subprocesses_lock: threading.Lock = threading.Lock()

# V0.1.13: Task-level resource tracking (ResourceBudget integration)
_task_resources: dict[int, dict[str, int | float]] = {}
_task_resources_lock: threading.Lock = threading.Lock()

_RETRY_JITTER_S: float = 5.0


class _ShutdownProxy:
    """后向兼容: 代理 _shutting_down bool 到 shutdown_event.is_set()。"""

    def __bool__(self) -> bool:
        return shutdown_event.is_set()

    def __eq__(self, other: object) -> bool:
        return bool(self) == bool(other)

    def __ne__(self, other: object) -> bool:
        return bool(self) != bool(other)

    def __repr__(self) -> str:
        return str(bool(self))


# 后向兼容: 模块级 _shutting_down (只读, 通过代理同步)
_module = _sys.modules[__name__]
_shutting_down = _ShutdownProxy()
_module._shutting_down = _shutting_down


def now_utc() -> datetime:
    """返回 UTC 时间。"""
    return datetime.now(UTC)


def jitter(base: float, jitter_s: float = _RETRY_JITTER_S) -> float:
    """添加随机抖动量。"""
    return base + random.uniform(0, jitter_s)


def track_subprocess(proc) -> None:
    """注册子进程句柄, 供关闭时统一 terminate/kill。"""
    with _subprocesses_lock:
        _subprocesses.append(proc)


def untrack_subprocess(proc) -> None:
    """从跟踪集中移除已正常结束的子进程。"""
    with _subprocesses_lock:
        try:
            _subprocesses.remove(proc)
        except ValueError:
            pass


def cleanup_subprocesses() -> None:
    """关闭所有被跟踪的子进程: SIGTERM → 等待 → SIGKILL。"""
    import logging

    _logger = logging.getLogger(__name__)
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
