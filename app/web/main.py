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

    from app.trends.scheduler import trend_scheduler

    trend_scheduler.start(recording_active=lambda: bool(service.recorder_manager.running_ids()))

    # V0.1.13: 启动后台指标采集器
    from app.core.metrics import start_metrics_collector

    start_metrics_collector(interval_s=60)

    # V0.1.6:启动持久化任务队列 Worker。
    from app.pipeline.task_worker import task_worker

    await task_worker.start()

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
import secrets as _secrets  # noqa: E402

from starlette.middleware.base import BaseHTTPMiddleware as _BaseMiddleware  # noqa: E402
from starlette.responses import JSONResponse as _JSONResponse  # noqa: E402

from app.core.config import settings as _cfg  # noqa: E402

_ADMIN_PASSWORD = _cfg.admin_password

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
        """跨站请求伪造检查。

        浏览器修改请求必须带有与 Host 匹配的 Origin/Referer。
        无 Origin 的非浏览器客户端通过（依赖 Basic Auth）。
        否则比较 Origin/Referer 的主机部分是否等于请求 Host。
        """
        origin = request.headers.get("Origin", "")
        referer = request.headers.get("Referer", "")
        host = request.headers.get("Host", "") or request.url.hostname or ""

        # 无 Origin/Referer → 非浏览器客户端,依赖 Basic Auth
        if not origin and not referer:
            return True

        # Extract hostname from request Host (strip port)
        request_host = host.split(":")[0] if host else ""

        # 提取源的主机名部分
        origin_hosts: list[str] = []
        for header_val in (origin, referer):
            if header_val and "://" in header_val:
                origin_hosts.append(header_val.split("://", 1)[1].split("/", 1)[0].split(":")[0])

        # 任一 Origin/Referer 的主机名匹配请求 Host → 同源
        for origin_host in origin_hosts:
            if origin_host == request_host:
                return True

        return False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
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
            if not username or username != "admin":
                return _JSONResponse({"detail": "认证失败"}, status_code=403)
            if not _secrets.compare_digest(password, _ADMIN_PASSWORD):
                ip = request.client.host if request.client else "unknown"
                _record_login_failure(ip)
                if not _check_login_rate(ip):
                    return _JSONResponse(
                        {"detail": "登录尝试过于频繁,请稍后再试"},
                        status_code=429,
                    )
                return _JSONResponse({"detail": "认证失败"}, status_code=403)
        except Exception:
            return _JSONResponse({"detail": "认证格式错误"}, status_code=400)
        # Basic Auth 通过后,修改请求仍须校验 CSRF
        if self._is_modifying(request) and not self._check_csrf(request):
            logger.warning(
                "csrf_blocked_auth: {} {} from Origin={}", request.method, path, request.headers.get("Origin", "-")
            )
            return _JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
        return await call_next(request)


app.add_middleware(_AuthMiddleware)
# ── 简易速率限制中间件(V0.1.8.2) ──────────────────────────────────────────
# 使用内存计数器(非分布式),仅对写操作端点做基本保护。
import time as _time  # noqa: E402

_RATE_LIMIT = 30  # 每窗口最多 30 次请求
_RATE_WINDOW = 60  # 窗口 60 秒
_rate_buckets: dict[str, tuple[float, int]] = {}  # key → (window_start, count)


class _RateLimitMiddleware(_BaseMiddleware):
    """简易速率限制中间件(写操作端点)。"""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        key = request.client.host if request.client else "unknown"
        now = _time.time()
        entry = _rate_buckets.get(key)
        if entry is None or now - entry[0] > _RATE_WINDOW:
            _rate_buckets[key] = (now, 1)
        else:
            count = entry[1] + 1
            if count > _RATE_LIMIT:
                return _JSONResponse(
                    {"detail": "请求过于频繁,请稍后重试。"},
                    status_code=429,
                )
            _rate_buckets[key] = (entry[0], count)
        # 定期清理过期桶(每 100 次触发一次)。
        if sum(1 for _ in _rate_buckets) > 500:
            _rate_buckets.clear()
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
