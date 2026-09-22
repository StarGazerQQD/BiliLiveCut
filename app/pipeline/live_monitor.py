"""P3 开播自动录制监控器。

后台 asyncio 任务,定期检测配置房间的开播状态:
- 检测到开播时自动创建 Session 并启动录制;
- 检测到下播后延迟一段时间结束 Session;
- 短暂断流不立即拆分为两场直播;
- 防止同一房间重复启动录制;
- 最大录制时长保护。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from loguru import logger
from sqlmodel import select

from app.analysis.room_config import load_room_config
from app.core.config import settings
from app.db.entities import LiveRoom
from app.db.session import get_session
from app.plugins.live_source import LiveStatus, SourceError, SourceRoom
from app.sources.registry import source_registry
from app.sources.rooms import room_source


class LiveMonitor:
    """直播状态监控器。

    在 FastAPI lifespan 中启动/停止。
    """

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        # 记录每个房间的连续离线计数和开播时间。
        self._offline_counts: dict[int, int] = {}
        self._started_at: dict[int, float] = {}
        # 防止重复启动。
        self._starting: set[int] = set()
        self._last_check_at: dict[int, float] = {}
        self._errors: dict[int, str] = {}
        self._reconnect_totals: dict[int, int] = {}
        self._pending_stops: dict[int, asyncio.Task[None]] = {}
        self._platform_checks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        """启动后台监控循环。"""
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        logger.info("直播状态监控已启动。")

    async def stop(self) -> None:
        """停止监控循环。"""
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        pending = [*self._pending_stops.values(), *self._platform_checks.values()]
        self._platform_checks.clear()
        self._pending_stops.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("直播状态监控已停止。")

    def status(self) -> dict:
        """返回监控器状态,供运维面板消费。"""
        return {
            "running": self._task is not None and not self._task.done(),
            "watching_rooms": len(self._offline_counts),
            "offline_counts": dict(self._offline_counts),
            "pending_stops": sorted(self._pending_stops),
            "last_check": {str(k): v for k, v in self._last_check_at.items()},
        }

    def room_status(self, room: LiveRoom, *, running: bool, runtime_state: str) -> dict[str, object]:
        """房间级守候状态与最近成功检测时间，失败不伪装成正常待机。"""
        config = load_room_config(room)
        if running:
            state = "starting" if runtime_state == "starting" else "recording"
        elif config.get("recording_paused") or config.get("recording_auto_restart_suppressed"):
            state = "manual_paused"
        elif not room.auto_record:
            state = "disabled"
        elif config.get("recording_wait_for_next_live"):
            state = "waiting_next_live"
        elif room.id in self._errors:
            state = "error"
        elif room.id in self._starting:
            state = "starting"
        else:
            state = "waiting_live"
        checked = self._last_check_at.get(room.id)
        return {
            "state": state,
            "last_checked_at": datetime.fromtimestamp(checked, UTC).isoformat() if checked else None,
            "error": self._errors.get(room.id),
            "service_running": self._task is not None and not self._task.done(),
        }

    def get_reconnect_total(self, db_id: int) -> int:
        """获取某房间累计重连次数。"""
        return self._reconnect_totals.get(db_id, 0)

    async def _run(self) -> None:
        """主监控循环。"""
        while not self._stop.is_set():
            try:
                await self._check_all(wait=False)
            except Exception as exc:  # noqa: BLE001
                logger.error("直播监控循环异常: {}", exc)
            await self._sleep_or_stop(settings.live_poll_interval_s)

    async def _check_all(self, *, wait: bool = True) -> None:
        """对所有启用了 auto_record 的房间检查开播状态。"""
        with get_session() as db:
            rooms = list(db.exec(select(LiveRoom).where(LiveRoom.auto_record == True)).all())  # noqa: E712
        # 每个平台独立排队；一个来源的请求预算不会阻塞另一来源开始检测。
        groups: dict[str, list[LiveRoom]] = {}
        for room in rooms:
            if room.platform != "local":
                groups.setdefault(room.platform, []).append(room)

        async def check_platform(items: list[LiveRoom]) -> None:
            for room in items:
                if self._stop is not None and self._stop.is_set():
                    return
                await self._check_room(room)

        for platform, items in groups.items():
            current = self._platform_checks.get(platform)
            if current is not None and not current.done():
                continue
            task = asyncio.create_task(check_platform(items))
            self._platform_checks[platform] = task

            def finished(done: asyncio.Task[None]) -> None:
                if not done.cancelled() and done.exception() is not None:
                    logger.error("来源轮询异常 error={}", type(done.exception()).__name__)

            task.add_done_callback(finished)
        if wait:
            await asyncio.gather(*self._platform_checks.values())

    async def _check_room(self, room: LiveRoom) -> None:
        """只把明确下播作为停止证据；未知和查询失败均保留当前录制。"""
        from app.web.service import recorder_manager

        db_id = room.id
        if db_id is None:
            return
        config = load_room_config(room)
        if config.get("recording_paused") or config.get("recording_auto_restart_suppressed") or db_id in self._starting:
            return
        if settings.require_authorization and not room.authorized:
            self._errors[db_id] = "直播间尚未确认授权"
            return
        try:
            identity = room_source(room)
            latest = await source_registry.get_room_info(identity)
        except SourceError as exc:
            self._errors[db_id] = f"开播检测失败：{exc.code}"
            self._cancel_pending_stop(db_id)
            self._offline_counts.pop(db_id, None)
            logger.warning("开播检测失败 db_id={} code={}", db_id, exc.code)
            return
        if latest.status == LiveStatus.UNKNOWN:
            self._errors[db_id] = "直播源返回未知状态，保留当前录制"
            self._cancel_pending_stop(db_id)
            self._offline_counts.pop(db_id, None)
            return
        self._last_check_at[db_id] = datetime.now(UTC).timestamp()
        self._errors.pop(db_id, None)
        is_live = latest.status == LiveStatus.LIVE
        is_recording = recorder_manager.is_running(db_id)
        if config.get("recording_wait_for_next_live"):
            if is_live:
                return
            recorder_manager.release_retry_hold(db_id)
            logger.info("房间 {} 已确认离线,下次开播将恢复自动录制。", db_id)
        if is_live:
            self._cancel_pending_stop(db_id)
            self._offline_counts[db_id] = 0
            if not is_recording:
                await self._start_recording(db_id, room.auto_analyze, room.auto_render)
            elif db_id in self._started_at:
                elapsed = asyncio.get_running_loop().time() - self._started_at[db_id]
                if elapsed > settings.recording_max_duration_s:
                    await recorder_manager.stop(db_id)
                    recorder_manager._set_recording_flags(db_id, wait_for_next_live=True)
                    self._started_at.pop(db_id, None)
        elif is_recording:
            count = self._offline_counts.get(db_id, 0) + 1
            self._offline_counts[db_id] = count
            if count >= settings.live_offline_confirm_count and db_id not in self._pending_stops:
                self._schedule_delayed_stop(db_id, identity)
        else:
            self._cancel_pending_stop(db_id)
            self._offline_counts.pop(db_id, None)

    def _schedule_delayed_stop(self, db_id: int, identity: SourceRoom) -> None:
        """登记唯一的延迟停止任务，并在完成时清理句柄。"""
        from app.web.service import recorder_manager

        token = recorder_manager.recording_token(db_id)
        if token is None:
            return
        task = asyncio.create_task(self._delayed_stop(db_id, identity, token))
        self._pending_stops[db_id] = task

        def _cleanup(done: asyncio.Task[None]) -> None:
            if self._pending_stops.get(db_id) is done:
                self._pending_stops.pop(db_id, None)

        task.add_done_callback(_cleanup)

    def _cancel_pending_stop(self, db_id: int) -> None:
        """直播恢复时撤销尚未执行的停录任务。"""
        task = self._pending_stops.pop(db_id, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info("房间 {} 在延迟收尾期间恢复直播,已撤销停止。", db_id)

    async def _delayed_stop(self, db_id: int, identity: SourceRoom, token: object) -> None:
        """延迟后再次向直播源确认，仍离线才停止录制。"""
        await self._sleep_or_stop(settings.live_session_end_delay_s)
        if self._stop is None or self._stop.is_set():
            return

        try:
            latest = await source_registry.get_room_info(identity)
        except SourceError as exc:
            logger.warning("房间 {} 停录前复核失败,本轮保留录制: {}", db_id, exc.code)
            return
        if latest.status != LiveStatus.OFFLINE:
            self._offline_counts[db_id] = 0
            logger.info("房间 {} 停录前未确认下播,继续当前会话。", db_id)
            return

        from app.web.service import recorder_manager

        if await recorder_manager.stop_if_current(db_id, token):
            self._offline_counts.pop(db_id, None)
            self._started_at.pop(db_id, None)

    async def _start_recording(self, db_id: int, auto_analyze: bool, auto_render: bool) -> None:
        """启动录制。

        :param db_id: 直播间数据库 ID。
        :param auto_analyze: 是否启用自动分析。
        :param auto_render: 是否启用自动渲染。
        """
        from app.web.service import recorder_manager

        self._starting.add(db_id)
        try:
            logger.info("检测到开播,自动启动录制 db_id={}", db_id)
            await recorder_manager.start(
                db_id,
                pipeline=auto_analyze,
                produce=auto_render,
                automatic=True,
            )
            self._started_at[db_id] = asyncio.get_event_loop().time()
            # 从 RecordingSession 获取重连次数。
            from app.db.entities import RecordingSession

            with get_session() as db:
                session = db.exec(
                    select(RecordingSession)
                    .where(
                        RecordingSession.room_id == db_id,
                        RecordingSession.status == "recording",
                    )
                    .order_by(RecordingSession.started_at.desc())
                    .limit(1)
                ).first()
                if session:
                    self._reconnect_totals[db_id] = session.reconnect_count
        except Exception as exc:
            self._errors[db_id] = f"自动启动失败：{type(exc).__name__}"
            logger.error("自动启动录制失败 db_id={}: {}", db_id, type(exc).__name__)
        finally:
            self._starting.discard(db_id)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """休眠或提前中断。"""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass


# 模块级单例。
live_monitor = LiveMonitor()
