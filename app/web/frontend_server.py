"""独立静态前端服务及同源 API 反向代理，不向 FastAPI 注册页面路由。"""

from __future__ import annotations

import argparse
import http.client
import mimetypes
import re
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import unquote, urlsplit

from loguru import logger

from app.core.http_limits import request_body_limit
from app.core.sanitize import sanitize_diagnostic
from app.web.security_headers import SECURITY_HEADERS

_API_PREFIXES = ("/api", "/auth", "/review/api", "/collection/api")
_STATIC_MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
_API_TARGET = (
    r"/(?:api|auth|review/api|collection/api)(?:/[^\x00-\x20\x7f?#\\]*)?(?:\?[^\x00-\x20\x7f#\\]*)?"
    r"|/openapi\.json(?:\?[^\x00-\x20\x7f#\\]*)?"
)
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_UI_ROUTE = re.compile(
    r"/(?:rooms|recording|imports|schedules|transcripts|danmaku|sessions|topics|clips|uploads|trends|tasks|analytics|monitor|logs"
    r"|settings(?:/(?:configuration|automation|llm|login|plugins|subtitles|intros|notifications))?"
    r"|review(?:/(?:queue|[0-9]+))?|collection/[0-9]+|plugins/[^/]+/settings)?/?$"
)


def _response_header(key: str, value: str) -> tuple[str, str]:
    """仅转发合法的单行响应头；规范化前后不一致时拒绝整个响应。"""
    name = key.replace("\r", "").replace("\n", "")
    content = value.replace("\r", "").replace("\n", "")
    if (
        name != key
        or content != value
        or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None
        or re.fullmatch(r"[^\x00-\x08\x0a-\x1f\x7f]*", content) is None
    ):
        raise ValueError("invalid backend response header")
    return name, content


class FrontendHTTPServer(ThreadingHTTPServer):
    """绑定静态服务不做反向 DNS 查询，启动不依赖主机名解析服务。"""

    def server_bind(self) -> None:
        """沿用 TCP 绑定规则，以实际监听地址填写 HTTP 服务元数据。"""
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class FrontendHandler(BaseHTTPRequestHandler):
    """流式转发媒体与大文件，静态文件只从指定构建目录读取。"""

    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args,
        directory: Path,
        backend: str,
        trusted_proxies: tuple[IPv4Network | IPv6Network, ...] = (),
        **kwargs,
    ):
        self.directory = directory.resolve()
        self.backend = urlsplit(backend)
        self.trusted_proxies = trusted_proxies
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        """读取页面、静态资产或后端接口。"""
        self._dispatch()

    def do_HEAD(self) -> None:
        """读取响应头。"""
        self._dispatch()

    def do_POST(self) -> None:
        """转发后端写操作。"""
        self._dispatch()

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST
    do_OPTIONS = do_POST

    def log_request(self, code="-", size="-") -> None:
        """与后端使用同一日志格式，不记录查询参数及其可能携带的凭据。"""
        path = sanitize_diagnostic(urlsplit(self.path).path)
        logger.info('{}:{} - "{} {}" {}', self.client_address[0], self.client_address[1], self.command, path, code)

    def log_message(self, format, *args) -> None:
        """替换 http.server 默认 stderr 格式并脱敏错误诊断。"""
        logger.warning("前端请求处理：{}", sanitize_diagnostic(format % args))

    def _dispatch(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or not self.path.startswith("/"):
            self.send_error(400, "Only origin-form requests are accepted")
            self.close_connection = True
            return
        path = parsed.path
        if path == "/openapi.json" or any(path == prefix or path.startswith(prefix + "/") for prefix in _API_PREFIXES):
            self._proxy()
            return
        if self.command not in {"GET", "HEAD"}:
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        try:
            decoded = unquote(path, errors="strict")
            target = (self.directory / decoded.lstrip("/")).resolve()
            if not target.is_relative_to(self.directory) or "\x00" in decoded:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not target.is_file() and _UI_ROUTE.fullmatch(decoded):
                target = self.directory / "index.html"
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            with target.open("rb") as stream:
                size = target.stat().st_size
                self.send_response(200)
                content_type = (
                    _STATIC_MIME.get(target.suffix.lower())
                    or mimetypes.guess_type(str(target))[0]
                    or "application/octet-stream"
                )
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header(
                    "Cache-Control",
                    "public, max-age=31536000, immutable" if target.parent.name == "assets" else "no-store",
                )
                for name, value in SECURITY_HEADERS.items():
                    self.send_header(name, value)
                if target.name == "media-auth.js":
                    self.send_header("Service-Worker-Allowed", "/")
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := stream.read(65536):
                        self.wfile.write(chunk)
        except (OSError, UnicodeError, ValueError):
            self.close_connection = True

    def _proxy(self) -> None:
        target = self.path
        if not re.fullmatch(_API_TARGET, target):
            self.send_error(400, "Invalid API request target")
            self.close_connection = True
            return
        # 禁止不明确的消息边界，避免前后端对同一请求产生不同解释。
        lengths = self.headers.get_all("Content-Length", [])
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if len(lengths) > 1 or (lengths and transfer) or transfer not in {"", "chunked"}:
            self.send_error(400, "Ambiguous request framing")
            self.close_connection = True
            return
        try:
            length = int(lengths[0]) if lengths else 0
            if length < 0:
                raise ValueError
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            self.close_connection = True
            return
        limit = request_body_limit(unquote(urlsplit(target).path), self.command)
        if length > limit:
            self.send_error(413, "Request body too large")
            self.close_connection = True
            return
        peer = ip_address(self.client_address[0])
        trusted = any(peer in network for network in self.trusted_proxies)
        forwarded_for = self.client_address[0]
        forwarded_proto = "http"
        if trusted:
            forwarded = self.headers.get("X-Forwarded-For", "")
            proto = self.headers.get("X-Forwarded-Proto", "http")
            try:
                if not forwarded or len(self.headers.get_all("X-Forwarded-Proto", [])) > 1:
                    raise ValueError("missing or ambiguous trusted forwarding")
                chain = [ip_address(item.strip()) for item in forwarded.split(",")]
                if proto not in {"http", "https"}:
                    raise ValueError("invalid forwarded protocol")
            except ValueError:
                self.send_error(400, "Invalid trusted proxy headers")
                self.close_connection = True
                return
            # 从最靠近本机的节点向外查找第一个非可信来源。
            for address in reversed(chain):
                forwarded_for = str(address)
                if not any(address in network for network in self.trusted_proxies):
                    break
            forwarded_proto = proto
        connection = http.client.HTTPConnection(self.backend.hostname, self.backend.port or 80, timeout=600)
        started = False
        try:
            connection_fields = {field.strip().lower() for field in self.headers.get("Connection", "").split(",")}
            if connection_fields & {"host", "content-length", "transfer-encoding", "authorization", "cookie"}:
                raise ValueError("invalid connection fields")
            ignored = (
                _HOP_HEADERS
                | connection_fields
                | {"forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip"}
            )
            headers = {key: value for key, value in self.headers.items() if key.lower() not in ignored}
            headers["Connection"] = "close"
            headers["X-Forwarded-For"] = forwarded_for
            headers["X-Forwarded-Proto"] = forwarded_proto
            connection.putrequest(self.command, target, skip_host=True, skip_accept_encoding=True)
            if transfer:
                headers["Transfer-Encoding"] = "chunked"
            for key, value in headers.items():
                connection.putheader(key, value)
            connection.endheaders()
            if transfer:
                self._forward_chunks(connection, limit)
            else:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ConnectionError("incomplete upload")
                    connection.send(chunk)
                    remaining -= len(chunk)
            response = connection.getresponse()
            connection_headers = {item.strip().lower() for item in response.getheader("Connection", "").split(",")}
            response_headers = [
                _response_header(key, value)
                for key, value in response.getheaders()
                if key.lower() not in _HOP_HEADERS | connection_headers
            ]
            self.send_response(response.status)
            started = True
            for key, value in response_headers:
                self.send_header(key, value)
            # 使用关闭连接结束流式响应，保留 Range、Set-Cookie、CSRF 和限流响应头。
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except OverflowError:
            if not started:
                self.send_error(413, "Request body too large")
        except (OSError, http.client.HTTPException, ValueError):
            if not started:
                self.send_error(502, "Backend unavailable")
        finally:
            self.close_connection = True
            connection.close()

    def _forward_chunks(self, connection: http.client.HTTPConnection, limit: int) -> None:
        total = 0
        while True:
            line = self.rfile.readline(128)
            if len(line) >= 128 or not line.endswith(b"\r\n"):
                raise ValueError("invalid chunk size")
            size = int(line.split(b";", 1)[0], 16)
            if size < 0:
                raise ValueError("invalid chunk size")
            total += size
            if total > limit:
                raise OverflowError("request body too large")
            connection.send(f"{size:x}\r\n".encode())
            if size == 0:
                # 这里只接受空 trailer，不把未经校验的尾部请求头传给后端。
                if self.rfile.readline(2) != b"\r\n":
                    raise ValueError("trailers not supported")
                connection.send(b"\r\n")
                return
            remaining = size
            while remaining:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    raise ConnectionError("incomplete chunk")
                connection.send(chunk)
                remaining -= len(chunk)
            if self.rfile.read(2) != b"\r\n":
                raise ValueError("invalid chunk boundary")
            connection.send(b"\r\n")


def validate_backend(backend: str) -> None:
    """校验固定上游地址，在启动服务前拒绝无效端口、路径或凭据。"""
    target = urlsplit(backend)
    if (
        target.scheme != "http"
        or not target.hostname
        or target.path not in {"", "/"}
        or target.query
        or target.fragment
        or target.username is not None
        or target.password is not None
    ):
        raise ValueError("backend 必须是无路径、无凭据的 HTTP 地址")
    if target.port is not None and not 1 <= target.port <= 65535:
        raise ValueError("backend 端口必须在 1 到 65535 之间")


def serve_frontend(directory: Path, backend: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    """启动前端服务；后端地址由操作者配置，不能由浏览器请求改变。"""
    validate_backend(backend)
    if not (directory / "index.html").is_file():
        raise FileNotFoundError(f"未找到前端构建产物: {directory}，请先执行 npm ci && npm run build")
    from app.core.config import get_settings

    proxy_setting = get_settings().frontend_trusted_proxy_ips
    proxies = tuple(ip_network(item, strict=False) for item in proxy_setting.split(",") if item)
    server = FrontendHTTPServer(
        (host, port), partial(FrontendHandler, directory=directory, backend=backend, trusted_proxies=proxies)
    )
    server.daemon_threads = True
    logger.info("前端服务 -> http://{}:{}，API -> {}", host, port, backend)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    """通过 python -m app.web.frontend_server 启动静态服务。"""
    from app.core.logging import setup_logging

    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("frontend/dist"))
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve_frontend(args.directory, args.backend, args.host, args.port)


if __name__ == "__main__":
    main()
