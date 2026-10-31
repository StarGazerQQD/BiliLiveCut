# ruff: noqa: D100
from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import Protocol

from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.execution import run_sync
from app.core.sanitize import safe_exception_summary
from app.db.entities import LiveRoom
from app.db.session import get_session
from app.recording.schedules import complete_schedule_occurrence, get_due_schedules


class ScheduleRecorder(Protocol):
    """预约仅需要录制状态和启动操作，无需依赖 Web 服务。"""

    def is_running(self, room_id: int) -> bool:
        """查询当前房间是否录制中。"""
        ...

    async def start(self, room_id: int, *, pipeline: bool, produce: bool, schedule_id: int | None = None) -> bool:
        """按房间策略启动录制。"""
        ...


def _room_policy(room_id: int) -> tuple[bool, bool]:
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        if room is None:
            raise ValueError("预约直播间已不存在")
        return room.auto_analyze, room.auto_render


class ScheduleRunner:
    """执行预约与记录结果，应用监管器负责其生命周期。"""

    def __init__(self, recorder: ScheduleRecorder, notify: Callable[[str], None]) -> None:
        self._recorder = recorder
        self._notify = notify
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动唯一预约循环。"""
        if self._task is not None:
            raise RuntimeError("预约循环尚未关闭，不能重复启动")
        self._task = asyncio.create_task(self._loop(), name="recording-schedules")

    async def stop(self) -> None:
        """取消预约循环并等待正在执行的有限数据库操作收尾。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(settings.schedule_check_interval_s)
            try:
                await self.run_due()
            except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
                logger.opt(exception=exc).error("预约调度异常: {}", safe_exception_summary(exc))

    async def run_due(self) -> None:
        """每个到期预约仅产生一个后继，失败和已录制也完成本次调度。"""
        for item in await run_sync(get_due_schedules):
            error: str | None = None
            started = False
            try:
                if not self._recorder.is_running(item["room_id"]):
                    pipeline, produce = await run_sync(partial(_room_policy, item["room_id"]))
                    started = await self._recorder.start(
                        item["room_id"],
                        pipeline=pipeline,
                        produce=produce,
                        schedule_id=item["id"],
                    )
                    if not started:
                        continue
            except (ValueError, RuntimeError) as exc:
                error = safe_exception_summary(exc)
                logger.warning("预约触发失败 room={}: {}", item["room_id"], error)
            await run_sync(partial(complete_schedule_occurrence, item["id"], error=error))
            if started:
                self._notify(f"预约触发：房间 #{item['room_id']} 已开始录制。")
