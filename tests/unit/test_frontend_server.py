"""独立前端服务的真实 HTTP 代理与静态路由契约。"""

from __future__ import annotations

import http.client
import socket
import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_network
from pathlib import Path

import pytest

from app.web.frontend_server import FrontendHandler, FrontendHTTPServer


@pytest.fixture
def gateway(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[tuple[int, list[dict[str, object]]]]:
    (tmp_path / "index.html").write_text("<main>react app</main>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main.js").write_text("console.log('ok')")
    observed: list[dict[str, object]] = []

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append({"path": self.path, "headers": self.headers})
            body = b"video" if "video" in self.path else b'{"ok":true}'
            self.send_response(206 if "video" in self.path else 200)
            self.send_header("Content-Type", "video/mp4" if "video" in self.path else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "session=opaque; HttpOnly; Path=/")
            if "folded-header" in self.path:
                self.send_header("X-Broken", "one\r\n\tInjected: two")
            if "video" in self.path:
                self.send_header("Content-Range", "bytes 0-4/100")
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            if self.headers.get("Transfer-Encoding") == "chunked":
                chunks = []
                while size := int(self.rfile.readline(), 16):
                    chunks.append(self.rfile.read(size))
                    assert self.rfile.read(2) == b"\r\n"
                assert self.rfile.read(2) == b"\r\n"
                body = b"".join(chunks)
            else:
                body = self.rfile.read(int(self.headers["Content-Length"]))
            observed.append({"body": body, "headers": self.headers})
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    frontend = FrontendHTTPServer(
        ("127.0.0.1", 0),
        partial(
            FrontendHandler,
            directory=tmp_path,
            backend=f"http://127.0.0.1:{backend.server_port}",
            trusted_proxies=tuple(ip_network(value) for value in getattr(request, "param", ())),
        ),
    )
    for server in (backend, frontend):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield frontend.server_port, observed
    for server in (frontend, backend):
        server.shutdown()
        server.server_close()


def request(port, path, method="GET", body=None, headers=None, **kwargs):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {}, **kwargs)
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def test_proxy_delivers_small_sse_frames_before_upstream_closes(tmp_path: Path) -> None:
    release, finished = threading.Event(), threading.Event()
    observed: list[str] = []
    frame = b'event: resync\ndata: {"version":1,"topics":[]}\n\n'

    class StreamingBackend(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            observed.append(self.headers.get("Last-Event-ID", ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(frame)
            self.wfile.flush()
            release.wait(5)
            finished.set()

        def log_message(self, _format: str, *args: object) -> None:
            return

    backend = ThreadingHTTPServer(("127.0.0.1", 0), StreamingBackend)
    frontend = FrontendHTTPServer(
        ("127.0.0.1", 0),
        partial(FrontendHandler, directory=tmp_path, backend=f"http://127.0.0.1:{backend.server_port}"),
    )
    threads = [threading.Thread(target=server.serve_forever) for server in (backend, frontend)]
    connection = http.client.HTTPConnection("127.0.0.1", frontend.server_port, timeout=2)
    try:
        for thread in threads:
            thread.start()
        connection.request("GET", "/api/events", headers={"Last-Event-ID": "opaque-cursor"})
        response = connection.getresponse()
        assert response.status == 200 and response.getheader("X-Accel-Buffering") == "no"
        assert response.read(len(frame)) == frame
        assert not finished.is_set()
        assert observed == ["opaque-cursor"]
    finally:
        release.set()
        connection.close()
        for server in (frontend, backend):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()


def test_frontend_starts_without_reverse_dns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def forbidden(*args):
        pytest.fail("静态前端启动不应等待反向 DNS")

    monkeypatch.setattr(socket, "getfqdn", forbidden)
    (tmp_path / "index.html").write_text("<main>frontend without DNS</main>")
    server = FrontendHTTPServer(
        ("127.0.0.1", 0), partial(FrontendHandler, directory=tmp_path, backend="http://127.0.0.1:1")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server.server_port, "/")[2] == b"<main>frontend without DNS</main>"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_spa_and_assets_have_separate_cache_and_fallback_rules(gateway):
    port, _ = gateway
    assert request(port, "/settings/configuration")[2] == b"<main>react app</main>"
    assert request(port, "/settings/notifications")[2] == b"<main>react app</main>"
    assert request(port, "/review/123")[0] == 200
    assert request(port, "/collection/1")[0] == 200
    assert request(port, "/assets/missing.js")[0] == 404
    assert request(port, "/unknown")[0] == 404
    assert request(port, "/%2e%2e/secret")[0] == 404
    assert request(port, "/")[1]["Cache-Control"] == "no-store"
    assert "immutable" in request(port, "/assets/main.js")[1]["Cache-Control"]


def test_frontend_access_uses_application_logger_without_query_credentials(gateway, monkeypatch):
    from app.web import frontend_server

    messages = []
    monkeypatch.setattr(frontend_server.logger, "info", lambda fmt, *args: messages.append(fmt.format(*args)))
    port, _ = gateway
    assert request(port, "/api/notifications?token=must-not-log")[0] == 200
    assert len(messages) == 1
    assert '"GET /api/notifications" 200' in messages[0]
    assert "must-not-log" not in messages[0]


def test_backend_proxy_access_is_suppressed_but_business_logs_remain(monkeypatch):
    import logging

    from app.core import logging as application_logging

    messages = []
    monkeypatch.setenv("BLC_FRONTEND_PORT", "8000")
    handler = application_logging._ApplicationLogHandler()
    sink = application_logging.logger.add(lambda message: messages.append(message.record["message"]))
    try:
        handler.emit(logging.LogRecord("uvicorn.access", logging.INFO, "", 1, "%s", ("proxy access",), None))
        assert messages == []
        handler.emit(logging.LogRecord("app.recording", logging.INFO, "", 1, "business message", (), None))
        assert messages == ["business message"]
    finally:
        application_logging.logger.remove(sink)


def test_proxy_preserves_media_range_auth_csrf_and_cookies(gateway):
    port, observed = gateway
    status, headers, body = request(
        port,
        "/api/clips/1/video",
        headers={
            "Host": "localhost:8080",
            "Range": "bytes=0-4",
            "Authorization": "Basic test",
            "X-Forwarded-For": "evil",
        },
    )
    assert status == 206 and body == b"video"
    assert headers["Content-Range"] == "bytes 0-4/100"
    assert "HttpOnly" in headers["Set-Cookie"]
    forwarded = observed[-1]["headers"]
    assert forwarded["Host"] == "localhost:8080"
    assert forwarded["Authorization"] == "Basic test"
    assert forwarded["Range"] == "bytes=0-4"
    assert forwarded["X-Forwarded-For"] == "127.0.0.1"


@pytest.mark.parametrize("path", ["http://elsewhere.invalid/api/test", "/api/test#fragment", "/api/test\\path"])
def test_proxy_rejects_ambiguous_targets_without_contacting_backend(gateway, path):
    port, observed = gateway
    assert request(port, path)[0] == 400
    assert observed == []


def test_proxy_keeps_url_query_values_on_fixed_backend(gateway):
    port, observed = gateway
    path = "/api/test?url=https%3A%2F%2Felsewhere.invalid%2Fasset&text=%E5%BC%B9%E5%B9%95"
    assert request(port, path)[0] == 200
    assert observed[-1]["path"] == path


def test_proxy_rejects_folded_backend_headers_before_sending_response(gateway):
    port, _ = gateway
    status, headers, _ = request(port, "/api/folded-header")
    assert status == 502
    assert "X-Broken" not in headers and "Injected" not in headers


@pytest.mark.parametrize("chunked", [False, True])
def test_import_upload_streams_without_losing_request_body(gateway, chunked):
    port, observed = gateway
    body = b"large video payload" * 10000
    headers = {"Origin": "http://localhost:8080", "X-BLC-CSRF": "csrf-token"}
    source = iter([body[:50000], body[50000:]]) if chunked else body
    assert (
        request(port, "/api/recording-imports/abc/files/video", "PUT", source, headers, encode_chunked=chunked)[0]
        == 200
    )
    assert observed[-1]["body"] == body
    assert observed[-1]["headers"]["Origin"] == "http://localhost:8080"
    assert observed[-1]["headers"]["X-BLC-CSRF"] == "csrf-token"


def test_static_responses_include_the_production_csp(gateway: tuple[int, list[dict[str, object]]]) -> None:
    from app.web.security_headers import SECURITY_HEADERS

    port, _ = gateway
    for path in ("/", "/assets/main.js"):
        status, headers, _ = request(port, path)
        assert status == 200
        assert all(headers.get(key) == value for key, value in SECURITY_HEADERS.items())
    assert "immutable" not in request(port, "/assets/missing.js")[1].get("Cache-Control", "")


def test_untrusted_peer_cannot_supply_forwarded_identity(gateway: tuple[int, list[dict[str, object]]]) -> None:
    port, observed = gateway
    status, _, _ = request(
        port,
        "/api/check",
        headers={"X-Forwarded-For": "192.0.2.10", "X-Forwarded-Proto": "https", "X-Real-IP": "198.51.100.1"},
    )
    assert status == 200
    headers = observed[-1]["headers"]
    assert isinstance(headers, http.client.HTTPMessage)
    assert headers["X-Forwarded-For"] == "127.0.0.1"
    assert headers["X-Forwarded-Proto"] == "http"
    assert headers.get("X-Real-IP") is None


@pytest.mark.parametrize("gateway", [("127.0.0.1/32",)], indirect=True)
def test_trusted_https_proxy_preserves_public_origin(gateway: tuple[int, list[dict[str, object]]]) -> None:
    port, observed = gateway
    status, _, _ = request(
        port,
        "/api/check",
        headers={
            "Host": "console.example",
            "Origin": "https://console.example",
            "X-Forwarded-For": "192.0.2.10",
            "X-Forwarded-Proto": "https",
        },
    )
    assert status == 200
    headers = observed[-1]["headers"]
    assert isinstance(headers, http.client.HTTPMessage)
    assert headers["Host"] == "console.example" and headers["Origin"] == "https://console.example"
    assert headers["X-Forwarded-For"] == "192.0.2.10" and headers["X-Forwarded-Proto"] == "https"


@pytest.mark.parametrize("gateway", [("127.0.0.1/32",)], indirect=True)
def test_trusted_proxy_without_client_identity_is_rejected(gateway: tuple[int, list[dict[str, object]]]) -> None:
    port, observed = gateway
    assert request(port, "/api/check", headers={"X-Forwarded-Proto": "https"})[0] == 400
    assert observed == []


@pytest.mark.parametrize("chunked", [False, True])
def test_frontend_rejects_oversized_management_upload(
    gateway: tuple[int, list[dict[str, object]]], monkeypatch: pytest.MonkeyPatch, chunked: bool
) -> None:
    from app.core import http_limits

    monkeypatch.setattr(http_limits, "MAX_MANAGEMENT_BYTES", 32)
    port, _ = gateway
    body = iter([b"a" * 20, b"b" * 20]) if chunked else b"a" * 40
    assert request(port, "/api/settings", "PUT", body, encode_chunked=chunked)[0] == 413
