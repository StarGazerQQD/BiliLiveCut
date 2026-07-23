"""FastAPI 应用入口。

* 启动时初始化日志与数据库;关闭时停止所有录制任务;
* 挂载 REST API 路由、静态资源与 Jinja2 模板;
* 提供单页仪表盘。

启动方式::

    python -m app.cli serve            # 推荐
    uvicorn app.web.main:app --reload  # 开发热重载
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

from app import __version__, __version_label__
from app.core.logging import setup_logging
from app.db.session import get_session, init_db
from app.web import service
from app.web.routers.api import router as api_router
from app.web.routers.collection_router import collection_router
from app.web.routers.intro_template_router import router as intro_template_router
from app.web.routers.monitor_router import monitor_router
from app.web.routers.review_router import review_router
from app.web.routers.subtitle_template_router import router as subtitle_template_router

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


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
            "检测到 SOCKS 代理配置 (ALL_PROXY={})。"
            "请确认 httpx[socks] 已安装, 否则本地 Dashboard 请求可能失败。"
            "安装: pip install httpx[socks]",
            all_proxy,
        )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期:启动初始化、启动 TaskWorker、自动恢复、预约调度、关闭时优雅停止。"""
    setup_logging()
    init_db()

    # V0.1.13: Web 安全兜底 — 启动时检查 host+password 配置
    from app.core.config import settings as _cfg_startup

    if not _cfg_startup.admin_password:
        import socket as _socket

        hostname = _socket.gethostname()
        non_loopback_ips = []
        for info in _socket.getaddrinfo(hostname, None):
            addr = info[4][0]
            if not addr.startswith("127.") and addr != "::1":
                non_loopback_ips.append(addr)
        if non_loopback_ips:
            logger.warning(
                "安全警告: ADMIN_PASSWORD 为空, 但主机存在非 loopback 接口: {}. Web 将禁止非本机请求访问 /api 路由。",
                non_loopback_ips[:3],
            )
    # V0.1.13: 启动 Metrics 后台采样
    from app.core.metrics import start_metrics_collector

    start_metrics_collector(interval_s=60)

    # V0.1.14.11: 代理环境变量规范化
    # 不篡改用户代理配置 (HTTP_PROXY/HTTPS_PROXY/ALL_PROXY), 但确保 localhost 绕过代理
    _setup_proxy_env()

    from app.trends.scheduler import trend_scheduler

    trend_scheduler.start(recording_active=lambda: bool(service.recorder_manager.running_ids()))

    # V0.1.6:启动持久化任务队列 Worker。
    from app.pipeline.task_worker import task_worker
    from app.web.services.background_jobs import web_job_manager

    await task_worker.start()
    await web_job_manager.start()

    # V0.1.2:自动恢复中断的录制会话。
    try:
        recovered = await service.auto_recover_interrupted_sessions()
        if recovered:
            logger.info("已恢复 {} 个中断的录制会话。", len(recovered))
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动恢复跳过(无活动会话或出错): {}", exc)

    # V0.1.2:启动录制预约调度后台任务。
    schedule_task = asyncio.create_task(_schedule_loop())

    logger.info("Web 后台已启动。")
    try:
        # V0.1.7 P3:启动开播自动录制监控器。
        from app.pipeline.live_monitor import live_monitor

        await live_monitor.start()

        yield
    finally:
        schedule_task.cancel()
        try:
            await schedule_task
        except asyncio.CancelledError:
            pass
        await trend_scheduler.stop()
        await live_monitor.stop()
        await service.recorder_manager.stop_all()
        await web_job_manager.stop()
        await task_worker.stop()
        logger.info("Web 后台已关闭,所有录制已停止。")


async def _schedule_loop() -> None:
    """后台定时检查录制预约(每 ``schedule_check_interval_s`` 秒)。"""
    from app.core.config import settings as s

    while True:
        try:
            await asyncio.sleep(s.schedule_check_interval_s)
            due = service.get_due_schedules()
            for item in due:
                if service.recorder_manager.is_running(item["room_id"]):
                    service.mark_schedule_triggered(item["id"])
                    continue
                try:
                    await service.recorder_manager.start(item["room_id"])
                    service.mark_schedule_triggered(item["id"])
                    logger.info("预约触发:房间 #{} 已启动录制。", item["room_id"])
                    service.push_notification(
                        f"预约触发:房间 #{item['room_id']} 已自动开始录制。",
                        kind="success",
                    )
                except ValueError as exc:
                    logger.warning("预约触发失败(房间 #{}): {}", item["room_id"], exc)
                # 对 recurring 预约,重新安排下次.
                if item["recurrent"] == "daily":
                    _reschedule_daily(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("预约调度异常: {}", exc)


def _reschedule_daily(item: dict) -> None:
    """为每日预约创建下一天的副本(原记录已标记 triggered)。"""
    from datetime import timedelta

    from app.db.models import RecordingSchedule, utcnow

    try:
        # 取原预约的时间(hour+minute),放到下一天。
        from datetime import datetime, timedelta

        old_dt = datetime.fromisoformat(item.get("scheduled_at", "")) if item.get("scheduled_at") else utcnow()
        new_ts = utcnow().replace(hour=old_dt.hour, minute=old_dt.minute) + timedelta(days=1)
        with get_session() as db:
            sched = RecordingSchedule(
                room_id=item["room_id"],
                scheduled_at=new_ts,
                enabled=True,
                recurrent="daily",
            )
            db.add(sched)
    except Exception:
        pass  # 复制失败不阻塞,用户可手动重新创建。


app = FastAPI(
    title="BiliLiveCut 控制台",
    version=f"{__version_label__} ({__version__})",
    lifespan=lifespan,
)


# ── 认证中间件(V0.1.8.2) ──────────────────────────────────────────────────
# 当 ADMIN_PASSWORD 被设置时,所有 /api/* /review/* /collection/* 路由要求 Basic Auth。
# 页面浏览(GET /)和静态资源不受影响。
# 请求头格式: Authorization: Basic <base64(admin:<密码>)>
import base64 as _base64  # noqa: E402
import json as _json  # noqa: E402
import secrets as _secrets  # noqa: E402

from starlette.middleware.base import BaseHTTPMiddleware as _BaseMiddleware  # noqa: E402
from starlette.responses import JSONResponse as _JSONResponse  # noqa: E402

from app.core.config import settings as _cfg  # noqa: E402

_ADMIN_PASSWORD = _cfg.admin_password


def _load_reviewer_passwords(raw: str) -> dict[str, str]:
    """解析审核员账号配置，拒绝空账号、空密码和非字符串值。"""
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
        and password
        and username != "admin"
    }
    if len(accounts) != len(value):
        logger.error("REVIEWER_ACCOUNTS_JSON 含无效账号，相关条目已忽略")
    return accounts


_REVIEWER_PASSWORDS = _load_reviewer_passwords(_cfg.reviewer_accounts_json)

_AUTH_PROTECTED_PREFIXES = ("/api/", "/review/", "/collection/")
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
        return host in ("127.0.0.1", "::1", "localhost", "testclient") or host.startswith("127.")

    def _is_modifying(self, request: Request) -> bool:
        """检查请求是否为状态修改类请求 (POST/PUT/PATCH/DELETE)。"""
        return request.method in ("POST", "PUT", "PATCH", "DELETE")

    def _check_csrf(self, request: Request) -> bool:
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
        parsed = self._parse_origin(origin)
        if parsed is None:
            return False

        # ── Build expected from request ──
        scheme = request.url.scheme or "http"
        host_headers = request.headers.get("Host", "")
        if host_headers:
            hostname = host_headers.split(":")[0]
            port_hint = host_headers.split(":")[1] if ":" in host_headers else ""
        else:
            hostname = request.url.hostname or ""
            port_hint = str(request.url.port or "")

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

    @staticmethod
    def _parse_origin(origin: str) -> tuple[str, str, str] | None:
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

        # Split scheme
        if "://" not in origin:
            return None
        scheme, rest = origin.split("://", 1)
        scheme = scheme.lower()
        if scheme not in ("http", "https"):
            return None

        # Split host:port
        rest = rest.rstrip("/")
        host_part = rest
        port = ""

        # Handle IPv6 bracket: [::1]:8080
        if rest.startswith("["):
            end_bracket = rest.find("]")
            if end_bracket == -1:
                return None
            host_part = rest[1:end_bracket]
            after = rest[end_bracket + 1 :]
            if after.startswith(":"):
                port = after[1:]
            elif after:
                return None  # garbage after bracket
        else:
            if ":" in rest:
                parts = rest.rsplit(":", 1)
                host_part, port = parts[0], parts[1]

        # Validate hostname is not empty
        if not host_part:
            return None

        return (scheme, host_part, port)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.auth_user = "anonymous"
        request.state.auth_role = "anonymous"
        protected = any(path.startswith(p) for p in _AUTH_PROTECTED_PREFIXES)
        if not protected:
            return await call_next(request)
        if path.startswith(_AUTH_WHITE_LIST):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"  # noqa: SLF001

        # V0.1.13: 无密码时仅 loopback/测试环境 允许
        if not _ADMIN_PASSWORD:
            if not self._is_loopback(client_ip) and client_ip != "unknown":
                logger.warning("access_denied: non-loopback request without ADMIN_PASSWORD from {}", client_ip)
                return _JSONResponse(
                    {"detail": "安全策略: 未设置 ADMIN_PASSWORD 时仅允许本机访问管理接口。"},
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
            return _JSONResponse({"detail": "需要认证"}, status_code=401, headers={"WWW-Authenticate": "Basic"})
        try:
            decoded = _base64.b64decode(auth[6:]).decode("utf-8", errors="ignore")
            username, _, password = decoded.partition(":")
            expected_password = _ADMIN_PASSWORD if username == "admin" else _REVIEWER_PASSWORDS.get(username)
            if not username or expected_password is None or not _secrets.compare_digest(password, expected_password):
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
        except Exception:
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
        if path.startswith("/review/"):
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

        # V0.1.14.11: 检查可信代理头 (仅 loopback 连接视为可信)
        client_ip = request.client.host if request.client else "unknown"
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                client_ip = forwarded.split(",")[0].strip()
            real_ip = request.headers.get("X-Real-IP", "")
            if real_ip:
                client_ip = real_ip.strip()

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

app.include_router(api_router)
app.include_router(review_router)
app.include_router(collection_router)
app.include_router(monitor_router)
app.include_router(subtitle_template_router)
app.include_router(intro_template_router)
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """渲染单页仪表盘。"""
    return _TEMPLATES.TemplateResponse(request, "dashboard.html")
