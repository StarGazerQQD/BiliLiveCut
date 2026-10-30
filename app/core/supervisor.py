# ruff: noqa: D100
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event, Lock

from loguru import logger

_deadline: ContextVar[float | None] = ContextVar("service_shutdown_deadline", default=None)
_process_control: ContextVar[ShutdownControl | None] = ContextVar("owned_process_shutdown", default=None)


class ShutdownControl:
    """独立程序入口共享的关闭期限，启动时创建的异步任务也能观察更新。"""

    def __init__(self) -> None:
        self.expires_at: float | None = None
        self.started = Event()
        self.finished = Event()
        self._lock = Lock()

    def begin(self, timeout_s: float) -> None:
        """仅首次停止请求冻结预算，重复信号不能延长关闭期限。"""
        with self._lock:
            if self.expires_at is None:
                self.expires_at = time.monotonic() + max(0.0, timeout_s)
                self.started.set()


def process_shutdown_control() -> ShutdownControl | None:
    """返回显式独立程序入口的控制对象；嵌入式 ASGI 宿主没有该权限。"""
    return _process_control.get()


@contextmanager
def standalone_process() -> Iterator[ShutdownControl]:
    """仅 CLI 主入口声明进程所有权，不在通用 lifespan 猜测宿主身份。"""
    control = ShutdownControl()
    token = _process_control.set(control)
    try:
        yield control
    finally:
        _process_control.reset(token)


def shutdown_deadline(timeout_s: float) -> float:
    """嵌套关闭沿用更早的绝对期限，不累加每个服务的超时。"""
    deadline = time.monotonic() + max(0.0, timeout_s)
    inherited = _deadline.get()
    if inherited is not None:
        deadline = min(deadline, inherited)
    control = _process_control.get()
    if control is not None and control.expires_at is not None:
        deadline = min(deadline, control.expires_at)
    return deadline


def remaining_shutdown(maximum_s: float) -> float:
    """返回当前清理步骤在共同期限内还能等待的时间。"""
    return max(0.0, shutdown_deadline(maximum_s) - time.monotonic())


@contextmanager
def shutdown_scope(timeout_s: float) -> Iterator[float]:
    """为异步子任务和继承上下文的同步执行共享关闭总预算。"""
    deadline = shutdown_deadline(timeout_s)
    token = _deadline.set(deadline)
    try:
        yield deadline
    finally:
        _deadline.reset(token)


@dataclass(frozen=True)
class Service:
    """组合入口提供的生命周期接口；监管器不读取业务表或调用领域逻辑。"""

    name: str
    start: Callable[[], Awaitable[object]] | None = None
    stop: Callable[[], Awaitable[object]] | None = None
    request_stop: Callable[[], None] | None = None


class Supervisor:
    """顺序启动服务，先通知停止接单，再逆序清理所有已登记服务。"""

    def __init__(
        self,
        services: Sequence[Service],
        request_stop: Callable[[], None],
        *,
        shutdown_timeout_s: float | Callable[[], float] = 30.0,
    ) -> None:
        if len({service.name for service in services}) != len(services):
            raise ValueError("受监管服务名称必须唯一")
        self._services = tuple(services)
        self._request_stop = request_stop
        self._active = False
        self._failed = False
        self._shutdown_timeout_s = shutdown_timeout_s
        self._closing_timeout_s = 0.0
        self._stopping: dict[str, asyncio.Task[object]] = {}

    @property
    def shutdown_confirmed(self) -> bool:
        """全部登记的服务已成功收尾，允许应用释放实例所有权。"""
        return not self._active and not self._failed and not self._stopping

    async def _stop(self, service: Service) -> None:
        assert service.stop is not None
        try:
            task = asyncio.create_task(service.stop(), name=f"stop:{service.name}")
            self._stopping[service.name] = task

            def completed(finished: asyncio.Task[object]) -> None:
                self._stopping.pop(service.name, None)
                if not finished.cancelled() and (error := finished.exception()) is not None:
                    logger.opt(exception=error).error("服务清理执行失败 service={}", service.name)

            task.add_done_callback(completed)
            # 即使前一服务耗尽预算，仍让其余服务收到各自的关闭调用。
            await asyncio.sleep(0)
            _, pending = await asyncio.wait({task}, timeout=remaining_shutdown(self._closing_timeout_s))
            if pending:
                raise RuntimeError(f"服务 {service.name} 尚未物理退出，关闭总期限已到，保留清理登记")
            task.result()
        except BaseException:
            # 关闭边界必须记录失败，同时由退出栈继续清理其余服务。
            self._failed = True
            logger.exception("应用服务关闭失败 service={}", service.name)
            raise

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """登记清理先于启动，覆盖部分启动失败及应用作用域取消。"""
        if self._active or self._failed:
            raise RuntimeError("应用服务仍在运行或此前关闭未完成，不能重复启动")
        self._active = True
        deadline_token = None
        registered: list[Service] = []
        try:
            async with AsyncExitStack() as cleanup:
                try:
                    for service in self._services:
                        registered.append(service)
                        if service.stop is not None:
                            cleanup.push_async_callback(self._stop, service)
                        if service.start is not None:
                            await service.start()
                    yield
                finally:
                    self._closing_timeout_s = (
                        self._shutdown_timeout_s() if callable(self._shutdown_timeout_s) else self._shutdown_timeout_s
                    )
                    deadline_token = _deadline.set(shutdown_deadline(self._closing_timeout_s))
                    errors: list[Exception] = []
                    callbacks = [self._request_stop, *(service.request_stop for service in registered)]
                    for callback in callbacks:
                        if callback is not None:
                            try:
                                callback()
                            except Exception as exc:
                                # 广播边界必须通知其余服务，失败随后由退出栈完整清理并报告。
                                errors.append(exc)
                    if errors:
                        self._failed = True
                        raise ExceptionGroup("停止广播失败", errors)
        finally:
            if deadline_token is not None:
                _deadline.reset(deadline_token)
            self._active = False
