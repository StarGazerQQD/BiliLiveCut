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
from app.web.routers.review_router import review_router
from app.web.routers.collection_router import collection_router
from app.web.routers.monitor_router import monitor_router
from app.web.routers.subtitle_template_router import router as subtitle_template_router

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期:启动初始化、启动 TaskWorker、自动恢复、预约调度、关闭时优雅停止。"""
    setup_logging()
    init_db()
    from app.trends.scheduler import trend_scheduler

    trend_scheduler.start(
        recording_active=lambda: bool(service.recorder_manager.running_ids())
    )

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
        old_ts = utcnow()
        # 取原时间的小时+分钟,放到明天同时间。
        new_ts = old_ts.replace(hour=old_ts.hour, minute=old_ts.minute) + timedelta(days=1)
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
app.include_router(api_router)
app.include_router(review_router)
app.include_router(collection_router)
app.include_router(monitor_router)
app.include_router(subtitle_template_router)
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """渲染单页仪表盘。"""
    return _TEMPLATES.TemplateResponse(request, "dashboard.html")
