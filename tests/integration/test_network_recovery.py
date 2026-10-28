from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import pytest

from app.plugins.live_source import (
    DanmakuStatus,
    SourceAuthenticationError,
    SourceRateLimited,
    SourceRoom,
    SourceTemporaryError,
    SourceUnavailable,
)
from app.sources.bilibili import danmaku, source
from app.sources.bilibili.client import BilibiliAuthenticationError, BilibiliLiveClient, BilibiliTransportError
from app.sources.registry import SourceRegistry
from tests.integration.test_bilibili_client import _WBI_RESPONSE


def http_boundary(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> None:
    class Client(BilibiliLiveClient):
        def __init__(self, cookie: str = "", timeout: float = 10.0) -> None:
            self._client = httpx.AsyncClient(
                headers={"Cookie": cookie}, transport=httpx.MockTransport(handler), timeout=timeout
            )

    monkeypatch.setattr(source, "BilibiliLiveClient", Client)
    monkeypatch.setattr(danmaku, "BilibiliLiveClient", Client)


def anonymous_socket_boundary(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    ready, closed = asyncio.Event(), asyncio.Event()

    class Socket:
        received = 0

        async def send(self, message: bytes) -> None:
            for _, operation, body in danmaku._iter_raw(message):
                if operation == danmaku.OP_AUTH:
                    assert json.loads(body)["uid"] == 0

        async def recv(self) -> bytes:
            self.received += 1
            if self.received == 1:
                return danmaku.encode_packet(danmaku.OP_AUTH_REPLY, b'{"code":0}')
            if self.received == 2:
                return danmaku.encode_packet(danmaku.OP_HEARTBEAT_REPLY, (91).to_bytes(4, "big"))
            ready.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    @asynccontextmanager
    async def connect(*args: object, **kwargs: object) -> AsyncIterator[Socket]:
        try:
            yield Socket()
        finally:
            closed.set()

    monkeypatch.setattr("websockets.connect", connect)
    return ready, closed


@pytest.fixture
def connection_alerts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    alerts: list[tuple[int, str]] = []

    def publish(room_id: int, reason: str) -> None:
        alerts.append((room_id, reason))

    monkeypatch.setattr("app.notify.delivery.publish_connection_alert", publish)
    return alerts


@pytest.mark.parametrize(
    ("status", "code", "expected", "attempts"),
    [
        (503, 0, SourceTemporaryError, 2),
        (408, 0, SourceTemporaryError, 2),
        (404, 0, SourceUnavailable, 1),
        (401, 0, SourceAuthenticationError, 1),
        (403, 0, SourceRateLimited, 1),
        (429, 0, SourceRateLimited, 1),
        (412, 0, SourceRateLimited, 1),
        (200, -352, SourceRateLimited, 1),
        (200, -101, SourceAuthenticationError, 1),
        (200, -412, SourceAuthenticationError, 1),
    ],
)
async def test_source_errors_preserve_authentication_and_bounded_retry(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, status: int, code: int, expected: type[Exception], attempts: int
) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"code": code, "message": "failure"})

    http_boundary(monkeypatch, respond)
    registry = SourceRegistry(retry_delay_s=0)
    room = SourceRoom(platform="bilibili", source_id="123", canonical_url="https://live.bilibili.com/123")
    with pytest.raises(expected) as caught:
        await registry.get_room_info(room)
    assert calls == attempts
    if status == 401:
        cause = caught.value.__cause__
        assert isinstance(cause, BilibiliAuthenticationError)
        assert not cause.cookie_login_request
        assert isinstance(cause.__cause__, httpx.HTTPStatusError)
        assert "过期" not in str(caught.value)


@pytest.mark.parametrize("response_delay", [0.0, 1.1], ids=["immediate-response", "slow-response"])
async def test_transient_http_retry_after_does_not_extend_call_budget(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, response_delay: float
) -> None:
    calls = 0
    response_ready, release_response = asyncio.Event(), asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(response_delay)
        response = httpx.Response(503, headers={"Retry-After": "120"})
        response_ready.set()
        await release_response.wait()
        return response

    http_boundary(monkeypatch, respond)
    registry = SourceRegistry()
    room = SourceRoom(platform="bilibili", source_id="123", canonical_url="https://live.bilibili.com/123")
    task = asyncio.create_task(registry.get_room_info(room))
    try:
        await asyncio.wait_for(response_ready.wait(), registry.timeout_s)
        # 从 503 响应就绪后计时，保留原 1 秒重试处理限制，排除响应前耗时。
        release_response.set()
        with pytest.raises(SourceTemporaryError) as caught:
            await asyncio.wait_for(task, 1)
        assert caught.value.retry_after == 120 and calls == 1
        with pytest.raises(SourceRateLimited) as cooldown:
            await asyncio.wait_for(registry.get_room_info(room), 1)
        assert cooldown.value.retry_after is not None and 0 < cooldown.value.retry_after <= 120
        assert calls == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_network_recovery_retries_login_while_anonymous_socket_remains_active(
    monkeypatch: pytest.MonkeyPatch, failure: type[httpx.RequestError]
) -> None:
    logged_calls = 0
    connected = asyncio.Event()
    closed = asyncio.Event()

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal logged_calls
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json=_WBI_RESPONSE)
        logged = bool(request.headers.get("Cookie"))
        if logged:
            logged_calls += 1
            if logged_calls == 1:
                raise failure("temporary network failure", request=request)
            assert connected.is_set() and not closed.is_set()
        return httpx.Response(200, json={"code": 0, "data": {"token": "logged" if logged else "anon", "host_list": []}})

    class Socket:
        authenticated = False

        async def send(self, message: bytes) -> None:
            pass

        async def recv(self) -> bytes:
            if not self.authenticated:
                self.authenticated = True
                return danmaku.encode_packet(danmaku.OP_AUTH_REPLY, b'{"code":0}')
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    @asynccontextmanager
    async def connect(*args: object, **kwargs: object) -> AsyncIterator[Socket]:
        connected.set()
        try:
            yield Socket()
        finally:
            closed.set()

    http_boundary(monkeypatch, respond)
    monkeypatch.setattr("websockets.connect", connect)
    client = danmaku.DanmakuClient(123, 4, cookie="DedeUserID=456; SESSDATA=secret", login_retry_interval_s=0.01)
    access = await client._select_access()
    assert not access.uses_cookie and client._login_failures == 0
    recovered = await asyncio.wait_for(client._consume_access(access), 1)
    assert recovered is not None and recovered.uses_cookie and recovered.uid == 456
    assert logged_calls == 2 and closed.is_set() and client._login_failures == 0
    assert not client.login_disabled


async def test_auth_reply_timeout_is_transport_failure() -> None:
    class Socket:
        async def send(self, message: bytes) -> None:
            pass

        async def recv(self) -> bytes:
            raise TimeoutError("socket read timeout")

    client = danmaku.DanmakuClient(123, 4, cookie="DedeUserID=456")
    with pytest.raises(BilibiliTransportError) as caught:
        await client._authenticate(Socket(), "token", uid=456)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert client._login_failures == 0


@pytest.mark.parametrize("failure", ["timeout", "invalid_json", "missing_code", "missing_reply"])
async def test_auth_transport_and_invalid_frames_keep_cookie_budget(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    modes: list[bool] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json=_WBI_RESPONSE)
        return httpx.Response(200, json={"code": 0, "data": {"token": "token", "host_list": []}})

    class Socket:
        logged = False
        authenticated = False

        async def send(self, message: bytes) -> None:
            for _, operation, body in danmaku._iter_raw(message):
                if operation == danmaku.OP_AUTH:
                    payload = json.loads(body)
                    self.logged = bool(payload["uid"])
                    modes.append(self.logged)

        async def recv(self) -> bytes:
            if not self.authenticated:
                self.authenticated = True
                if self.logged:
                    if failure == "timeout":
                        raise TimeoutError("authentication transport timeout")
                    body = {"invalid_json": b"[]", "missing_code": b"{}", "missing_reply": b"{}"}[failure]
                    operation = danmaku.OP_MESSAGE if failure == "missing_reply" else danmaku.OP_AUTH_REPLY
                    return danmaku.encode_packet(operation, body)
                return danmaku.encode_packet(danmaku.OP_AUTH_REPLY, b'{"code":0}')
            client.stop()
            return danmaku.encode_packet(danmaku.OP_HEARTBEAT_REPLY, b"\x00\x00\x00\x00")

    @asynccontextmanager
    async def connect(*args: object, **kwargs: object) -> AsyncIterator[Socket]:
        yield Socket()

    http_boundary(monkeypatch, respond)
    monkeypatch.setattr("websockets.connect", connect)
    client = danmaku.DanmakuClient(123, 4, cookie="DedeUserID=456", login_retry_interval_s=30)
    await asyncio.wait_for(client.run(), 3)
    assert modes == [True, False]
    assert client._login_failures == 0 and not client.login_disabled


@pytest.mark.parametrize(("endpoint", "status"), [("danmu", 401), ("danmu", 404), ("nav", 401), ("nav", 404)])
async def test_permanent_login_failure_keeps_anonymous_consuming_and_stops_probing(
    monkeypatch: pytest.MonkeyPatch, connection_alerts: list[tuple[int, str]], endpoint: str, status: int
) -> None:
    rejected = 0
    prior_tasks = asyncio.all_tasks()

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal rejected
        is_nav = request.url.path.endswith("/nav")
        if request.headers.get("Cookie") and is_nav == (endpoint == "nav"):
            rejected += 1
            return httpx.Response(status)
        if is_nav:
            return httpx.Response(200, json=_WBI_RESPONSE)
        assert not request.headers.get("Cookie")
        return httpx.Response(200, json={"code": 0, "data": {"token": "anon", "host_list": []}})

    http_boundary(monkeypatch, respond)
    ready, closed = anonymous_socket_boundary(monkeypatch)
    client = danmaku.DanmakuClient(
        123, 4, cookie="DedeUserID=456; SESSDATA=secret", login_retry_max_attempts=3, login_retry_interval_s=0.01
    )
    task = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(3):
            await ready.wait()
            while not client.login_disabled:
                await asyncio.sleep(0.001)
        counted = endpoint == "danmu" and status == 401
        assert rejected == (3 if counted else 1)
        assert client._login_failures == (3 if counted else 0)
        assert connection_alerts == ([(123, "login_rejected")] if counted else [])
        if not counted:
            assert client._login_blocked_reason is not None
            assert f"HTTP {status}" in client._login_blocked_reason
            assert "secret" not in client._login_blocked_reason
        await asyncio.sleep(0.05)
        assert rejected == (3 if counted else 1)
        assert client.popularity == 91 and not closed.is_set() and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and closed.is_set()
    assert not asyncio.all_tasks() - prior_tasks


@pytest.mark.parametrize(("endpoint", "status"), [("danmu", 401), ("danmu", 404), ("nav", 401), ("nav", 404)])
async def test_permanent_anonymous_http_failure_stops_collection_without_cookie_rejection(
    monkeypatch: pytest.MonkeyPatch, connection_alerts: list[tuple[int, str]], endpoint: str, status: int
) -> None:
    failed_calls = 0
    states: list[DanmakuStatus] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal failed_calls
        assert not request.headers.get("Cookie")
        if request.url.path.endswith("/nav") and endpoint != "nav":
            return httpx.Response(200, json=_WBI_RESPONSE)
        failed_calls += 1
        return httpx.Response(status)

    async def on_state(state: DanmakuStatus) -> None:
        states.append(state)

    http_boundary(monkeypatch, respond)
    ready, closed = anonymous_socket_boundary(monkeypatch)
    client = danmaku.DanmakuClient(123, 4, on_state=on_state, login_retry_interval_s=0.01)
    await asyncio.wait_for(client.run(), 1)
    assert states == [DanmakuStatus.CONNECTING, DanmakuStatus.FAILED]
    assert failed_calls == 1 and client._login_failures == 0 and connection_alerts == []
    assert not ready.is_set() and not closed.is_set()


async def test_cancelling_http_login_probe_closes_anonymous_socket_and_pending_request(
    monkeypatch: pytest.MonkeyPatch, connection_alerts: list[tuple[int, str]]
) -> None:
    prior_tasks = asyncio.all_tasks()
    probing, request_cancelled = asyncio.Event(), asyncio.Event()
    logged_calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal logged_calls
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json=_WBI_RESPONSE)
        if request.headers.get("Cookie"):
            logged_calls += 1
            if logged_calls == 1:
                raise httpx.ReadTimeout("temporary HTTP timeout", request=request)
            probing.set()
            try:
                await asyncio.Event().wait()
            finally:
                request_cancelled.set()
            raise AssertionError("unreachable")
        return httpx.Response(200, json={"code": 0, "data": {"token": "anon", "host_list": []}})

    http_boundary(monkeypatch, respond)
    ready, closed = anonymous_socket_boundary(monkeypatch)
    client = danmaku.DanmakuClient(123, 4, cookie="DedeUserID=456", login_retry_interval_s=0.01)
    task = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(3):
            await ready.wait()
            await probing.wait()
        assert client.popularity == 91 and not closed.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and closed.is_set() and request_cancelled.is_set()
    assert logged_calls == 2 and client._login_failures == 0 and connection_alerts == []
    assert not asyncio.all_tasks() - prior_tasks
