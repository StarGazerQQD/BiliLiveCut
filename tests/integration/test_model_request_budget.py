from __future__ import annotations

import gzip
import json
import os
import selectors
import socket
import time
from collections.abc import AsyncIterator
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread, Timer
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest

from app.analysis import llm
from app.analysis.model_execution import ModelCallBudget
from app.core.cancellation import OperationCancelledError, OperationShutdownError, shutdown_event
from app.core.config import settings
from app.models.configuration import save_model_configuration
from tests.integration.test_structured_llm import call, catalog, response

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_structured_transient_retry_retains_identity_and_unknown_cost(
    temp_db: None, monkeypatch: MonkeyPatch, status: int
) -> None:
    save_model_configuration(catalog())
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                status, json={"error": {"code": "temporary"}}, headers={"Retry-After": "0", "x-request-id": "failed-1"}
            )
        return httpx.Response(200, json=response(), headers={"x-request-id": "success-2"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = ModelCallBudget(max_attempts=2, max_elapsed_s=10)
    result = call(budget)
    assert result.value is not None and result.value.answer == "有效正文"
    assert len(requests) == 2 and requests[0].content == requests[1].content
    assert [item.request_id for item in budget.attempts] == ["failed-1", "success-2"]
    assert budget.attempts[0].usage.estimated_cost_usd is None
    assert budget.attempts[1].usage.estimated_cost_usd == pytest.approx(0.00005)
    assert llm._today_spend() == pytest.approx(0.00005)
    assert call(budget).error == "attempt_budget"
    assert len(requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_permanent_http_failure_is_not_retried(temp_db: None, monkeypatch: MonkeyPatch, status: int) -> None:
    save_model_configuration(catalog())
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "invalid request"}}, headers={"Retry-After": "0"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=10)
    assert call(budget).value is None
    assert len(requests) == 1 and len(budget.attempts) == 1


def test_retry_after_cannot_extend_deadline_or_repeat_early(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    save_model_configuration(catalog())
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, json={"error": {}}, headers={"Retry-After": "120"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=3)
    started = time.monotonic()
    assert call(budget).error == "rate_limited"
    assert time.monotonic() - started < 2
    assert len(requests) == 1


def test_text_and_structured_calls_share_every_actual_attempt(temp_db: None, monkeypatch: MonkeyPatch) -> None:
    save_model_configuration(catalog())
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        text = "" if len(requests) == 1 else "完整正文"
        return httpx.Response(200, json=response(text), headers={"x-request-id": f"request-{len(requests)}"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = ModelCallBudget(max_attempts=2, max_elapsed_s=10)
    try:
        assert llm.call_text("正文", budget=budget) == "完整正文"
        assert len(budget.attempts) == 2
        assert [item.request_id for item in budget.attempts] == ["request-1", "request-2"]
        assert call(budget).error == "attempt_budget"
        assert llm.call_text("不能重新授予额度", budget=budget) is None
        assert len(requests) == 2
        assert all(json.loads(item.content)["max_tokens"] == 512 for item in requests)
    finally:
        llm.close_clients()


@pytest.mark.parametrize("structured", [True, False])
def test_cancellation_while_waiting_for_retry_is_not_original_network_failure(
    temp_db: None, monkeypatch: MonkeyPatch, structured: bool
) -> None:
    save_model_configuration(catalog())
    stopped = Event()
    timer = Timer(0.15, stopped.set)
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        timer.start()
        return httpx.Response(503, json={"error": {}}, headers={"Retry-After": "1"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=5, cancelled=stopped.is_set)
    try:
        if structured:
            assert call(budget).error == "cancelled"
        else:
            with pytest.raises(OperationCancelledError):
                llm.call_text("取消等待", budget=budget)
        assert len(requests) == len(budget.attempts) == 1
    finally:
        timer.cancel()
        if timer.is_alive():
            timer.join(2)
        llm.close_clients()


@pytest.mark.parametrize("status", [200, 500])
@pytest.mark.parametrize("compressed", [True, False])
def test_text_success_and_error_bodies_are_bounded_and_closed(
    temp_db: None, monkeypatch: MonkeyPatch, status: int, compressed: bool
) -> None:
    save_model_configuration(catalog())
    closed = Event()
    requests: list[httpx.Request] = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            payload = b" " * 16_384
            yield gzip.compress(payload) if compressed else payload

        async def aclose(self) -> None:
            closed.set()

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Content-Encoding": "gzip"} if compressed else {}, stream=Stream())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(llm, "_HTTP_RESPONSE_MAX_BYTES", 128)
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=5)
    try:
        assert llm.call_text("有界响应", budget=budget) is None
        assert closed.is_set() and len(requests) == 1
        assert budget.attempts[0].error == "invalid_output"
    finally:
        llm.close_clients()


@pytest.mark.parametrize("minimum_descriptor", [0] if os.name == "nt" else [0, 1100])
def test_text_trickled_network_response_cannot_extend_deadline(
    temp_db: None, monkeypatch: MonkeyPatch, minimum_descriptor: int
) -> None:
    closed = Event()
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            if len(requests) == 1:
                body = json.dumps(response("连接就绪")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                return
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                with ExitStack() as stack:
                    connection = self.connection
                    if minimum_descriptor:
                        import fcntl
                        import resource

                        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                        if soft < minimum_descriptor + 128:
                            resource.setrlimit(resource.RLIMIT_NOFILE, (minimum_descriptor + 128, hard))
                            stack.callback(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
                        descriptor = fcntl.fcntl(connection.fileno(), fcntl.F_DUPFD_CLOEXEC, minimum_descriptor)
                        connection = stack.enter_context(socket.socket(fileno=descriptor))
                        assert connection.fileno() >= minimum_descriptor
                    readable = stack.enter_context(selectors.DefaultSelector())
                    readable.register(connection, selectors.EVENT_READ)
                    for _ in range(100):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        if readable.select(0.03) and connection.recv(1) == b"":
                            closed.set()
                            break
            except OSError:
                closed.set()
            finally:
                self.close_connection = True

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever)
    worker.start()
    save_model_configuration(catalog(base_url=f"http://127.0.0.1:{server.server_port}/v1"))
    monkeypatch.setattr(settings, "llm_request_timeout_s", 60)
    try:
        # 用真实请求完成客户端冷初始化，原有短预算只测已进入网络的滴流响应。
        assert llm.call_text("预热连接", budget=ModelCallBudget(max_attempts=1, max_elapsed_s=10)) == "连接就绪"
        assert len(requests) == 1
        budget = ModelCallBudget(max_attempts=8, max_elapsed_s=0.4)
        started = time.monotonic()
        assert llm.call_text("绝对截止", budget=budget) is None
        assert time.monotonic() - started < 2
        assert len(requests) == 2, requests
        assert closed.wait(2)
        assert len(budget.attempts) == 1
        assert budget.attempts[0].error == "deadline"
    finally:
        llm.close_clients()
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_received_text_usage_survives_shutdown_before_business_acceptance(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    save_model_configuration(catalog())

    def create(**_kwargs: object) -> dict[str, object]:
        shutdown_event.set()
        return {**response("不能采纳"), "_request_id": "received-before-stop"}

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_get_client", lambda _provider: client)
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=5)
    with pytest.raises(OperationShutdownError):
        llm.call_text("取消前已收到回执", budget=budget)
    assert len(budget.attempts) == 1 and budget.attempts[0].error == "cancelled"
    assert budget.attempts[0].request_id == "received-before-stop"
    assert budget.attempts[0].usage.input_tokens == 10
    assert budget.attempts[0].usage.estimated_cost_usd == pytest.approx(0.00005)
    assert llm._today_spend() == pytest.approx(0.00005)


@pytest.mark.parametrize("structured", [True, False])
def test_accounting_write_failure_preserves_received_usage_and_stops_calls(
    temp_db: None, monkeypatch: MonkeyPatch, structured: bool
) -> None:
    save_model_configuration(catalog(routes=2))
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response(), headers={"x-request-id": "known-usage"})

    def denied(_source: Path, _destination: Path) -> None:
        raise PermissionError("受控账本写失败")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(os, "replace", denied)
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=5)
    try:
        with pytest.raises(llm.BudgetAccountingError):
            call(budget) if structured else llm.call_text("账本不能写", budget=budget)
        assert len(requests) == len(budget.attempts) == 1
        assert budget.attempts[0].usage.input_tokens == 10
        assert budget.attempts[0].usage.estimated_cost_usd == pytest.approx(0.00005)
        assert budget.attempts[0].request_id == "known-usage"
    finally:
        llm.close_clients()
