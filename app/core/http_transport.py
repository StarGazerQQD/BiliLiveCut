# ruff: noqa: D100
from __future__ import annotations

import asyncio
from contextlib import ExitStack, suppress
from functools import partial
from threading import Event, Lock

import httpx
from anyio import TASK_STATUS_IGNORED, CancelScope
from anyio.abc import TaskStatus
from anyio.from_thread import BlockingPortal, start_blocking_portal

from app.core.cancellation import OperationShutdownError, check_cancellation, shutdown_event


class CancellableHTTPTransport(httpx.BaseTransport):
    """同步 SDK 的异步网络边界；复用连接池并等待被取消连接实际关闭。"""

    def __init__(self, *, limits: httpx.Limits) -> None:
        self._limits = limits
        self._lock = Lock()
        self._stack = ExitStack()
        self._portal: BlockingPortal | None = None
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    def _connection(self) -> tuple[BlockingPortal, httpx.AsyncClient]:
        with self._lock:
            if self._closed:
                raise RuntimeError("模型连接已关闭")
            if self._portal is None:
                stack = ExitStack()
                try:
                    portal = stack.enter_context(start_blocking_portal(name="blc-model-http"))
                    client = portal.call(partial(httpx.AsyncClient, limits=self._limits))
                    stack.callback(portal.call, client.aclose)
                except BaseException:
                    stack.close()
                    raise
                self._stack = stack
                self._portal, self._client = portal, client
            assert self._client is not None
            return self._portal, self._client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """保留 SDK 请求与重定向规则，停止信号只取消当前请求。"""
        check_cancellation()
        portal, client = self._connection()
        incoming = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.read(),
            extensions=request.extensions,
        )
        finished = Event()

        async def receive() -> httpx.Response:
            if shutdown_event.is_set():
                raise OperationShutdownError("服务关闭，取消尚未发送的模型请求")
            response = await client.send(incoming, stream=True, follow_redirects=False)
            try:
                await response.aread()
            finally:
                with CancelScope(shield=True):
                    await response.aclose()
            # 同一响应已完成解码并关闭；同步 SDK 读取缓存正文，不重新解码 gzip。
            response.stream = httpx.ByteStream(response.content)
            return response

        async def send(*, task_status: TaskStatus[None] = TASK_STATUS_IGNORED) -> httpx.Response:
            task_status.started()
            network = asyncio.create_task(receive())
            try:
                while not network.done():
                    # 租约读取可能在调用线程等待数据库；全局停止仍能独立关闭网络。
                    if shutdown_event.is_set():
                        raise OperationShutdownError("服务关闭，中断在途模型请求")
                    await asyncio.wait({network}, timeout=0.05)
                return network.result()
            finally:
                try:
                    if not network.done():
                        network.cancel()
                    with CancelScope(shield=True), suppress(asyncio.CancelledError):
                        await network
                finally:
                    finished.set()

        future, _ = portal.start_task(send)
        try:
            while not finished.wait(0.05):
                check_cancellation(force=False)
            check_cancellation()
            return future.result()
        finally:
            if not finished.is_set():
                future.cancel()
                # Future.cancel 只表达意图；网络协程 finally 才能证明连接已关闭。
                finished.wait()

    def close(self) -> None:
        """由客户端租用计数归零后的关闭入口回收连接池及专属事件循环。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stack.close()
