"""推送驱动、状态补查竞态及订阅所有权的离线验收。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest

from app.analysis.room_config import load_room_config
from app.db.entities import LiveRoom
from app.db.session import get_session
from app.pipeline.live_monitor import LiveMonitor
from app.plugins.live_source import (
    DanmakuStateSink,
    DanmakuStatus,
    LiveStateSink,
    LiveStatus,
    RoomSnapshot,
    SourceDescriptor,
    SourceRoom,
    StreamPreference,
    StreamSpec,
)
from app.sources.registry import SourceRegistry, source_registry
from app.sources.rooms import register_room
from app.web.services import rooms as rooms_service


class EventSource:
    descriptor = SourceDescriptor(platform="eventtest", name="推送测试", domains=("events.invalid",))

    def __init__(self) -> None:
        self.status = LiveStatus.OFFLINE
        self.info_calls = 0
        self.info_gate: asyncio.Event | None = None
        self.info_entered = asyncio.Event()
        self.connected = asyncio.Event()
        self.queue: asyncio.Queue[RoomSnapshot | DanmakuStatus] = asyncio.Queue()
        self.order: list[str] = []

    async def resolve_room(self, value: str) -> SourceRoom:
        return SourceRoom(platform="eventtest", source_id="100", canonical_url="https://events.invalid/100")

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        self.info_calls += 1
        result = RoomSnapshot(status=self.status)
        self.info_entered.set()
        if self.info_gate is not None:
            await self.info_gate.wait()
        return result

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        return []

    async def aclose(self) -> None:
        self.order.append("source_closed")

    async def collect_live_events(self, room: SourceRoom, emit: LiveStateSink, state: DanmakuStateSink) -> None:
        try:
            await state(DanmakuStatus.AVAILABLE)
            self.connected.set()
            while True:
                value = await self.queue.get()
                try:
                    if isinstance(value, RoomSnapshot):
                        await emit(value)
                    else:
                        await state(value)
                finally:
                    self.queue.task_done()
        finally:
            self.order.append("stream_closed")

    async def send(self, value: LiveStatus | DanmakuStatus) -> None:
        await self.queue.put(RoomSnapshot(status=value) if isinstance(value, LiveStatus) else value)
        await asyncio.wait_for(self.queue.join(), 2)


@dataclass
class Runtime:
    source: EventSource
    room_id: int
    monitor: LiveMonitor
    manager: rooms_service.RecorderManager
    starts: list[tuple[int, bool, bool]] = field(default_factory=list)
    stops: list[int] = field(default_factory=list)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)


@pytest.fixture
async def runtime(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Runtime]:
    monkeypatch.setattr(source_registry, "_entries", SourceRegistry()._entries)
    source = EventSource()
    source_registry.register_many("event-test", [source])
    room = await register_room("100", True, "eventtest")
    with get_session() as db:
        stored = db.get(LiveRoom, room.id)
        stored.auto_record = True
        stored.auto_analyze = True
        stored.auto_render = False
        db.add(stored)
    source.info_calls = 0
    source.info_entered.clear()
    manager = rooms_service.RecorderManager()
    monitor = LiveMonitor()
    value = Runtime(source, room.id, monitor, manager)
    monkeypatch.setattr(rooms_service, "recorder_manager", manager)

    async def start(
        db_id: int,
        *,
        pipeline: bool,
        produce: bool,
        automatic: bool,
        recovery: rooms_service.RecordingRecovery | None = None,
        still_current: Callable[[], bool] | None = None,
    ) -> None:
        assert automatic
        assert recovery is None
        assert still_current is None or still_current()
        value.starts.append((db_id, pipeline, produce))
        manager._tasks[db_id] = asyncio.create_task(asyncio.Event().wait())

    async def stop_current(db_id: int, token: object, *, wait_for_next_live: bool = False) -> bool:
        if manager.recording_token(db_id) is not token:
            return False
        task = manager._tasks.pop(db_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if wait_for_next_live:
            manager._set_recording_flags(db_id, wait_for_next_live=True)
        value.stops.append(db_id)
        value.stopped.set()
        return True

    monkeypatch.setattr(manager, "start", start)
    monkeypatch.setattr(manager, "stop_if_current", stop_current)
    await monitor._check_all()
    await asyncio.wait_for(source.connected.wait(), 2)
    await asyncio.wait_for(monitor._subscriptions[room.id].snapshot_task, 2)
    try:
        yield value
    finally:
        await monitor.stop()
        for task in manager._tasks.values():
            task.cancel()
        await asyncio.gather(*manager._tasks.values(), return_exceptions=True)
        await source_registry.unregister_owner("event-test")


async def test_push_starts_immediately_without_network_polling_or_duplicate_sessions(runtime: Runtime) -> None:
    for _ in range(3):
        await runtime.monitor._check_all()
    assert runtime.source.info_calls == 1 and runtime.starts == []
    await runtime.source.send(LiveStatus.LIVE)
    await runtime.source.send(LiveStatus.LIVE)
    assert runtime.starts == [(runtime.room_id, True, False)]
    assert runtime.manager.is_running(runtime.room_id)
    assert runtime.source.info_calls == 1


@pytest.mark.parametrize("block", ["authorization", "auto_record", "paused", "suppressed"])
async def test_queued_events_recheck_current_recording_permissions(runtime: Runtime, block: str) -> None:
    with get_session() as db:
        room = db.get(LiveRoom, runtime.room_id)
        if block == "authorization":
            room.authorized = False
        elif block == "auto_record":
            room.auto_record = False
        db.add(room)
    if block in {"paused", "suppressed"}:
        runtime.manager._set_recording_flags(
            runtime.room_id, **({"paused": True} if block == "paused" else {"suppress_auto_restart": True})
        )
    await runtime.source.send(LiveStatus.LIVE)
    assert not runtime.starts


async def test_one_offline_event_schedules_confirmed_stop(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.pipeline.live_monitor.settings.live_session_end_delay_s", 0)
    await runtime.source.send(LiveStatus.LIVE)
    runtime.source.status = LiveStatus.OFFLINE
    await runtime.source.send(LiveStatus.OFFLINE)
    await asyncio.wait_for(runtime.stopped.wait(), 2)
    assert runtime.stops == [runtime.room_id]
    assert runtime.source.info_calls == 2  # 连通补查 + 下播延迟后的单次复核。


async def test_live_recovery_cancels_delayed_stop_without_another_snapshot(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = asyncio.Event()

    async def delay(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime.monitor, "_sleep_or_stop", delay)
    await runtime.source.send(LiveStatus.LIVE)
    await runtime.source.send(LiveStatus.OFFLINE)
    await asyncio.wait_for(waiting.wait(), 2)
    task = runtime.monitor._pending_stops[runtime.room_id]
    await runtime.source.send(LiveStatus.LIVE)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and runtime.manager.is_running(runtime.room_id)
    assert not runtime.stops and runtime.source.info_calls == 1


async def test_disconnect_and_unknown_status_do_not_stop_recording(runtime: Runtime) -> None:
    await runtime.source.send(LiveStatus.LIVE)
    await runtime.source.send(DanmakuStatus.FAILED)
    await runtime.source.send(LiveStatus.UNKNOWN)
    assert runtime.manager.is_running(runtime.room_id)
    assert not runtime.stops and not runtime.monitor._pending_stops
    assert runtime.source.info_calls == 1


async def test_late_reconnect_snapshot_cannot_overwrite_a_new_live_event(runtime: Runtime) -> None:
    runtime.source.info_gate = asyncio.Event()
    runtime.source.info_entered.clear()
    await runtime.source.send(DanmakuStatus.CONNECTING)
    await runtime.source.send(DanmakuStatus.AVAILABLE)
    await asyncio.wait_for(runtime.source.info_entered.wait(), 2)
    snapshot = runtime.monitor._subscriptions[runtime.room_id].snapshot_task
    await runtime.source.send(LiveStatus.LIVE)
    runtime.source.info_gate.set()
    await asyncio.wait_for(snapshot, 2)
    assert runtime.manager.is_running(runtime.room_id)
    assert not runtime.monitor._pending_stops and not runtime.stops
    assert runtime.source.info_calls == 2


async def test_retry_budget_hold_requires_real_offline_then_new_live_event(runtime: Runtime) -> None:
    runtime.manager._set_recording_flags(runtime.room_id, wait_for_next_live=True)
    await runtime.source.send(LiveStatus.LIVE)
    assert not runtime.starts
    await runtime.source.send(LiveStatus.OFFLINE)
    with get_session() as db:
        assert not load_room_config(db.get(LiveRoom, runtime.room_id))["recording_wait_for_next_live"]
    await runtime.source.send(LiveStatus.LIVE)
    assert len(runtime.starts) == 1


async def test_disabling_room_drains_subscription_and_releases_source_lease(runtime: Runtime) -> None:
    with get_session() as db:
        room = db.get(LiveRoom, runtime.room_id)
        room.auto_record = False
        db.add(room)
    await runtime.monitor._check_all()
    assert not runtime.monitor._subscriptions
    assert runtime.source.order == ["stream_closed"]
    assert not source_registry._entries["eventtest"].users


async def test_plugin_disable_closes_listener_before_source(runtime: Runtime) -> None:
    await source_registry.unregister_owner("event-test")
    assert runtime.source.order == ["stream_closed", "source_closed"]
    assert not runtime.monitor._subscriptions


async def test_local_duration_guard_does_not_need_platform_events(runtime: Runtime) -> None:
    await runtime.source.send(LiveStatus.LIVE)
    runtime.monitor._started_at[runtime.room_id] = asyncio.get_running_loop().time() - 700000
    await runtime.monitor._check_all()
    assert runtime.stops == [runtime.room_id]
    assert runtime.source.info_calls == 1
    assert runtime.room_id not in runtime.monitor._duration_tokens
    with get_session() as db:
        assert load_room_config(db.get(LiveRoom, runtime.room_id))["recording_wait_for_next_live"]


async def test_recording_end_reapplies_last_live_notification_once(runtime: Runtime) -> None:
    await runtime.source.send(LiveStatus.LIVE)
    task = runtime.manager._tasks.pop(runtime.room_id)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await runtime.monitor._check_all()
    await asyncio.wait_for(runtime.monitor._subscriptions[runtime.room_id].reconcile_task, 2)
    await runtime.monitor._check_all()
    assert len(runtime.starts) == 2 and runtime.source.info_calls == 1


async def test_duration_guard_does_not_stop_a_replacement_recording(runtime: Runtime) -> None:
    await runtime.source.send(LiveStatus.LIVE)
    old = runtime.manager._tasks[runtime.room_id]
    old.cancel()
    await asyncio.gather(old, return_exceptions=True)
    runtime.manager._tasks[runtime.room_id] = asyncio.create_task(asyncio.Event().wait())
    runtime.monitor._started_at[runtime.room_id] = asyncio.get_running_loop().time() - 700000
    await runtime.monitor._check_all()
    assert not runtime.stops and runtime.manager.is_running(runtime.room_id)
