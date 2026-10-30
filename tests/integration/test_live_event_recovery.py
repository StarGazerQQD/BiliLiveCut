from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.analysis.room_config import merge_room_config
from app.core.config import settings
from app.db import session as database
from app.db.entities import LiveRoom, RecordingSession
from app.db.session import get_session
from app.pipeline.live_monitor import LiveMonitor
from app.plugins.live_source import (
    DanmakuStateSink,
    DanmakuStatus,
    LiveStateSink,
    LiveStatus,
    RoomSnapshot,
    SourceAuthenticationError,
    SourceDescriptor,
    SourceError,
    SourceInvalidInput,
    SourceRoom,
    SourceTemporaryError,
    SourceUnavailable,
    StreamPreference,
    StreamSpec,
)
from app.sources.registry import SourceRegistry, source_registry
from app.sources.rooms import register_room
from app.web.services import rooms

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, ExecutionContext


class RecoverySource:
    """控制外部平台响应，监控器、注册表、录制管理器及数据库均使用真实实现。"""

    descriptor = SourceDescriptor(platform="recovery", name="恢复测试", domains=("recovery.invalid",))

    def __init__(self) -> None:
        self.status = LiveStatus.OFFLINE
        self.failures = 0
        self.unknown = 0
        self.calls = 0
        self.error: type[SourceError] = SourceTemporaryError
        self.failed = asyncio.Event()
        self.connected = asyncio.Event()
        self.queue: asyncio.Queue[LiveStatus] = asyncio.Queue()
        self.block_info_call: int | None = None
        self.info_blocked = asyncio.Event()
        self.info_release = asyncio.Event()

    async def resolve_room(self, value: str) -> SourceRoom:
        return SourceRoom(platform="recovery", source_id="1", canonical_url="https://recovery.invalid/1")

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        self.calls += 1
        if self.calls == self.block_info_call:
            self.info_blocked.set()
            await self.info_release.wait()
        if self.failures:
            self.failures -= 1
            self.failed.set()
            raise self.error("测试平台调用失败", retry_after=0.05)
        if self.unknown:
            self.unknown -= 1
            self.failed.set()
            return RoomSnapshot(status=LiveStatus.UNKNOWN)
        return RoomSnapshot(status=self.status)

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        # 平台暂未提供媒体，真实录制器继续等待，仍可验收停止和实例身份。
        return []

    async def collect_live_events(self, room: SourceRoom, emit: LiveStateSink, state: DanmakuStateSink) -> None:
        await state(DanmakuStatus.AVAILABLE)
        self.connected.set()
        while True:
            value = await self.queue.get()
            try:
                await emit(RoomSnapshot(status=value))
            finally:
                self.queue.task_done()

    async def aclose(self) -> None:
        pass

    async def send(self, status: LiveStatus) -> None:
        await self.queue.put(status)
        await asyncio.wait_for(self.queue.join(), 3)


@dataclass
class RecoveryRuntime:
    source: RecoverySource
    monitor: LiveMonitor
    manager: rooms.RecorderManager
    room_id: int


@pytest.fixture
async def recovery(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RecoveryRuntime]:
    monkeypatch.setattr(source_registry, "_entries", SourceRegistry()._entries)
    monkeypatch.setattr(source_registry, "attempts", 1)
    monkeypatch.setattr(settings, "live_poll_interval_s", 0.05)
    monkeypatch.setattr(settings, "live_session_end_delay_s", 0)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 1000)
    monkeypatch.setattr(rooms, "open_path", lambda path: False)
    source = RecoverySource()
    source_registry.register_many("recovery-test", [source])
    room = await register_room("1", True, "recovery")
    assert room.id is not None
    with get_session() as db:
        stored = db.get(LiveRoom, room.id)
        assert stored is not None
        stored.auto_record = True
        stored.auto_analyze = False
        stored.auto_render = False
        db.add(stored)
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    monitor = LiveMonitor()
    try:
        yield RecoveryRuntime(source, monitor, manager, room.id)
    finally:
        await monitor.stop()
        await manager.stop_all()
        await source_registry.unregister_owner("recovery-test")


@pytest.mark.parametrize("failure", ["failures", "unknown"])
async def test_initial_snapshot_recovers_without_reconnect_or_another_event(
    recovery: RecoveryRuntime, failure: str
) -> None:
    setattr(recovery.source, failure, 1)
    await recovery.monitor._check_all()
    await asyncio.wait_for(recovery.source.connected.wait(), 2)
    entry = recovery.monitor._subscriptions[recovery.room_id]
    await asyncio.wait_for(entry.snapshot_task, 2)
    assert entry.last_snapshot is not None and entry.last_snapshot.status == LiveStatus.OFFLINE
    assert recovery.room_id not in recovery.monitor._errors
    calls = recovery.source.calls
    await recovery.monitor._check_all()
    assert recovery.source.calls == calls


async def _exhaust_live_recording(
    recovery: RecoveryRuntime, monkeypatch: pytest.MonkeyPatch
) -> tuple[rooms.RecordingRecovery, list[float]]:
    now = [100.0]
    monkeypatch.setattr(rooms, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 1)
    recovery.source.status = LiveStatus.LIVE
    await recovery.monitor._check_all()
    await asyncio.wait_for(recovery.source.connected.wait(), 2)
    entry = recovery.monitor._subscriptions[recovery.room_id]
    await asyncio.wait_for(entry.snapshot_task, 2)
    async with asyncio.timeout(3):
        while recovery.manager.recovery(recovery.room_id) is None:
            await asyncio.sleep(0.01)
    cooling = recovery.manager.recovery(recovery.room_id)
    assert cooling is not None and cooling.remaining() > 0
    return cooling, now


@pytest.mark.parametrize("failure", ["unknown", "temporary", "authentication"])
async def test_event_cooldown_rechecks_without_a_new_live_event(
    recovery: RecoveryRuntime, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    cooling, now = await _exhaust_live_recording(recovery, monkeypatch)
    entry = recovery.monitor._subscriptions[recovery.room_id]
    calls = recovery.source.calls
    await recovery.monitor._check_all()
    assert recovery.source.calls == calls
    assert not recovery.manager.is_running(recovery.room_id)
    now[0] = cooling.ready_at
    if failure == "unknown":
        recovery.source.unknown = 1
    else:
        recovery.source.failures = 1
        recovery.source.error = SourceTemporaryError if failure == "temporary" else SourceAuthenticationError
    await recovery.monitor._check_all()
    assert entry.reconcile_task is not None
    await asyncio.wait_for(entry.reconcile_task, 2)
    assert not recovery.manager.is_running(recovery.room_id)
    if failure == "authentication":
        assert recovery.manager.recovery(recovery.room_id) is None
        await recovery.monitor._check_all()
        assert recovery.room_id not in recovery.monitor._subscriptions
        return
    assert recovery.manager.recovery(recovery.room_id) is cooling
    assert cooling.remaining() > 0
    now[0] = cooling.ready_at
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 1000)
    await recovery.monitor._check_all()
    await asyncio.wait_for(entry.reconcile_task, 2)
    assert recovery.manager.is_running(recovery.room_id)
    assert recovery.manager.recovery(recovery.room_id) is None


async def test_new_offline_event_during_recovery_metadata_prevents_start(
    recovery: RecoveryRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    cooling, now = await _exhaust_live_recording(recovery, monkeypatch)
    now[0] = cooling.ready_at
    entry = recovery.monitor._subscriptions[recovery.room_id]
    # 第一次是冷却复核，第二次是创建录制器前的资料刷新。
    recovery.source.block_info_call = recovery.source.calls + 2
    await recovery.monitor._check_all()
    await asyncio.wait_for(recovery.source.info_blocked.wait(), 2)
    revision = entry.revision
    offline = asyncio.create_task(recovery.source.send(LiveStatus.OFFLINE))
    try:
        async with asyncio.timeout(2):
            while entry.revision == revision:
                await asyncio.sleep(0)
        recovery.source.status = LiveStatus.OFFLINE
        recovery.source.info_release.set()
        await asyncio.wait_for(entry.reconcile_task, 2)
        await asyncio.wait_for(offline, 2)
        assert not recovery.manager.is_running(recovery.room_id)
        assert recovery.manager.recovery(recovery.room_id) is None
        with get_session() as db:
            sessions = db.exec(select(RecordingSession).where(RecordingSession.room_id == recovery.room_id)).all()
            assert len(sessions) == 1 and sessions[0].status == "error"
    finally:
        recovery.source.info_release.set()
        await asyncio.gather(offline, return_exceptions=True)
    calls = recovery.source.calls
    await recovery.monitor._check_all()
    assert recovery.source.calls == calls


@pytest.mark.parametrize("error", [SourceTemporaryError, SourceAuthenticationError])
async def test_cancelled_cooldown_does_not_publish_late_lookup_error(
    recovery: RecoveryRuntime, monkeypatch: pytest.MonkeyPatch, error: type[SourceError]
) -> None:
    cooling, now = await _exhaust_live_recording(recovery, monkeypatch)
    now[0] = cooling.ready_at
    entry = recovery.monitor._subscriptions[recovery.room_id]
    recovery.source.block_info_call = recovery.source.calls + 1
    recovery.source.failures = 1
    recovery.source.error = error
    await recovery.monitor._check_all()
    await asyncio.wait_for(recovery.source.info_blocked.wait(), 2)
    try:
        await recovery.manager.stop(recovery.room_id, pause_auto_restart=True)
        assert recovery.manager.recovery(recovery.room_id) is None
        recovery.source.info_release.set()
        await asyncio.wait_for(entry.reconcile_task, 2)
        assert recovery.room_id not in recovery.monitor._errors
        assert not recovery.manager.is_running(recovery.room_id)
    finally:
        recovery.source.info_release.set()


@pytest.mark.parametrize("failure", ["failures", "unknown"])
async def test_offline_confirmation_recovers_and_stops_the_real_recording(
    recovery: RecoveryRuntime, failure: str
) -> None:
    await recovery.monitor._check_all()
    await asyncio.wait_for(recovery.source.connected.wait(), 2)
    await asyncio.wait_for(recovery.monitor._subscriptions[recovery.room_id].snapshot_task, 2)
    recovery.source.status = LiveStatus.LIVE
    await recovery.source.send(LiveStatus.LIVE)
    token = recovery.manager.recording_token(recovery.room_id)
    assert isinstance(token, asyncio.Task) and recovery.manager.is_running(recovery.room_id)
    recovery.source.status = LiveStatus.OFFLINE
    setattr(recovery.source, failure, 1)
    await recovery.source.send(LiveStatus.OFFLINE)
    await asyncio.wait_for(recovery.source.failed.wait(), 2)
    await asyncio.wait_for(asyncio.shield(token), 2)
    assert not recovery.manager.is_running(recovery.room_id)


async def test_new_event_cancels_failed_snapshot_retry(recovery: RecoveryRuntime) -> None:
    recovery.source.failures = 100
    await recovery.monitor._check_all()
    await asyncio.wait_for(recovery.source.connected.wait(), 2)
    await asyncio.wait_for(recovery.source.failed.wait(), 2)
    entry = recovery.monitor._subscriptions[recovery.room_id]
    await recovery.source.send(LiveStatus.OFFLINE)
    await asyncio.wait_for(entry.snapshot_task, 2)
    assert entry.last_snapshot is not None and entry.last_snapshot.status == LiveStatus.OFFLINE
    assert recovery.room_id not in recovery.monitor._errors
    assert recovery.source.failures == 99


@pytest.mark.parametrize("error", [SourceAuthenticationError, SourceInvalidInput, SourceUnavailable])
@pytest.mark.parametrize("stage", ["initial", "offline"])
async def test_permanent_source_failure_stays_visible_without_retries(
    recovery: RecoveryRuntime, error: type[SourceError], stage: str
) -> None:
    if stage == "initial":
        recovery.source.error = error
        recovery.source.failures = 100
    await recovery.monitor._check_all()
    await asyncio.wait_for(recovery.source.connected.wait(), 2)
    entry = recovery.monitor._subscriptions[recovery.room_id]
    await asyncio.wait_for(entry.snapshot_task, 2)
    if stage == "offline":
        recovery.source.status = LiveStatus.LIVE
        await recovery.source.send(LiveStatus.LIVE)
        recovery.source.error = error
        recovery.source.failures = 100
        recovery.source.status = LiveStatus.OFFLINE
        await recovery.source.send(LiveStatus.OFFLINE)
        await asyncio.wait_for(recovery.source.failed.wait(), 2)
        task = recovery.monitor._pending_stops.get(recovery.room_id)
        if task is not None:
            await asyncio.wait_for(task, 2)
        assert recovery.manager.is_running(recovery.room_id)
    assert recovery.source.failures == 99
    assert error.code in recovery.monitor._errors[recovery.room_id]


@pytest.mark.parametrize("gate", ["retry", "paused", "suppressed", "disabled", "unauthorized", "offline"])
async def test_transient_start_failure_retries_with_recording_gates(recovery: RecoveryRuntime, gate: str) -> None:
    failed = False

    def fail_start_once(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal failed
        if not failed and statement.startswith("UPDATE live_rooms SET enabled="):
            failed = True
            raise OperationalError(statement, parameters, OSError("测试数据库暂时不可写"))

    await recovery.monitor._check_all()
    await asyncio.wait_for(recovery.source.connected.wait(), 2)
    entry = recovery.monitor._subscriptions[recovery.room_id]
    await asyncio.wait_for(entry.snapshot_task, 2)
    recovery.source.status = LiveStatus.LIVE
    event.listen(database.engine, "before_cursor_execute", fail_start_once)
    try:
        await recovery.source.send(LiveStatus.LIVE)
    finally:
        event.remove(database.engine, "before_cursor_execute", fail_start_once)
    assert failed and not recovery.manager.is_running(recovery.room_id)
    if gate == "offline":
        recovery.source.status = LiveStatus.OFFLINE
        await recovery.source.send(LiveStatus.OFFLINE)
    elif gate != "retry":
        with get_session() as db:
            room = db.get(LiveRoom, recovery.room_id)
            assert room is not None
            if gate in {"paused", "suppressed"}:
                key = "recording_paused" if gate == "paused" else "recording_auto_restart_suppressed"
                room.room_config_json = json.dumps(merge_room_config(room, {key: True}))
            elif gate == "disabled":
                room.auto_record = False
            else:
                room.authorized = False
            db.add(room)
    await recovery.monitor._check_all()
    if gate != "retry":
        assert entry.reconcile_task is None
        assert not recovery.manager.is_running(recovery.room_id)
        return
    assert entry.reconcile_task is not None
    await asyncio.wait_for(entry.reconcile_task, 2)
    assert recovery.manager.is_running(recovery.room_id)
    assert recovery.room_id not in recovery.monitor._errors
