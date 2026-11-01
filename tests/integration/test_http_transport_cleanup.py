from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import httpx
import pytest

from app.core.cancellation import OperationCancelledError, cancellation_scope, shutdown_event
from app.core.http_transport import CancellableHTTPTransport, HTTPRequestDeadline, bounded_http_request


@pytest.mark.parametrize("interruption", ["cancel", "shutdown", "deadline"])
def test_cancellation_waits_for_response_cleanup_already_in_progress(
    monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    closing, completed, interrupted, peer_closed = Event(), Event(), Event(), Event()
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            if self.path == "/warmup":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                self.wfile.flush()
                return
            try:
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b"body exceeds the configured response bound")
                self.wfile.flush()
                self.connection.settimeout(5)
                if self.connection.recv(1) == b"":
                    peer_closed.set()
            except (BrokenPipeError, ConnectionResetError):
                peer_closed.set()
            finally:
                self.close_connection = True

        def log_message(self, format: str, *args: object) -> None:
            pass

    original_send = httpx.AsyncHTTPTransport.handle_async_request

    class DelayedClose(httpx.AsyncByteStream):
        def __init__(self, stream: httpx.AsyncByteStream) -> None:
            self.stream = stream

        async def __aiter__(self) -> AsyncIterator[bytes]:
            async for chunk in self.stream:
                yield chunk

        async def aclose(self) -> None:
            # 只延迟外部 HTTP 清理；保留真实 socket 和原始关闭实现。
            closing.set()
            if interruption == "shutdown":
                shutdown_event.set()
            try:
                await asyncio.sleep(0.6 if interruption == "deadline" else 0.2)
                await self.stream.aclose()
            except asyncio.CancelledError:
                interrupted.set()
                raise
            completed.set()

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        response = await original_send(transport, request)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        if request.url.path != "/warmup":
            response.stream = DelayedClose(response.stream)
        return response

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever)
    worker.start()
    try:
        with httpx.Client(transport=CancellableHTTPTransport(limits=httpx.Limits())) as client:
            assert client.post(f"http://127.0.0.1:{server.server_port}/warmup", content=b"warmup").text == "ok"
            with cancellation_scope(lambda: interruption == "cancel" and closing.is_set()):
                started = time.monotonic()
                with bounded_http_request(
                    timeout=0.4 if interruption == "deadline" else 10, max_response_bytes=2, check=lambda: None
                ):
                    with pytest.raises(HTTPRequestDeadline if interruption == "deadline" else OperationCancelledError):
                        client.post(f"http://127.0.0.1:{server.server_port}/v1/chat/completions", content=b"request")
                assert time.monotonic() - started < 2
            assert closing.is_set() and completed.is_set() and not interrupted.is_set()
            assert peer_closed.wait(2)
            assert requests == ["/warmup", "/v1/chat/completions"]
    finally:
        shutdown_event.clear()
        server.shutdown()
        server.server_close()
        worker.join(3)
