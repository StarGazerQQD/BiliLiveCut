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

from loguru import logger
from sqlmodel import select

from app.analysis.room_config import load_room_config
from app.core.config import settings
from app.core.cookie import get_bilibili_cookie
from app.db.models import LiveRoom
from app.db.session import get_session
from app.sources.bilibili.client import BilibiliLiveClient


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
        self._reconnect_totals: dict[int, int] = {}
        self._pending_stops: dict[int, asyncio.Task[None]] = {}

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
        pending = list(self._pending_stops.values())
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

    def get_reconnect_total(self, db_id: int) -> int:
        """获取某房间累计重连次数。"""
        return self._reconnect_totals.get(db_id, 0)

    async def _run(self) -> None:
        """主监控循环。"""
        while not self._stop.is_set():
            try:
                await self._check_all()
            except Exception as exc:  # noqa: BLE001
                logger.error("直播监控循环异常: {}", exc)
            await self._sleep_or_stop(settings.live_poll_interval_s)

    async def _check_all(self) -> None:
        """对所有启用了 auto_record 的房间检查开播状态。"""
        with get_session() as db:
            rooms = db.exec(
                select(LiveRoom).where(
                    LiveRoom.auto_record == True,  # noqa: E712
                    LiveRoom.room_id.is_not(None),
                )
            ).all()
            # V0.1.8.2: 在 session 内提取标量属性,避免 session 关闭后懒加载触发 DetachedInstanceError。
            room_info: list[dict[str, object]] = [
                {
                    "db_id": r.id,
                    "room_id": r.room_id,
                    "auto_analyze": r.auto_analyze,
                    "auto_render": r.auto_render,
                    "needs_identity": not r.uploader_name or not r.title,
                    "recording_paused": load_room_config(r).get("recording_paused", False),
                }
                for r in rooms
            ]

        from app.web.service import recorder_manager

        async with BilibiliLiveClient(cookie=get_bilibili_cookie()) as client:
            for room_state in room_info:
                if self._stop.is_set():
                    return
                db_id: int = room_state["db_id"]
                room_id: int = room_state["room_id"]
                auto_analyze: bool = room_state["auto_analyze"]
                auto_render: bool = room_state["auto_render"]
                if room_state["recording_paused"]:
                    continue
                self._last_check_at[db_id] = asyncio.get_event_loop().time()

                if db_id in self._starting:
                    continue  # 正在启动中,跳过

                try:
                    latest = await client.get_room_info(str(room_id), include_detail=bool(room_state["needs_identity"]))
                except Exception as exc:
                    logger.warning("房间 {} 状态查询失败: {}", room_id, exc)
                    continue

                if latest.title or latest.uploader_name:
                    with get_session() as db:
                        room = db.get(LiveRoom, db_id)
                        if room is not None:
                            changed = False
                            if latest.title and room.title != latest.title:
                                room.title = latest.title
                                changed = True
                            if latest.uploader_name and room.uploader_name != latest.uploader_name:
                                room.uploader_name = latest.uploader_name
                                changed = True
                            if changed:
                                db.add(room)

                is_live = latest.live_status == 1
                is_recording = recorder_manager.is_running(db_id)

                if is_live and not is_recording:
                    # 开播,启动录制。
                    self._cancel_pending_stop(db_id)
                    self._offline_counts[db_id] = 0
                    await self._start_recording(db_id, auto_analyze, auto_render)
                elif is_live and is_recording:
                    # 持续直播,重置离线计数。
                    self._cancel_pending_stop(db_id)
                    self._offline_counts[db_id] = 0
                    # 检查最大录制时长。
                    if db_id in self._started_at:
                        elapsed = asyncio.get_event_loop().time() - self._started_at[db_id]
                        if elapsed > settings.recording_max_duration_s:
                            logger.warning(
                                "房间 {} 录制已达 {} 秒上限,自动停止。",
                                db_id,
                                settings.recording_max_duration_s,
                            )
                            await recorder_manager.stop(db_id)
                            self._started_at.pop(db_id, None)
                elif not is_live and is_recording:
                    # 可能下播,累积离线计数。
                    count = self._offline_counts.get(db_id, 0) + 1
                    self._offline_counts[db_id] = count
                    if count >= settings.live_offline_confirm_count and db_id not in self._pending_stops:
                        logger.info(
                            "房间 {} 连续 {} 次检测到未开播,延迟 {} 秒后停止录制。",
                            room_id,
                            count,
                            settings.live_session_end_delay_s,
                        )
                        self._schedule_delayed_stop(db_id, room_id)
                elif not is_live and not is_recording:
                    # 未开播也未录制,重置状态。
                    self._cancel_pending_stop(db_id)
                    self._offline_counts.pop(db_id, None)

    def _schedule_delayed_stop(self, db_id: int, room_id: int) -> None:
        """登记唯一的延迟停止任务，并在完成时清理句柄。"""
        task = asyncio.create_task(self._delayed_stop(db_id, room_id))
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

    async def _delayed_stop(self, db_id: int, room_id: int) -> None:
        """延迟后再次向直播源确认，仍离线才停止录制。"""
        await self._sleep_or_stop(settings.live_session_end_delay_s)
        if self._stop is None or self._stop.is_set():
            return

        try:
            async with BilibiliLiveClient(cookie=get_bilibili_cookie()) as client:
                latest = await client.get_room_info(str(room_id), include_detail=False)
        except Exception as exc:  # noqa: BLE001 - 网络边界失败时保守地保留录制
            logger.warning("房间 {} 停录前复核失败,本轮保留录制: {}", room_id, exc)
            return
        if latest.live_status == 1:
            self._offline_counts[db_id] = 0
            logger.info("房间 {} 停录前复核已恢复直播,继续当前会话。", room_id)
            return

        from app.web.service import recorder_manager

        if recorder_manager.is_running(db_id):
            await recorder_manager.stop(db_id)
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
            )
            self._started_at[db_id] = asyncio.get_event_loop().time()
            # 从 RecordingSession 获取重连次数。
            from app.db.models import RecordingSession

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
            logger.error("自动启动录制失败 db_id={}: {}", db_id, exc)
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
