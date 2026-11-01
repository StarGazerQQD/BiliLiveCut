# ruff: noqa: D100
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Lock
from time import monotonic

shutdown_event = Event()
_checks: ContextVar[tuple[CancellationProbe, ...]] = ContextVar("execution_cancel_checks", default=())


class CancellationProbe:
    """限制外部身份读取频率；开始请求与采纳结果仍可立即复核。"""

    def __init__(self, check: Callable[[], bool], *, interval_s: float = 0.5) -> None:
        self._check = check
        self._interval_s = interval_s
        self._next_check = 0.0
        self._cancelled = False
        self._lock = Lock()

    def poll(self, *, force: bool = False) -> bool:
        """取消一经观察便保持有效，避免旧执行恢复身份。"""
        with self._lock:
            if not self._cancelled and (force or monotonic() >= self._next_check):
                self._cancelled = self._check()
                self._next_check = monotonic() + self._interval_s
            return self._cancelled


class OperationCancelledError(RuntimeError):
    """执行收到取消意图，物理资源收尾后由业务边界结算状态。"""


class OperationShutdownError(OperationCancelledError):
    """应用关闭中断执行，不计为业务失败或授予新的请求额度。"""


def cancellation_requested(*, force: bool = False) -> bool:
    """优先检查不访问外部资源的关闭信号，再核对当前执行身份。"""
    return shutdown_event.is_set() or any(check.poll(force=force) for check in _checks.get())


def check_cancellation(*, force: bool = True) -> None:
    """在开始外部操作和采纳结果前检查共享取消意图。"""
    check_shutdown()
    if any(check.poll(force=force) for check in _checks.get()):
        raise OperationCancelledError("当前执行已取消或失去提交身份")


def check_shutdown() -> None:
    """在已核验身份的提交事务内复核关闭，不额外获取数据库会话。"""
    if shutdown_event.is_set():
        raise OperationShutdownError("服务正在关闭，停止当前执行")


@contextmanager
def cancellation_scope(check: Callable[[], bool], *, poll_interval_s: float = 0.0) -> Iterator[None]:
    """为当前调用链叠加取消条件，嵌套与线程上下文保留原有条件。"""
    token = _checks.set((*_checks.get(), CancellationProbe(check, interval_s=poll_interval_s)))
    try:
        yield
    finally:
        _checks.reset(token)
