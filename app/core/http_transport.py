# ruff: noqa: D100
from __future__ import annotations

import asyncio
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from threading import Event, Lock

import httpx
from anyio import TASK_STATUS_IGNORED, CancelScope
from anyio.abc import TaskStatus
from anyio.from_thread import BlockingPortal, start_blocking_portal

from app.core.cancellation import OperationShutdownError, check_cancellation, shutdown_event


class HTTPResponseInvalid(RuntimeError):
    """模型响应无法在当前网络协议和容量约束内完整读取。"""


class HTTPResponseTooLarge(HTTPResponseInvalid):
    """实际网络或解码正文超过当前请求的容量上限。"""


class HTTPRequestDeadline(RuntimeError):
    """当前请求的绝对期限已到，连接关闭后才能返回。"""


@dataclass(frozen=True, slots=True)
class _RequestScope:
    deadline: float
    max_response_bytes: int
    check: Callable[[], None]


_request_scope: ContextVar[_RequestScope | None] = ContextVar("model_http_request_scope", default=None)


@contextmanager
def bounded_http_request(*, timeout: float, max_response_bytes: int, check: Callable[[], None]) -> Iterator[None]:
    """将绝对期限与取消核查限定到单次请求，不改变共享连接池。"""
    token = _request_scope.set(_RequestScope(time.monotonic() + timeout, max_response_bytes, check))
    try:
        check()
        yield
    finally:
        _request_scope.reset(token)


async def _read_response(response: httpx.Response, scope: _RequestScope | None) -> bytes:
    if scope is None:
        return await response.aread()
    if response.is_stream_consumed:
        if len(response.content) > scope.max_response_bytes:
            raise HTTPResponseTooLarge("模型响应超过大小上限")
        return response.content
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip"}:
        raise HTTPResponseInvalid("模型响应使用未协商的压缩格式")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    body = bytearray()
    received = 0
    async for chunk in response.aiter_raw():
        if time.monotonic() >= scope.deadline:
            raise HTTPRequestDeadline("模型请求超过绝对时间预算")
        received += len(chunk)
        if received > scope.max_response_bytes:
            raise HTTPResponseTooLarge("模型网络响应超过大小上限")
        try:
            # zlib 的 max_length 在解压分配前生效，不先展开完整压缩块。
            body.extend(
                decoder.decompress(chunk, scope.max_response_bytes - len(body) + 1) if decoder is not None else chunk
            )
        except zlib.error as exc:
            raise HTTPResponseInvalid("模型响应压缩格式无效") from exc
        if len(body) > scope.max_response_bytes:
            raise HTTPResponseTooLarge("模型解码正文超过大小上限")
    if decoder is not None and (not decoder.eof or decoder.unused_data):
        raise HTTPResponseInvalid("模型压缩响应不完整或包含额外数据")
    return bytes(body)


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
        scope = _request_scope.get()
        portal, client = self._connection()
        incoming = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.read(),
            extensions=request.extensions,
        )
        if scope is not None:
            incoming.headers["accept-encoding"] = "gzip, identity"
        finished = Event()

        async def receive() -> httpx.Response:
            if shutdown_event.is_set():
                raise OperationShutdownError("服务关闭，取消尚未发送的模型请求")
            response = await client.send(incoming, stream=True, follow_redirects=False)
            try:
                body = await _read_response(response, scope)
            finally:
                with CancelScope(shield=True):
                    await response.aclose()
            # 已完成合法压缩解码；重建同步响应时不再次解码，仍复用底层连接。
            headers = response.headers.copy()
            headers.pop("content-encoding", None)
            headers.pop("transfer-encoding", None)
            headers["content-length"] = str(len(body))
            return httpx.Response(
                response.status_code, headers=headers, content=body, request=request, extensions=response.extensions
            )

        async def send(*, task_status: TaskStatus[None] = TASK_STATUS_IGNORED) -> httpx.Response:
            task_status.started()
            network_scope = CancelScope()

            async def scoped_receive() -> httpx.Response:
                with network_scope:
                    return await receive()
                raise asyncio.CancelledError

            network = asyncio.create_task(scoped_receive())
            try:
                while not network.done():
                    # 租约读取可能在调用线程等待数据库；全局停止仍能独立关闭网络。
                    if shutdown_event.is_set():
                        raise OperationShutdownError("服务关闭，中断在途模型请求")
                    if scope is not None and time.monotonic() >= scope.deadline:
                        raise HTTPRequestDeadline("模型请求超过绝对时间预算")
                    await asyncio.wait({network}, timeout=0.05)
                return network.result()
            except (OperationShutdownError, HTTPRequestDeadline, asyncio.CancelledError) as interruption:
                # 通过作用域取消，避免原生 Task.cancel 打断已受保护的连接关闭。
                network_scope.cancel()
                with CancelScope(shield=True):
                    result = (await asyncio.gather(network, return_exceptions=True))[0]
                if isinstance(result, Exception) and result is not interruption:
                    raise interruption from result
                raise
            finally:
                finished.set()

        future, _ = portal.start_task(send)
        try:
            while not finished.wait(0.05):
                check_cancellation(force=False)
                if scope is not None:
                    scope.check()
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
