"""FastAPI 应用入口。

* 启动时初始化日志与数据库;关闭时停止所有录制任务;
* 挂载 REST API 路由、静态资源与 Jinja2 模板;
* 提供单页仪表盘。

启动方式::

    python -m app.cli serve            # 推荐
    uvicorn app.web.main:app --reload  # 开发热重载
"""

from __future__ import annotations

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
from app.db.session import init_db
from app.web import service
from app.web.routers.api import router as api_router

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期:启动初始化、关闭时优雅停止录制。"""
    setup_logging()
    init_db()
    from app.trends.scheduler import trend_scheduler

    trend_scheduler.start(
        recording_active=lambda: bool(service.recorder_manager.running_ids())
    )
    logger.info("Web 后台已启动。")
    try:
        yield
    finally:
        await trend_scheduler.stop()
        await service.recorder_manager.stop_all()
        logger.info("Web 后台已关闭,所有录制已停止。")


app = FastAPI(
    title="BiliLiveCut 控制台",
    version=f"{__version_label__} ({__version__})",
    lifespan=lifespan,
)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """渲染单页仪表盘。"""
    return _TEMPLATES.TemplateResponse(request, "dashboard.html")
