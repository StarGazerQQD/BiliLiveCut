"""开播事件驱动的自动录制；不提供推送能力的来源保留状态查询兼容。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loguru import logger
from sqlmodel import select

from app.analysis.room_config import load_room_config
from app.core.config import settings
from app.core.sanitize import safe_exception_summary
from app.db.entities import LiveRoom
from app.db.session import get_session
from app.plugins.live_source import (
    DanmakuStatus,
    LiveEventSource,
    LiveStatus,
    RoomSnapshot,
    SourceError,
    SourceRoom,
    SourceTemporaryError,
)
from app.sources.registry import source_registry
from app.sources.rooms import registered_rooms, room_is_removed, room_source

if TYPE_CHECKING:
    from app.web.services.rooms import RecordingRecovery


@dataclass
class _Subscription:
    """监控器拥有房间连接、补查任务及状态应用顺序。"""

    identity: SourceRoom
    task: asyncio.Task[None] | None = None
    snapshot_task: asyncio.Task[None] | None = None
    reconcile_task: asyncio.Task[None] | None = None
    last_snapshot: RoomSnapshot | None = None
    recording_token: object | None = None
    retry_start: bool = False
    state: DanmakuStatus = DanmakuStatus.CONNECTING
    revision: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
        self._subscriptions: dict[int, _Subscription] = {}
        self._duration_tokens: dict[int, object] = {}

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
        await asyncio.gather(*(self._stop_subscription(db_id) for db_id in list(self._subscriptions)))
        pending = [*self._pending_stops.values(), *self._platform_checks.values()]
        self._platform_checks.clear()
        self._pending_stops.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._started_at.clear()
        self._duration_tokens.clear()
        logger.info("直播状态监控已停止。")

    def status(self) -> dict:
        """返回监控器状态,供运维面板消费。"""
        return {
            "running": self._task is not None and not self._task.done(),
            "watching_rooms": len(set(self._offline_counts) | set(self._subscriptions)),
            "event_connections": {str(key): entry.state for key, entry in self._subscriptions.items()},
            "offline_counts": dict(self._offline_counts),
            "pending_stops": sorted(self._pending_stops),
            "last_check": {str(k): v for k, v in self._last_check_at.items()},
        }

    def room_status(self, room: LiveRoom, *, running: bool, runtime_state: str) -> dict[str, object]:
        """房间级守候状态与最近成功检测时间，失败不伪装成正常待机。"""
        from app.web.services.rooms import recorder_manager

        config = load_room_config(room)
        recovery = recorder_manager.recovery(room.id) if room.id is not None else None
        if running:
            state = runtime_state
        elif config.get("recording_paused"):
            state = "manual_paused"
        elif config.get("recording_auto_restart_suppressed"):
            state = "action_required" if runtime_state == "error" else runtime_state
        elif not room.auto_record:
            state = "disabled"
        elif config.get("recording_wait_for_next_live"):
            state = "waiting_next_live"
        elif recovery is not None:
            state = "cooldown"
        elif room.id in self._errors:
            state = "error"
        elif room.id in self._starting:
            state = "starting"
        else:
            state = "waiting_live"
        checked = self._last_check_at.get(room.id)
        subscription = self._subscriptions.get(room.id)
        return {
            "state": state,
            "last_checked_at": datetime.fromtimestamp(checked, UTC).isoformat() if checked else None,
            "error": self._errors.get(room.id),
            "message": recovery.reason if recovery is not None else None,
            "retry_in_s": recovery.remaining() if recovery is not None else None,
            "service_running": self._task is not None and not self._task.done(),
            "detection_mode": "events" if source_registry.supports_live_events(room.platform) else "polling",
            "connection_state": subscription.state if subscription else None,
        }

    def get_reconnect_total(self, db_id: int) -> int:
        """获取某房间累计重连次数。"""
        return self._reconnect_totals.get(db_id, 0)

    async def _run(self) -> None:
        """刷新本地配置和时长保护；事件来源不在这里请求平台状态。"""
        while not self._stop.is_set():
            try:
                await self._check_all(wait=False)
            except Exception as exc:  # noqa: BLE001
                logger.error("直播监控循环异常: {}", exc)
            await self._sleep_or_stop(settings.live_poll_interval_s)

    async def _check_all(self, *, wait: bool = True) -> None:
        """同步房间订阅；仅向没有推送能力的兼容来源查询状态。"""
        from app.web.services.rooms import recorder_manager

        if self._stop is None:
            self._stop = asyncio.Event()
        with get_session() as db:
            rooms = [
                room
                for room in registered_rooms(db)
                if room.auto_record or recorder_manager.recovery_requested(room.id)
            ]
        groups: dict[str, list[LiveRoom]] = {}
        watched: set[int] = set()
        for room in rooms:
            if room.platform == "local" or room.id is None:
                continue
            if source_registry.supports_live_events(room.platform):
                config = load_room_config(room)
                if config.get("recording_paused") or config.get("recording_auto_restart_suppressed"):
                    continue
                if settings.require_authorization and not room.authorized:
                    self._errors[room.id] = "直播间尚未确认授权"
                    continue
                try:
                    identity = room_source(room)
                except SourceError as exc:
                    self._errors[room.id] = f"开播事件订阅失败：{exc.code}"
                    continue
                watched.add(room.id)
                current = self._subscriptions.get(room.id)
                if current is not None and current.identity != identity:
                    await self._stop_subscription(room.id)
                    current = None
                if current is None:
                    entry = _Subscription(identity)
                    self._subscriptions[room.id] = entry
                    entry.task = asyncio.create_task(self._watch_room(room.id, entry))
                else:
                    self._reconcile_recording_end(room.id, current)
            else:
                groups.setdefault(room.platform, []).append(room)
        for db_id in set(self._subscriptions) - watched:
            await self._stop_subscription(db_id)
            self._cancel_pending_stop(db_id)
        for platform in set(self._platform_checks) - groups.keys():
            task = self._platform_checks.pop(platform)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._enforce_duration()

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

            def finished(done: asyncio.Task[None], source_platform: str = platform) -> None:
                if not done.cancelled() and done.exception() is not None:
                    logger.opt(exception=done.exception()).error("来源轮询异常 platform={}", source_platform)

            task.add_done_callback(finished)
        if wait:
            await asyncio.gather(*self._platform_checks.values())

    async def _stop_subscription(self, db_id: int) -> None:
        entry = self._subscriptions.pop(db_id, None)
        if entry is None:
            return
        tasks = [task for task in (entry.task, entry.snapshot_task, entry.reconcile_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_room(self, db_id: int, entry: _Subscription) -> None:
        """持有来源租约；停用插件、移除房间和关闭服务均等待连接收尾。"""
        delay = 1.0

        async def stop() -> None:
            await self._stop_subscription(db_id)

        async def emit(snapshot: RoomSnapshot) -> None:
            if not isinstance(snapshot, RoomSnapshot):
                raise ValueError("开下播事件必须是 RoomSnapshot")
            snapshot = RoomSnapshot.model_validate(snapshot.model_dump())
            entry.revision += 1
            async with entry.lock:
                if self._subscriptions.get(db_id) is entry:
                    await self._apply_event(db_id, entry, snapshot)

        async def state(value: DanmakuStatus) -> None:
            if self._subscriptions.get(db_id) is not entry:
                return
            previous = entry.state
            entry.state = DanmakuStatus(value)
            if value == DanmakuStatus.AVAILABLE and previous != value:
                self._errors.pop(db_id, None)
                entry.snapshot_task = asyncio.create_task(self._initial_snapshot(db_id, entry, entry.revision))
                entry.snapshot_task.add_done_callback(lambda task: self._observe_snapshot(db_id, entry, task))
            elif value != DanmakuStatus.AVAILABLE:
                entry.revision += 1
                entry.retry_start = False
                if entry.snapshot_task is not None:
                    entry.snapshot_task.cancel()
                    await asyncio.gather(entry.snapshot_task, return_exceptions=True)
                    entry.snapshot_task = None
                if value == DanmakuStatus.FAILED:
                    self._errors[db_id] = "开播事件连接失败，正在重连；不会将断连视为下播"

        try:
            while self._subscriptions.get(db_id) is entry and not self._stop.is_set():
                try:
                    async with source_registry.use(entry.identity.platform, stop) as source:
                        if not isinstance(source, LiveEventSource):
                            raise SourceTemporaryError("来源不再提供开下播事件")
                        await source.collect_live_events(entry.identity, emit, state)
                    raise SourceTemporaryError("开下播事件连接提前结束")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 事件来源边界；不得因单个房间故障停止其它房间。
                    await state(DanmakuStatus.FAILED)
                    logger.warning("开播事件监听异常 db_id={}: {}", db_id, safe_exception_summary(exc))
                await self._sleep_or_stop(delay)
                delay = min(delay * 2, settings.reconnect_max_backoff_s)
        finally:
            if entry.snapshot_task is not None:
                entry.snapshot_task.cancel()
                await asyncio.gather(entry.snapshot_task, return_exceptions=True)

    async def _initial_snapshot(self, db_id: int, entry: _Subscription, revision: int) -> None:
        """暂时失败或未知时限速重试；较新推送优先，永久错误保留诊断并结束补查。"""
        while self._subscriptions.get(db_id) is entry and entry.revision == revision and not self._stop.is_set():
            delay = settings.live_poll_interval_s
            try:
                snapshot = await source_registry.get_room_info(entry.identity)
            except SourceError as exc:
                if self._subscriptions.get(db_id) is not entry or entry.revision != revision:
                    return
                self._errors[db_id] = f"开播事件连通后的状态补查失败：{exc.code}"
                if not isinstance(exc, SourceTemporaryError):
                    return
                delay = max(delay, exc.retry_after or 0)
            else:
                async with entry.lock:
                    if self._subscriptions.get(db_id) is not entry or entry.revision != revision:
                        return
                    if snapshot.status != LiveStatus.UNKNOWN:
                        await self._apply_event(db_id, entry, snapshot)
                        return
                    self._errors[db_id] = "开播事件连通后的状态未知，将重新补查"
            await self._sleep_or_stop(delay)

    def _observe_snapshot(self, db_id: int, entry: _Subscription, task: asyncio.Task[None]) -> None:
        """业务应用错误也必须可见，不能留下无人读取的后台异常。"""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and self._subscriptions.get(db_id) is entry:
            self._errors[db_id] = "开播状态补查处理失败，请查看服务日志"
            logger.opt(exception=error).error("开播状态补查处理失败 db_id={}", db_id)

    async def _apply_event(self, db_id: int, entry: _Subscription, snapshot: RoomSnapshot) -> None:
        from app.web.services.rooms import recorder_manager

        entry.last_snapshot = snapshot
        with get_session() as db:
            room = db.get(LiveRoom, db_id)
        if room is not None:
            revision = entry.revision
            await self._apply_snapshot(
                room,
                entry.identity,
                snapshot,
                event_driven=True,
                still_current=lambda: self._subscriptions.get(db_id) is entry and entry.revision == revision,
            )
        entry.recording_token = recorder_manager.recording_token(db_id)
        entry.retry_start = (
            snapshot.status == LiveStatus.LIVE and entry.recording_token is None and db_id in self._errors
        )

    def _reconcile_recording_end(self, db_id: int, entry: _Subscription) -> None:
        """录制收尾或启动失败后重新应用有效推送，仍经过授权与人工暂停闸门。"""
        from app.web.services.rooms import recorder_manager

        token = recorder_manager.recording_token(db_id)
        recovery = recorder_manager.recovery(db_id)
        previous = entry.recording_token
        entry.recording_token = token
        if (
            token is not None
            or entry.state != DanmakuStatus.AVAILABLE
            or (previous is None and not entry.retry_start and recovery is None)
            or (recovery is not None and recovery.remaining() > 0)
        ):
            return
        if entry.reconcile_task is not None and not entry.reconcile_task.done():
            return
        revision = entry.revision

        async def apply() -> None:
            async with entry.lock:
                if (
                    self._subscriptions.get(db_id) is entry
                    and entry.revision == revision
                    and entry.last_snapshot is not None
                    and (entry.last_snapshot.status == LiveStatus.LIVE or recovery is not None)
                ):
                    snapshot = entry.last_snapshot
                    if recovery is not None:
                        try:
                            snapshot = await source_registry.get_room_info(entry.identity)
                        except SourceError as exc:
                            if (
                                self._subscriptions.get(db_id) is entry
                                and entry.revision == revision
                                and recorder_manager.recovery(db_id) is recovery
                            ):
                                self._errors[db_id] = f"冷却后直播状态复核失败：{exc.code}"
                                if isinstance(exc, SourceTemporaryError):
                                    recorder_manager.defer_recovery(db_id, recovery)
                                else:
                                    recorder_manager.block_recovery(db_id, recovery, self._errors[db_id])
                            return
                        if (
                            self._subscriptions.get(db_id) is not entry
                            or entry.revision != revision
                            or recorder_manager.recovery(db_id) is not recovery
                        ):
                            return
                    await self._apply_event(db_id, entry, snapshot)
                    if recovery is not None:
                        recorder_manager.defer_recovery(db_id, recovery)

        entry.reconcile_task = asyncio.create_task(apply())
        entry.reconcile_task.add_done_callback(lambda task: self._observe_snapshot(db_id, entry, task))

    async def _enforce_duration(self) -> None:
        """时长保护依据本地计时，不依赖平台消息频率或 HTTP 状态轮询。"""
        from app.web.services.rooms import recorder_manager

        for db_id, started_at in list(self._started_at.items()):
            token = recorder_manager.recording_token(db_id)
            if not recorder_manager.is_running(db_id):
                self._started_at.pop(db_id, None)
                self._duration_tokens.pop(db_id, None)
            elif self._duration_tokens.get(db_id, token) is not token:
                self._started_at.pop(db_id, None)
                self._duration_tokens.pop(db_id, None)
            elif asyncio.get_running_loop().time() - started_at > settings.recording_max_duration_s:
                if token is not None and await recorder_manager.stop_if_current(db_id, token, wait_for_next_live=True):
                    self._started_at.pop(db_id, None)
                    self._duration_tokens.pop(db_id, None)

    async def _check_room(self, room: LiveRoom) -> None:
        """只把明确下播作为停止证据；未知和查询失败均保留当前录制。"""
        from app.web.services.rooms import recorder_manager

        db_id = room.id
        if db_id is None:
            return
        config = load_room_config(room)
        if config.get("recording_paused") or config.get("recording_auto_restart_suppressed") or db_id in self._starting:
            return
        if settings.require_authorization and not room.authorized:
            self._errors[db_id] = "直播间尚未确认授权"
            return
        recovery = recorder_manager.recovery(db_id)
        if recovery is not None and recovery.remaining() > 0:
            return
        try:
            identity = room_source(room)
            latest = await source_registry.get_room_info(identity)
        except SourceError as exc:
            if recovery is not None:
                if recorder_manager.recovery(db_id) is not recovery:
                    return
                if isinstance(exc, SourceTemporaryError):
                    recorder_manager.defer_recovery(db_id, recovery)
                else:
                    recorder_manager.block_recovery(db_id, recovery, f"冷却后直播状态复核失败：{exc.code}")
            self._errors[db_id] = f"开播检测失败：{exc.code}"
            self._cancel_pending_stop(db_id)
            self._offline_counts.pop(db_id, None)
            logger.warning("开播检测失败 db_id={} code={}", db_id, exc.code)
            return
        if recovery is not None and recorder_manager.recovery(db_id) is not recovery:
            return
        await self._apply_snapshot(room, identity, latest)
        if recovery is not None:
            recorder_manager.defer_recovery(db_id, recovery)

    async def _apply_snapshot(
        self,
        room: LiveRoom,
        identity: SourceRoom,
        latest: RoomSnapshot,
        *,
        event_driven: bool = False,
        still_current: Callable[[], bool] | None = None,
    ) -> None:
        """推送与兼容查询共用录制闸门，状态通知本身不绕过授权或人工暂停。"""
        from app.web.services.rooms import recorder_manager

        db_id = room.id
        with get_session() as db:
            room = db.get(LiveRoom, db_id)
            if (
                room is None
                or room_is_removed(db, db_id)
                or (not room.auto_record and not recorder_manager.recovery_requested(db_id))
                or room_source(room, db) != identity
            ):
                return
        config = load_room_config(room)
        if config.get("recording_paused") or config.get("recording_auto_restart_suppressed"):
            return
        if settings.require_authorization and not room.authorized:
            self._errors[db_id] = "直播间尚未确认授权"
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
        recovery = recorder_manager.recovery(db_id)
        if recovery is not None and (not is_live or recovery.source != identity):
            recorder_manager.clear_recovery(db_id, recovery)
            recovery = None
        if config.get("recording_wait_for_next_live"):
            if is_live:
                return
            recorder_manager.release_retry_hold(db_id)
            logger.info("房间 {} 已确认离线,下次开播将恢复自动录制。", db_id)
        if is_live:
            self._cancel_pending_stop(db_id)
            self._offline_counts[db_id] = 0
            if not is_recording and db_id not in self._starting:
                if recovery is None or recovery.remaining() == 0:
                    await self._start_recording(
                        db_id, room.auto_analyze, room.auto_render, recovery=recovery, still_current=still_current
                    )
        elif is_recording:
            count = self._offline_counts.get(db_id, 0) + 1
            self._offline_counts[db_id] = count
            if (event_driven or count >= settings.live_offline_confirm_count) and db_id not in self._pending_stops:
                self._schedule_delayed_stop(db_id, identity)
        else:
            self._cancel_pending_stop(db_id)
            self._offline_counts.pop(db_id, None)

    def _schedule_delayed_stop(self, db_id: int, identity: SourceRoom) -> None:
        """登记唯一的延迟停止任务，并在完成时清理句柄。"""
        from app.web.services.rooms import recorder_manager

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
        """延迟后确认下播；暂时失败或未知时保留录制并限速复核同一实例。"""
        from app.web.services.rooms import recorder_manager

        await self._sleep_or_stop(settings.live_session_end_delay_s)
        while self._stop is not None and not self._stop.is_set():
            delay = settings.live_poll_interval_s
            try:
                latest = await source_registry.get_room_info(identity)
            except SourceError as exc:
                logger.warning("房间 {} 停录前复核失败,保留录制: {}", db_id, exc.code)
                if not isinstance(exc, SourceTemporaryError):
                    self._errors[db_id] = f"停录前状态复核失败：{exc.code}"
                    return
                delay = max(delay, exc.retry_after or 0)
            else:
                if latest.status == LiveStatus.LIVE:
                    self._offline_counts[db_id] = 0
                    logger.info("房间 {} 停录前已确认仍在直播,继续当前会话。", db_id)
                    return
                if latest.status == LiveStatus.OFFLINE:
                    if await recorder_manager.stop_if_current(db_id, token):
                        self._offline_counts.pop(db_id, None)
                        self._started_at.pop(db_id, None)
                        self._duration_tokens.pop(db_id, None)
                    return
            await self._sleep_or_stop(delay)
            if recorder_manager.recording_token(db_id) is not token:
                return

    async def _start_recording(
        self,
        db_id: int,
        auto_analyze: bool,
        auto_render: bool,
        *,
        recovery: RecordingRecovery | None = None,
        still_current: Callable[[], bool] | None = None,
    ) -> None:
        """启动录制。

        :param db_id: 直播间数据库 ID。
        :param auto_analyze: 是否启用自动分析。
        :param auto_render: 是否启用自动渲染。
        """
        from app.web.services.rooms import recorder_manager

        self._starting.add(db_id)
        try:
            logger.info("检测到开播,自动启动录制 db_id={}", db_id)
            await recorder_manager.start(
                db_id,
                pipeline=auto_analyze,
                produce=auto_render,
                automatic=True,
                recovery=recovery,
                still_current=still_current,
            )
            if not recorder_manager.is_running(db_id):
                return
            self._started_at[db_id] = asyncio.get_running_loop().time()
            self._duration_tokens[db_id] = recorder_manager.recording_token(db_id)
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
            self._errors[db_id] = f"自动启动失败：{safe_exception_summary(exc)}"
            logger.opt(exception=exc).error("自动启动录制失败 db_id={}: {}", db_id, safe_exception_summary(exc))
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
