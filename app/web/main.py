"""FastAPI 应用入口。

* 启动时初始化日志与数据库;关闭时停止所有录制任务;
* 挂载 REST API 与媒体路由，页面由独立前端服务提供;
* 提供单页仪表盘。

启动方式::

    python -m app.cli serve            # 推荐
    uvicorn app.web.main:app --reload --timeout-graceful-shutdown 10  # 开发热重载
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from ipaddress import ip_address

import psutil
from fastapi import FastAPI, Request
from loguru import logger

from app import __version__, __version_label__
from app.core.execution import run_sync
from app.core.logging import setup_logging
from app.core.supervisor import Service, Supervisor
from app.db.service_ownership import ServiceOwnership
from app.db.session import init_db
from app.recording.schedule_runner import ScheduleRunner
from app.web.routers.api import router as api_router
from app.web.routers.collection_router import collection_router
from app.web.routers.intro_template_router import router as intro_template_router
from app.web.routers.monitor_router import monitor_router
from app.web.routers.review_router import review_router
from app.web.routers.subtitle_template_router import router as subtitle_template_router
from app.web.services import notifications as notifications_service
from app.web.services import rooms as rooms_service


def _setup_proxy_env() -> None:
    """代理环境变量规范化 (V0.1.14.11)。

    不篡改用户代理配置 (HTTP_PROXY/HTTPS_PROXY/ALL_PROXY),
    但确保 localhost 始终绕过代理以支持本地 Dashboard 访问。

    NO_PROXY 合并规则:
    - 如果用户已设置 NO_PROXY, 追加 localhost 项
    - 如果用户未设置, 设置 NO_PROXY=127.0.0.1,localhost,::1

    SOCKS 诊断:
    - 如果检测到 ALL_PROXY 使用 socks:// scheme,
      记录警告并建议用户确认 httpx[socks] 已安装。
    """
    import os as _os

    no_proxy_defaults = ["127.0.0.1", "localhost", "::1"]
    existing = _os.environ.get("NO_PROXY", "") or _os.environ.get("no_proxy", "")

    if existing:
        existing_items = [x.strip() for x in existing.split(",") if x.strip()]
        for item in no_proxy_defaults:
            if item not in existing_items:
                existing_items.append(item)
        _os.environ["NO_PROXY"] = ",".join(existing_items)
    else:
        _os.environ["NO_PROXY"] = ",".join(no_proxy_defaults)

    # SOCKS diagnostic
    all_proxy = _os.environ.get("ALL_PROXY", "") or _os.environ.get("all_proxy", "")
    if all_proxy and all_proxy.lower().startswith("socks"):
        logger.warning(
            "检测到 SOCKS 代理配置。"
            "请确认 httpx[socks] 已安装, 否则本地 Dashboard 请求可能失败。"
            "安装: pip install httpx[socks]",
        )


def _non_loopback_interface_addresses() -> list[str]:
    """读取本机网卡地址，不通过 DNS/mDNS 解析主机名。"""
    return sorted(
        {
            address.address
            for addresses in psutil.net_if_addrs().values()
            for address in addresses
            if address.family in (socket.AF_INET, socket.AF_INET6) and not ip_address(address.address).is_loopback
        }
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """先取得实例所有权，再初始化数据库并恢复后台工作。"""
    from app.db.session import engine

    ownership = await run_sync(partial(ServiceOwnership, runtime_app_root(), engine), lambda owner: owner.release())
    try:
        async with _application_lifespan(_app, ownership):
            yield
    finally:
        ownership.release()


@asynccontextmanager
async def _application_lifespan(_app: FastAPI, ownership: ServiceOwnership) -> AsyncIterator[None]:
    """应用生命周期:启动初始化、启动 TaskWorker、自动恢复、预约调度、关闭时优雅停止。"""
    setup_logging()
    await run_sync(init_db)
    from app.recording.danmaku import freeze_interrupted_captures

    await run_sync(freeze_interrupted_captures)

    # V0.1.13: Web 安全兜底 — 启动时检查 host+password 配置
    from app.core.config import settings as _cfg_startup

    if _cfg_startup.docker_auth_enabled:
        auth_state = await run_sync(AuthStore(runtime_app_root()).state)
        logger.info(
            "Docker 后台认证：{}",
            "等待使用一次性初始化码设置管理员密码" if auth_state["setup_required"] else "网页登录已启用",
        )
    elif not _cfg_startup.admin_password:
        try:
            non_loopback_ips = await run_sync(_non_loopback_interface_addresses)
        except OSError as exc:
            logger.opt(exception=exc).warning(
                "安全警告: ADMIN_PASSWORD 为空，无法枚举本机网络接口。Web 将禁止非本机请求访问 /api 路由。"
            )
        else:
            if non_loopback_ips:
                logger.warning(
                    "安全警告: ADMIN_PASSWORD 为空, 但主机存在非 loopback 接口: {}. "
                    "Web 将禁止非本机请求访问 /api 路由。",
                    non_loopback_ips[:3],
                )
    # V0.1.13: 启动 Metrics 后台采样
    from app.core.metrics import start_metrics_collector, stop_metrics_collector

    # V0.1.14.11: 代理环境变量规范化
    # 不篡改用户代理配置 (HTTP_PROXY/HTTPS_PROXY/ALL_PROXY), 但确保 localhost 绕过代理
    _setup_proxy_env()

    # V0.1.6:启动持久化任务队列 Worker。
    from app.analysis.llm import close_clients
    from app.notify.delivery import request_stop_delivery, start_email_delivery, stop_email_delivery
    from app.pipeline.lifecycle import shutdown_event
    from app.pipeline.task_worker import get_worker
    from app.plugins.manager import plugin_manager
    from app.trends.scheduler import trend_scheduler
    from app.web.services.background_jobs import web_job_manager

    task_worker = get_worker()
    # V0.1.7 P3:启动开播自动录制监控器。
    from app.pipeline.live_monitor import live_monitor
    from app.web.qr_login import qr_logins

    async def start_metrics() -> None:
        start_metrics_collector(interval_s=60)

    async def start_trends() -> None:
        trend_scheduler.start(recording_active=lambda: bool(rooms_service.recorder_manager.running_ids()))

    async def recover_recordings() -> None:
        recovered = await rooms_service.auto_recover_interrupted_sessions()
        if recovered:
            logger.info("已恢复 {} 个中断的录制会话。", len(recovered))

    def request_stop() -> None:
        shutdown_event.set()
        rooms_service.recorder_manager.request_stop_all()

    schedules = _schedule_runner()
    supervisor = Supervisor(
        [
            Service("qr_login", stop=qr_logins.stop),
            Service("model_connections", stop=partial(run_sync, close_clients)),
            Service("email_delivery", start_email_delivery, stop_email_delivery, request_stop_delivery),
            Service("metrics", start_metrics, partial(run_sync, stop_metrics_collector)),
            Service("trends", start_trends, trend_scheduler.stop),
            Service("plugins", plugin_manager.start, plugin_manager.stop),
            Service("recording_settlement", rooms_service.settle_interrupted_recordings),
            Service("worker", task_worker.start, task_worker.stop, task_worker.request_stop),
            Service("web_jobs", web_job_manager.start, web_job_manager.stop, web_job_manager.request_stop),
            Service("recorders", recover_recordings, rooms_service.recorder_manager.stop_all),
            Service("schedules", schedules.start, schedules.stop),
            Service("live_monitor", live_monitor.start, live_monitor.stop),
        ],
        request_stop,
        shutdown_timeout_s=lambda: _cfg_startup.worker_shutdown_timeout_seconds,
    )
    ownership.supervisor = supervisor
    async with supervisor.running():
        logger.info("Web 后台已启动。")
        yield
    logger.info("Web 服务资源已关闭；操作系统进程退出由服务器调用方确认。")


def _schedule_runner() -> ScheduleRunner:
    return ScheduleRunner(
        rooms_service.recorder_manager,
        partial(notifications_service.push_notification, kind="success"),
    )


async def _run_due_schedules() -> None:
    """每个到期预约仅产生一个后继，失败和已录制也完成本次调度。"""
    await _schedule_runner().run_due()


app = FastAPI(
    title="BiliLiveCut 控制台",
    version=f"{__version_label__} ({__version__})",
    lifespan=lifespan,
)


from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402


@app.exception_handler(RequestValidationError)
async def safe_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """校验错误只返回字段位置和类型，输入内容可能包含密码或 Cookie。"""
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": list(error["loc"]), "type": error["type"], "msg": "请求字段缺失、类型或格式不正确"}
                for error in exc.errors()
            ]
        },
    )


@app.exception_handler(Exception)
async def report_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """为未处理的请求故障生成定位编号，详细安全异常链写入统一日志。"""
    from uuid import uuid4

    reference = uuid4().hex[:12]
    logger.opt(exception=exc).error(
        "Web 请求失败 reference={} method={} path={}", reference, request.method, request.url.path
    )
    return JSONResponse(status_code=500, content={"detail": f"服务处理失败，请查看日志，编号 {reference}"})


# ── 认证中间件(V0.1.8.2) ──────────────────────────────────────────────────
# 当 ADMIN_PASSWORD 被设置时,所有 /api/* /review/* /collection/* 路由要求 Basic Auth。
# 页面浏览(GET /)和静态资源不受影响。
# 请求头格式: Authorization: Basic <base64(admin:<密码>)>
import base64 as _base64  # noqa: E402
import ipaddress as _ipaddress  # noqa: E402
import json as _json  # noqa: E402
import secrets as _secrets  # noqa: E402
from urllib.parse import urlsplit as _urlsplit  # noqa: E402

from starlette.middleware.base import BaseHTTPMiddleware as _BaseMiddleware  # noqa: E402
from starlette.middleware.base import RequestResponseEndpoint as _RequestResponseEndpoint  # noqa: E402
from starlette.responses import JSONResponse as _JSONResponse  # noqa: E402
from starlette.responses import Response as _Response  # noqa: E402

from app.core.config import settings as _cfg  # noqa: E402

_ADMIN_PASSWORD = _cfg.admin_password

from app.core.docker_auth import AuthStore  # noqa: E402
from app.web import docker_auth as _docker_auth  # noqa: E402
from config.launcher_settings import runtime_app_root  # noqa: E402

_DOCKER_AUTH = (
    AuthStore(
        runtime_app_root(), idle_seconds=_cfg.docker_auth_idle_seconds, session_seconds=_cfg.docker_auth_session_seconds
    )
    if _cfg.docker_auth_enabled
    else None
)


def _load_reviewer_passwords(raw: str) -> dict[str, str]:
    """解析审核员配置，拒绝无效账号及已知模板占位密码。"""
    if not raw.strip():
        return {}
    try:
        value = _json.loads(raw)
    except _json.JSONDecodeError:
        logger.error("REVIEWER_ACCOUNTS_JSON 不是有效 JSON，审核员登录已禁用")
        return {}
    if not isinstance(value, dict):
        logger.error("REVIEWER_ACCOUNTS_JSON 必须是账号到密码的 JSON 对象")
        return {}
    accounts = {
        username: password
        for username, password in value.items()
        if isinstance(username, str)
        and isinstance(password, str)
        and username.strip()
        and username == username.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in username)
        and password.strip()
        and password.strip().casefold() != "change-me"
        and username.casefold() != "admin"
    }
    if len(accounts) != len(value):
        logger.error("REVIEWER_ACCOUNTS_JSON 含无效账号，相关条目已忽略")
    return accounts


_REVIEWER_PASSWORDS = _load_reviewer_passwords(_cfg.reviewer_accounts_json)

_AUTH_PROTECTED_PREFIXES = ("/api/", "/review/", "/collection/", "/docs", "/redoc", "/openapi.json")
_AUTH_WHITE_LIST = tuple()  # 未来可扩展公开端点

# ── 登录失败限流 ────────────────────────────────────────────────────────────
_LOGIN_FAILURES: dict[str, list[float]] = {}
_MAX_LOGIN_ATTEMPTS = 5
_LOGIN_WINDOW_S = 300  # 5 分钟窗口


def _check_login_rate(ip: str) -> bool:
    """检查指定 IP 的登录失败次数是否在限流窗口内超限。

    :param ip: 客户端 IP 地址。
    :returns: ``True`` 表示允许继续尝试,``False`` 表示已触发限流。
    """
    now = _time.time()
    timestamps = _LOGIN_FAILURES.get(ip, [])
    # 清理窗口外的旧时间戳
    _LOGIN_FAILURES[ip] = [t for t in timestamps if now - t <= _LOGIN_WINDOW_S]
    return len(_LOGIN_FAILURES[ip]) < _MAX_LOGIN_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    """记录一次登录失败的时间戳。

    :param ip: 客户端 IP 地址。
    """
    now = _time.time()
    if ip not in _LOGIN_FAILURES:
        _LOGIN_FAILURES[ip] = []
    _LOGIN_FAILURES[ip].append(now)


class _AuthMiddleware(_BaseMiddleware):
    """轻量 Basic Auth 中间件 (V0.1.13 强化: loopback 守卫)。

    当 ADMIN_PASSWORD 为空时:
    - loopback 请求 → 允许
    - 非 loopback 请求 → 拒绝 (401)

    当 ADMIN_PASSWORD 设置后: 使用 secrets.compare_digest 校验。
    修改状态的请求额外检查 Origin/Referer (CSRF 保护)。
    """

    def _is_loopback(self, host: str) -> bool:
        """判断 IP 地址是否为 loopback 或测试环境。

        :param host: 客户端 IP 地址字符串。
        :returns: ``True`` 表示是 loopback 地址或测试客户端。
        """
        normalized = host.strip().lower()
        if normalized in ("localhost", "testclient"):
            return True
        try:
            return _ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _parse_authority(authority: str) -> tuple[str, str] | None:
        """使用标准 URL 解析器读取 ``host[:port]``，包括 bracket IPv6。"""
        authority = authority.strip()
        if not authority or any(char.isspace() for char in authority):
            return None
        try:
            parsed = _urlsplit(f"//{authority}")
            if parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
                return None
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return None
        if not hostname or authority.endswith(":"):
            return None
        return hostname.lower(), str(port) if port is not None else ""

    @classmethod
    def _is_local_authority(cls, authority: str) -> bool:
        """判断 Host authority 是否明确指向本机 loopback。"""
        parsed = cls._parse_authority(authority)
        if parsed is None:
            return False
        hostname, _port = parsed
        if hostname == "localhost":
            return True
        try:
            return _ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _is_modifying(self, request: Request) -> bool:
        """检查请求是否为状态修改类请求 (POST/PUT/PATCH/DELETE)。"""
        return request.method in ("POST", "PUT", "PATCH", "DELETE")

    @classmethod
    def _check_csrf(cls, request: Request) -> bool:
        """跨站请求伪造检查 (V0.1.14.11 强化)。

        浏览器修改请求必须带有与当前请求完全同源的 Origin。
        无 Origin 的非浏览器客户端通过（依赖 Basic Auth）。

        比较规则:
        - 解析完整的 (scheme, hostname, port)
        - 仅折叠 scheme 对应的默认端口: http→80, https→443
        - http://host:443 ≠ http://host (443 不是 http 默认端口)
        - 支持 IPv6 bracket 形式
        - 非法 Origin 返回 False
        """
        origin = request.headers.get("Origin", "")

        # 无 Origin → 非浏览器客户端, 依赖 Basic Auth
        if not origin:
            return True

        # ── Parse origin ──
        parsed = cls._parse_origin(origin)
        if parsed is None:
            return False

        # ── Build expected from request ──
        scheme = request.url.scheme or "http"
        authority = cls._parse_authority(request.headers.get("Host", ""))
        if authority is None:
            return False
        hostname, port_hint = authority

        # Effective port: explicit port on Host header, or default for scheme
        effective_port = port_hint if port_hint else ("443" if scheme == "https" else "80")

        # ── Compare (scheme, hostname, port) ──
        origin_scheme, origin_host, origin_port = parsed

        if origin_scheme != scheme:
            return False
        if origin_host != hostname:
            return False

        # Port comparison: only collapse when both sides use default port for their scheme
        origin_port_for_compare = origin_port if origin_port else ("443" if origin_scheme == "https" else "80")
        expected_port_for_compare = effective_port

        return origin_port_for_compare == expected_port_for_compare

    @classmethod
    def _parse_origin(cls, origin: str) -> tuple[str, str, str] | None:
        """Parse an Origin header into (scheme, normalized_host, port).

        Handles:
        - https://example.com → (https, example.com, )
        - http://example.com:8080 → (http, example.com, 8080)
        - http://[::1]:8000 → (http, ::1, 8000)
        - Invalid origins → None

        :param origin: Origin header value.
        :returns: (scheme, hostname, port) or None if invalid.
        """
        origin = origin.strip()
        if not origin:
            return None

        try:
            parsed = _urlsplit(origin)
        except ValueError:
            return None
        scheme = parsed.scheme.lower()
        if (
            scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return None
        authority = cls._parse_authority(parsed.netloc)
        if authority is None:
            return None
        hostname, port = authority
        return scheme, hostname, port

    async def dispatch(self, request: Request, call_next: _RequestResponseEndpoint) -> _Response:
        """先检查认证冷却，再验证凭据、角色及修改请求的来源。"""
        path = request.url.path
        request.state.auth_user = "anonymous"
        request.state.auth_role = "anonymous"
        if _DOCKER_AUTH is not None:
            return await _docker_auth.dispatch(
                request, call_next, _DOCKER_AUTH, _REVIEWER_PASSWORDS, self._check_csrf, self._reviewer_path_allowed
            )
        if path == "/auth/status" and request.method == "GET":
            return _JSONResponse({"enabled": False}, headers={"Cache-Control": "no-store"})
        protected = any(path.startswith(p) for p in _AUTH_PROTECTED_PREFIXES)
        if not protected:
            return await call_next(request)
        if path.startswith(_AUTH_WHITE_LIST):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"  # noqa: SLF001

        # 无密码时只允许可确认的本机来源。
        if not _ADMIN_PASSWORD:
            if not self._is_loopback(client_ip):
                logger.warning("access_denied: non-loopback request without ADMIN_PASSWORD from {}", client_ip)
                return _JSONResponse(
                    {"detail": "安全策略: 未设置 ADMIN_PASSWORD 时仅允许本机访问管理接口。"},
                    status_code=403,
                )
            host_header = request.headers.get("Host", "")
            test_authority = client_ip == "testclient" and host_header.lower() == "testserver"
            if not test_authority and not self._is_local_authority(host_header):
                logger.warning("access_denied: non-loopback Host without ADMIN_PASSWORD: {}", host_header or "-")
                return _JSONResponse(
                    {"detail": "安全策略: 未设置 ADMIN_PASSWORD 时 Host 必须明确指向本机。"},
                    status_code=403,
                )
            # Even loopback + no password: enforce CSRF on modifying requests
            if self._is_modifying(request) and not self._check_csrf(request):
                logger.warning(
                    "csrf_blocked: {} {} Origin={}", request.method, path, request.headers.get("Origin", "-")
                )
                return _JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
            request.state.auth_user = "local-admin"
            request.state.auth_role = "admin"
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not _check_login_rate(client_ip):
            return _JSONResponse({"detail": "登录尝试过于频繁,请稍后再试"}, status_code=429)
        if not auth.startswith("Basic "):
            # 无 Basic Auth 时,修改请求额外检查 CSRF
            if self._is_modifying(request) and not self._check_csrf(request):
                logger.warning(
                    "csrf_blocked: {} {} from Origin={} Referer={}",
                    request.method,
                    path,
                    request.headers.get("Origin", "-"),
                    request.headers.get("Referer", "-"),
                )
                return _JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
            # 浏览器由 React 登录框接管，显式 API 客户端保留 Basic challenge。
            headers = {} if request.headers.get("sec-fetch-site") else {"WWW-Authenticate": "Basic"}
            return _JSONResponse({"detail": "需要认证"}, status_code=401, headers=headers)
        try:
            decoded = _base64.b64decode(auth[6:], validate=True).decode("utf-8")
            username, separator, password = decoded.partition(":")
            if not separator:
                raise ValueError("Basic credentials must contain a colon")
            expected_password = _ADMIN_PASSWORD if username == "admin" else _REVIEWER_PASSWORDS.get(username)
            if (
                not username
                or expected_password is None
                or not _secrets.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
            ):
                ip = request.client.host if request.client else "unknown"
                _record_login_failure(ip)
                if not _check_login_rate(ip):
                    return _JSONResponse(
                        {"detail": "登录尝试过于频繁,请稍后再试"},
                        status_code=429,
                    )
                return _JSONResponse({"detail": "认证失败"}, status_code=403)
            role = "admin" if username == "admin" else "reviewer"
            if role == "reviewer" and not self._reviewer_path_allowed(path):
                return _JSONResponse({"detail": "审核员无权访问该管理接口"}, status_code=403)
            request.state.auth_user = username
            request.state.auth_role = role
        except (ValueError, UnicodeError):
            _record_login_failure(client_ip)
            return _JSONResponse({"detail": "认证格式错误"}, status_code=400)
        # Basic Auth 通过后,修改请求仍须校验 CSRF
        if self._is_modifying(request) and not self._check_csrf(request):
            logger.warning(
                "csrf_blocked_auth: {} {} from Origin={}", request.method, path, request.headers.get("Origin", "-")
            )
            return _JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
        return await call_next(request)

    @staticmethod
    def _reviewer_path_allowed(path: str) -> bool:
        """限制审核员只能进入审核工作台和读取审核所需媒体。"""
        if path == "/api/events" or path.startswith("/review/"):
            return True
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[:2] == ["api", "jobs"]:
            return True
        return len(parts) == 4 and parts[0] == "api" and parts[1] == "clips" and parts[3] in {"video", "cover"}


app.add_middleware(_AuthMiddleware)
# ── 简易速率限制中间件(V0.1.8.2) ──────────────────────────────────────────
# 使用内存计数器(非分布式),仅对写操作端点做基本保护。
import time as _time  # noqa: E402

_RATE_LIMIT = 30  # 每窗口最多 30 次请求
_RATE_WINDOW = 60  # 窗口 60 秒
_rate_buckets: dict[str, tuple[float, int]] = {}  # key → (window_start, count)
_MAX_BUCKETS = 1000  # 有界桶上限, 超出则 LRU 淘汰最旧 entry


class _RateLimitMiddleware(_BaseMiddleware):
    """速率限制中间件 (V0.1.14.11: 有界 TTL/LRU)。

    仅对写操作 (非 GET/HEAD/OPTIONS) 生效。
    按客户端 IP 分桶, 60s 窗口内最多 30 次。
    桶数超过 _MAX_BUCKETS 时淘汰最旧的, 不粗暴清空全部。
    可信代理配置: 检查 X-Forwarded-For X-Real-IP。
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        # 代理头由 Uvicorn 按可信 peer 处理，限流不再次信任原始头。
        client_ip = request.client.host if request.client else "unknown"

        now = _time.time()
        entry = _rate_buckets.get(client_ip)
        if entry is None or now - entry[0] > _RATE_WINDOW:
            _rate_buckets[client_ip] = (now, 1)
        else:
            count = entry[1] + 1
            if count > _RATE_LIMIT:
                return _JSONResponse(
                    {"detail": "请求过于频繁,请稍后重试。"},
                    status_code=429,
                )
            _rate_buckets[client_ip] = (entry[0], count)

        # V0.1.14.11: 有界LRU淘汰, 不粗暴清空全部
        if len(_rate_buckets) > _MAX_BUCKETS:
            oldest_key = min(_rate_buckets.keys(), key=lambda k: _rate_buckets[k][0])
            del _rate_buckets[oldest_key]

        return await call_next(request)


app.add_middleware(_RateLimitMiddleware)

from app.web.request_limits import RequestBodyLimitMiddleware  # noqa: E402

app.add_middleware(RequestBodyLimitMiddleware)

app.include_router(api_router)
app.include_router(review_router)
app.include_router(collection_router)
app.include_router(monitor_router)
app.include_router(subtitle_template_router)
app.include_router(intro_template_router)
