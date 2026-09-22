from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from loguru import logger
from sqlmodel import select

from app.analysis.source_policy import session_danmaku_lag_s, session_has_danmaku
from app.core.config import settings
from app.db.entities import AppSetting, Danmaku, LiveRoom, RawSegment, RecordingSession, SegmentTask
from app.db.session import get_session
from app.plugins.live_source import (
    DanmakuEvent,
    DanmakuSink,
    DanmakuStateSink,
    DanmakuStatus,
    LiveStatus,
    RoomSnapshot,
    SourceAuthenticationError,
    SourceDescriptor,
    SourceInvalidInput,
    SourceRoom,
    SourceTemporaryError,
    SourceUnavailable,
    StreamPreference,
    StreamSpec,
)
from app.recording.danmaku import DanmakuCapture, read_evidence
from app.recording.recorder import Recorder
from app.sources.registry import SourceRegistry, source_registry
from app.sources.rooms import register_room, room_source


class RuntimeSource:
    def __init__(self, platform: str = "external") -> None:
        self.descriptor = SourceDescriptor(platform=platform, name=platform, domains=(f"{platform}.invalid",))
        self.title = "标题 A"
        self.status = LiveStatus.LIVE
        self.failure: Exception | None = None
        self.stream_failure: Exception | None = None
        self.info_gate: asyncio.Event | None = None
        self.stream_gate: asyncio.Event | None = None
        self.requested = asyncio.Event()
        self.stream_calls = 0
        self.info_calls = 0
        self.closed = False
        self.order: list[str] = []

    async def resolve_room(self, value: str) -> SourceRoom:
        return SourceRoom(
            platform=self.descriptor.platform,
            source_id="room-A:123",
            canonical_url=f"https://{self.descriptor.platform}.invalid/123",
        )

    async def get_room_info(self, room: SourceRoom) -> RoomSnapshot:
        self.info_calls += 1
        self.requested.set()
        if self.info_gate is not None:
            await self.info_gate.wait()
        if self.failure is not None:
            raise self.failure
        return RoomSnapshot(status=self.status, title=self.title, uploader_name="外部主播")

    async def get_streams(self, room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        self.stream_calls += 1
        self.requested.set()
        if self.stream_gate is not None:
            await self.stream_gate.wait()
        if self.stream_failure is not None:
            raise self.stream_failure
        if self.failure is not None:
            raise self.failure
        return [
            StreamSpec(
                url="https://cdn.invalid/expired",
                transport="flv",
                container="flv",
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
            StreamSpec(
                url=f"https://cdn.invalid/live?token=secret-{self.stream_calls}",
                transport="flv",
                container="flv",
                quality_id="original",
                headers={"X-Platform-Key": "header-secret"},
            ),
        ]

    async def aclose(self) -> None:
        self.closed = True
        self.order.append("closed")


@pytest.fixture
def runtime(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> RuntimeSource:
    monkeypatch.setattr("app.web.services.notifications._NOTIFICATIONS", deque(maxlen=200))
    monkeypatch.setattr(source_registry, "_entries", SourceRegistry(timeout_s=1)._entries)
    monkeypatch.setattr(source_registry, "timeout_s", 1)
    monkeypatch.setattr(source_registry, "retry_delay_s", 0)
    monkeypatch.setattr(settings, "collect_danmaku", False)
    monkeypatch.setattr("app.pipeline.storage_lifecycle.should_stop_recording", lambda: False)
    source = RuntimeSource()
    source_registry.register_many("runtime", [source])
    return source


async def test_reconnect_refreshes_credentials_and_reuses_durable_idempotent_pipeline(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.pipeline.orchestrator import make_pipeline_callback

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.auto_analyze = True
        db.add(saved)
    received: list[str] = []

    async def media(self: Recorder, stream: StreamSpec, directory: Path) -> int:
        received.append(stream.url)
        command = self._build_ffmpeg_cmd(stream, directory, "part", directory / "segments.csv")
        assert command[command.index("-headers") + 1] == "X-Platform-Key: header-secret\r\n"
        assert "bilibili" not in " ".join(command) and "Cookie" not in " ".join(command)
        name = f"part{len(received)}.ts"
        (directory / name).write_bytes(b"media-boundary")
        line = f"{name},0,3"
        await self._register_segment(line, directory)
        await self._register_segment(line, directory)
        if len(received) == 2:
            self.stop()
        return 1

    monkeypatch.setattr(Recorder, "_record_once", media)
    recorder = Recorder(room_source(room), room.id, on_segment=make_pipeline_callback(room_id=room.id))
    await asyncio.wait_for(recorder.run(), 5)
    assert received == ["https://cdn.invalid/live?token=secret-1", "https://cdn.invalid/live?token=secret-2"]
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "stopped" and session.ended_at and session.stream_url is None
        assert session.reconnect_count == 1
        assert len(db.exec(select(RawSegment)).all()) == len(db.exec(select(SegmentTask)).all()) == 2
        assert not db.get(LiveRoom, room.id).auto_approve and not db.get(LiveRoom, room.id).auto_upload
        persisted = " ".join(row.value for row in db.exec(select(AppSetting)).all())
        assert "secret" not in persisted and "original" in persisted
    assert not session_has_danmaku(recorder.session_id)


async def test_disable_during_stream_fetch_never_starts_media(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = await register_room("123", True, "external")
    runtime.requested.clear()
    runtime.stream_gate = asyncio.Event()

    async def forbidden(*args: object) -> int:
        pytest.fail("停用后不应启动媒体进程")

    monkeypatch.setattr(Recorder, "_record_once", forbidden)
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    task = asyncio.create_task(recorder.run())
    await asyncio.wait_for(runtime.requested.wait(), 2)
    disabling = asyncio.create_task(source_registry.unregister_owner("runtime"))
    await asyncio.sleep(0)
    assert not source_registry.available("external") and recorder._stop.is_set()
    runtime.stream_gate.set()
    await asyncio.wait_for(asyncio.gather(task, disabling), 3)
    assert runtime.closed
    with get_session() as db:
        assert db.get(RecordingSession, recorder.session_id).ended_at is not None


async def test_disable_during_metadata_cancels_start_without_session(runtime: RuntimeSource) -> None:
    from app.web.services.rooms import RecorderManager

    room = await register_room("123", True, "external")
    runtime.requested.clear()
    runtime.info_gate = asyncio.Event()
    manager = RecorderManager()
    task = asyncio.create_task(manager.start(room.id, pipeline=False))
    await asyncio.wait_for(runtime.requested.wait(), 2)
    await source_registry.unregister_owner("runtime")
    with pytest.raises(SourceUnavailable):
        await task
    assert not manager.is_running(room.id)
    with get_session() as db:
        assert not db.exec(select(RecordingSession)).all()


async def test_auth_failure_ends_without_exhausting_retry_budget(runtime: RuntimeSource) -> None:
    room = await register_room("123", True, "external")
    runtime.failure = SourceAuthenticationError("credential-secret")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    await recorder.run()
    assert runtime.stream_calls == 1 and not recorder.retry_budget_exhausted
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "stopped" and "authentication" in session.error_message
        assert "secret" not in session.error_message


@pytest.mark.parametrize("error_type", [SourceAuthenticationError, SourceInvalidInput, SourceUnavailable])
@pytest.mark.parametrize("resume", ["manual", "auto"])
async def test_permanent_stream_failure_blocks_monitor_until_explicit_resume(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[SourceAuthenticationError | SourceInvalidInput | SourceUnavailable],
    resume: str,
) -> None:
    from app.analysis.room_config import load_room_config
    from app.pipeline.live_monitor import LiveMonitor
    from app.web import service
    from app.web.services import rooms

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.auto_record = True
        db.add(saved)
    manager = rooms.RecorderManager()
    monkeypatch.setattr(service, "recorder_manager", manager)
    started = asyncio.Event()

    async def ended(session_id: int) -> None:
        return None

    async def media(self: Recorder, stream: StreamSpec, directory: Path) -> int:
        started.set()
        await self._stop.wait()
        return 0

    monkeypatch.setattr(rooms, "_on_session_end", ended)
    monkeypatch.setattr(Recorder, "_record_once", media)
    monitor = LiveMonitor()
    monitor._stop = asyncio.Event()
    runtime.stream_failure = error_type("credential-secret")
    try:
        for _ in range(3):
            await monitor._check_all()
            await asyncio.gather(*list(manager._tasks.values()))
        assert runtime.stream_calls == 1
        with get_session() as db:
            sessions = db.exec(select(RecordingSession)).all()
            assert len(sessions) == 1 and sessions[0].ended_at is not None
            assert "secret" not in sessions[0].error_message
            saved = db.get(LiveRoom, room.id)
            flags = load_room_config(saved)
            assert flags["recording_auto_restart_suppressed"]
            assert not flags["recording_wait_for_next_live"] and not flags["recording_paused"]
            assert saved.auto_record and not saved.enabled
        assert manager.status(room.id)["state"] == "error"

        # 重建运行管理器后仍从数据库读取限制，修复凭据本身不隐式开启录制。
        manager = rooms.RecorderManager()
        monkeypatch.setattr(service, "recorder_manager", manager)
        runtime.stream_failure = None
        await monitor._check_all()
        assert runtime.stream_calls == 1 and not manager.is_running(room.id)
        if resume == "manual":
            await manager.start(room.id, pipeline=False)
        else:
            await manager.arm_auto_recording(room.id)
            await monitor._check_all()
        await asyncio.wait_for(started.wait(), 2)
        assert runtime.stream_calls == 2 and manager.is_running(room.id)
        with get_session() as db:
            saved = db.get(LiveRoom, room.id)
            assert not load_room_config(saved)["recording_auto_restart_suppressed"]
            assert not saved.auto_approve and not saved.auto_upload
            assert len(db.exec(select(RecordingSession)).all()) == 2
    finally:
        await manager.stop_all()
        await monitor.stop()


async def test_ffmpeg_stderr_cannot_leak_urls_headers_or_body(runtime: RuntimeSource) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id)
    messages: list[str] = []

    class Process:
        stderr = asyncio.StreamReader()

    process = Process()
    process.stderr.feed_data(b"https://cdn.invalid/?token=secret Cookie: header-secret Server returned 403\n")
    process.stderr.feed_eof()
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        await recorder._drain_stderr(process)
    finally:
        logger.remove(sink)
    assert "secret" not in " ".join(messages + recorder._stderr_tail)
    assert recorder._classify_recording_exit(1, recorder._stderr_tail).name == "UPSTREAM_UNAVAILABLE"


async def test_monitor_keeps_unknown_and_failures_and_polls_healthy_platform_again(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.pipeline.live_monitor import LiveMonitor
    from app.web import service
    from app.web.services.rooms import RecorderManager

    other = RuntimeSource("healthy")
    source_registry.register_many("healthy-plugin", [other])
    rooms = [await register_room("123", True, platform) for platform in ("external", "healthy")]
    with get_session() as db:
        for room in rooms:
            room.auto_record = True
            db.add(room)
    manager = RecorderManager()
    monkeypatch.setattr(service, "recorder_manager", manager)
    monitor = LiveMonitor()
    monitor._stop = asyncio.Event()
    runtime.status = LiveStatus.UNKNOWN
    other.status = LiveStatus.OFFLINE
    manager._set_recording_flags(rooms[0].id, wait_for_next_live=True)
    await monitor._check_all()
    from app.analysis.room_config import load_room_config

    with get_session() as db:
        assert load_room_config(db.get(LiveRoom, rooms[0].id))["recording_wait_for_next_live"]
    assert "未知" in monitor._errors[rooms[0].id]
    runtime.failure = SourceTemporaryError("network-secret")
    await monitor._check_all()
    assert "secret" not in monitor._errors[rooms[0].id]
    runtime.failure = None
    runtime.info_gate = asyncio.Event()
    runtime.requested.clear()
    before = other.info_calls
    try:
        await monitor._check_all(wait=False)
        await asyncio.wait_for(runtime.requested.wait(), 1)
        await asyncio.wait_for(monitor._platform_checks["healthy"], 1)
        await monitor._check_all(wait=False)
        await asyncio.wait_for(monitor._platform_checks["healthy"], 1)
        assert other.info_calls == before + 2
        assert not monitor._platform_checks["external"].done()
    finally:
        await monitor.stop()


async def test_delayed_stop_token_cannot_stop_a_new_recording(runtime: RuntimeSource) -> None:
    from app.web.services.rooms import RecorderManager

    room = await register_room("123", True, "external")
    manager = RecorderManager()
    old = asyncio.create_task(asyncio.sleep(0))
    await old
    current = asyncio.create_task(asyncio.Event().wait())
    manager._tasks[room.id] = current
    try:
        assert not await manager.stop_if_current(room.id, old)
        assert not current.done()
    finally:
        current.cancel()
        await asyncio.gather(current, return_exceptions=True)


class CommentSource(RuntimeSource):
    def __init__(self, *, send: bool = False, fail: bool = False) -> None:
        super().__init__()
        self.send = send
        self.fail = fail
        self.connected = asyncio.Event()

    async def collect_danmaku(self, room: SourceRoom, emit: DanmakuSink, state: DanmakuStateSink) -> None:
        try:
            if self.fail:
                raise SourceAuthenticationError("comment-secret")
            await state(DanmakuStatus.AVAILABLE)
            if self.send:
                await emit(
                    DanmakuEvent(
                        occurred_at=datetime.now(timezone(timedelta(hours=8))), content="普通文本", user_name="viewer"
                    )
                )
            self.connected.set()
            await asyncio.Event().wait()
        finally:
            self.order.append("comments_closed")


@pytest.mark.parametrize("mode", ["unsupported", "disabled", "available", "failed", "text"])
async def test_optional_comments_have_distinct_evidence_and_utc_mapping(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    from app.web.services.dashboard import danmaku_overview

    room = await register_room("123", True, "external")
    with get_session() as db:
        db.add(RecordingSession(id=1, room_id=room.id))
    source = runtime if mode == "unsupported" else CommentSource(send=mode == "text", fail=mode == "failed")
    monkeypatch.setattr(settings, "collect_danmaku", mode != "disabled")
    capture = DanmakuCapture(source, room_source(room), room.id, 1)
    await capture.start()
    if mode in {"available", "text"}:
        await asyncio.wait_for(source.connected.wait(), 2)
    elif mode == "failed":
        await asyncio.wait_for(capture._task, 2)
    await capture.stop()
    expected = "available" if mode == "text" else mode
    with get_session() as db:
        evidence = read_evidence(db, 1)
        assert evidence.status.value == expected and evidence.ended_at is not None
        events = db.exec(select(Danmaku)).all()
        assert len(events) == int(mode == "text")
        if events:
            assert events[0].room_id == room.id and events[0].value == 1.0
            assert abs((datetime.now(UTC) - events[0].ts.replace(tzinfo=UTC)).total_seconds()) < 5
    assert session_has_danmaku(1) == (expected == "available")
    assert session_danmaku_lag_s(1) == 0
    view = danmaku_overview(session_id=1)
    assert view["sessions"][0]["evidence"]["state"] == expected
    assert view["sessions"][0]["count"] == (int(mode == "text") if expected == "available" else None)


async def test_disconnection_retains_old_coverage_but_not_missing_window(runtime: RuntimeSource) -> None:
    from app.recording.danmaku import CaptureInterval, DanmakuEvidence

    room = await register_room("123", True, "external")
    now = datetime.now(UTC)
    evidence = DanmakuEvidence(
        status=DanmakuStatus.FAILED,
        intervals=[CaptureInterval(start=now - timedelta(seconds=20), end=now - timedelta(seconds=10))],
    )
    with get_session() as db:
        db.add(RecordingSession(id=1, room_id=room.id))
        db.add(AppSetting(key="session_danmaku:1", value=evidence.model_dump_json()))
    assert session_has_danmaku(1, now - timedelta(seconds=19), now - timedelta(seconds=11))
    assert not session_has_danmaku(1, now - timedelta(seconds=9), now)


async def test_active_source_drains_final_segment_and_comments_before_close(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.pipeline.orchestrator import make_pipeline_callback

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.auto_analyze = True
        db.add(saved)
    await source_registry.unregister_owner("runtime")
    source = CommentSource()
    source_registry.register_many("runtime", [source])
    monkeypatch.setattr(settings, "collect_danmaku", True)
    recording = asyncio.Event()

    async def media(self: Recorder, stream: StreamSpec, directory: Path) -> int:
        recording.set()
        try:
            await self._stop.wait()
        finally:
            (directory / "last.ts").write_bytes(b"last-fragment")
            await self._register_segment("last.ts,0,3", directory)
            source.order.append("segment")
        return 0

    async def ended(session_id: int) -> None:
        source.order.append("ended")

    monkeypatch.setattr(Recorder, "_record_once", media)
    recorder = Recorder(room_source(room), room.id, on_segment=make_pipeline_callback(room_id=room.id), on_end=ended)
    task = asyncio.create_task(recorder.run())
    await asyncio.wait_for(recording.wait(), 2)
    await asyncio.wait_for(source.connected.wait(), 2)
    await asyncio.wait_for(source_registry.unregister_owner("runtime"), 3)
    await task
    assert source.order == ["segment", "comments_closed", "ended", "closed"]
    with get_session() as db:
        assert len(db.exec(select(SegmentTask)).all()) == 1
        assert len(db.exec(select(RawSegment)).all()) == 1
        assert db.get(LiveRoom, room.id) is not None


def test_bilibili_frames_preserve_gift_superchat_sampling_and_text(temp_db: None) -> None:
    from app.sources.bilibili.danmaku import OP_MESSAGE, DanmakuClient, encode_packet

    messages = [
        {"cmd": "DANMU_MSG", "info": [[], "文字", [1, "用户"]]},
        {"cmd": "SEND_GIFT", "data": {"total_coin": 5000, "giftName": "礼物", "uname": "用户"}},
        {"cmd": "SUPER_CHAT_MESSAGE", "data": {"price": 30, "message": "SC", "user_info": {"uname": "用户"}}},
        {"cmd": "INTERACT_WORD", "data": {"uname": "用户"}},
    ]
    from app.analysis.danmaku_sampling import DanmakuSampler

    # 确定性采样丢弃第1条文字及第3条礼物，保留第2/4条；SC与进场全保留。
    messages = [messages[0], messages[0], messages[1], messages[1], messages[2], messages[3]]
    client = DanmakuClient(room_id=321, session_id=1)
    client._sampler = DanmakuSampler()
    client._handle_frame(b"".join(encode_packet(OP_MESSAGE, json.dumps(msg).encode()) for msg in messages))
    with get_session() as db:
        rows = db.exec(select(Danmaku).order_by(Danmaku.id)).all()
        assert [row.msg_type for row in rows] == ["danmaku", "gift", "superchat", "interact"]
        assert [row.value for row in rows] == [1.0, 5.0, 30.0, 0.2]
        assert all(row.room_id == 321 for row in rows)


async def test_crash_evidence_stops_at_last_checkpoint(runtime: RuntimeSource) -> None:
    from app.recording.danmaku import CaptureInterval, DanmakuEvidence, freeze_interrupted_captures

    room = await register_room("123", True, "external")
    now = datetime.now(UTC)
    checkpoint = now - timedelta(seconds=20)
    evidence = DanmakuEvidence(
        status=DanmakuStatus.AVAILABLE,
        confirmed_until=checkpoint,
        intervals=[CaptureInterval(start=now - timedelta(seconds=60))],
    )
    with get_session() as db:
        db.add(RecordingSession(id=1, room_id=room.id))
        db.add(AppSetting(key="session_danmaku:1", value=evidence.model_dump_json()))
    assert not session_has_danmaku(1, now - timedelta(seconds=10), now)
    freeze_interrupted_captures()
    freeze_interrupted_captures()
    with get_session() as db:
        saved = read_evidence(db, 1)
        assert saved.interrupted and saved.status == DanmakuStatus.FAILED
        assert saved.intervals[0].end == saved.ended_at == checkpoint
    assert session_has_danmaku(1, now - timedelta(seconds=50), now - timedelta(seconds=30))


async def test_bilibili_successful_zero_event_capture_does_not_fail_on_stop(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.sources.bilibili.danmaku import DanmakuClient
    from app.sources.bilibili.source import BilibiliSource

    connected = asyncio.Event()

    async def run(self: DanmakuClient) -> None:
        await self._report_state(DanmakuStatus.AVAILABLE)
        connected.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return  # 即使客户端正常返回，主动停用也不能伪装为采集失败。

    monkeypatch.setattr(DanmakuClient, "run", run)
    monkeypatch.setattr(settings, "collect_danmaku", True)
    identity = SourceRoom(platform="bilibili", source_id="123", canonical_url="https://live.bilibili.com/123")
    capture = DanmakuCapture(BilibiliSource(), identity, 1, 1)
    await capture.start()
    await asyncio.wait_for(connected.wait(), 2)
    await capture.stop()
    with get_session() as db:
        evidence = read_evidence(db, 1)
        assert evidence.status == DanmakuStatus.AVAILABLE and evidence.ended_at is not None
        assert not db.exec(select(Danmaku)).all()


@pytest.mark.parametrize("failure", [False, True])
def test_cli_external_recording_cleans_state_and_hides_process_secrets(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: bool,
) -> None:
    from typer.testing import CliRunner

    from app import cli
    from app.plugins.manager import PluginManager

    room = asyncio.run(register_room("123", True, "external"))
    manager = PluginManager(tmp_path / "plugins", registry=source_registry)
    monkeypatch.setattr("app.plugins.runtime.plugin_manager", manager)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr("app.core.osutil.open_path", lambda _path: None)

    async def media(self: Recorder, stream: StreamSpec, directory: Path) -> int:
        assert self.source_room.source_id == "room-A:123"
        if failure:
            raise OSError("token=process-secret; Cookie: header-secret")
        self.stop()
        return 0

    monkeypatch.setattr(Recorder, "_record_once", media)
    result = CliRunner().invoke(cli.app, ["record", str(room.id), "--no-pipeline"])
    assert result.exit_code == int(failure), result.output
    assert "process-secret" not in result.output and "header-secret" not in result.output
    with get_session() as db:
        assert not db.get(LiveRoom, room.id).enabled
        session = db.exec(select(RecordingSession)).one()
        assert session.status == ("error" if failure else "stopped")
        assert session.ended_at is not None and "secret" not in (session.error_message or "")


@pytest.mark.parametrize("trigger", ["schedule", "recovery"])
async def test_automatic_entries_keep_external_identity_and_independent_switches(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    from app.db.entities import RecordingSchedule
    from app.web import main, service
    from app.web.services import rooms

    room = await register_room("123", True, "external")
    manager = rooms.RecorderManager()
    monkeypatch.setattr(service, "recorder_manager", manager)
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    monkeypatch.setattr(rooms.settings_store, "recording_pipeline_enabled", lambda: True)
    started = asyncio.Event()

    async def media(self: Recorder, stream: StreamSpec, directory: Path) -> int:
        assert self.source_room.platform == "external" and self.source_room.source_id == "room-A:123"
        started.set()
        await self._stop.wait()
        return 0

    async def ended(session_id: int) -> None:
        return None

    monkeypatch.setattr(Recorder, "_record_once", media)
    monkeypatch.setattr(rooms, "_on_session_end", ended)
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.schedule_enabled = True
        db.add(saved)
        if trigger == "schedule":
            db.add(RecordingSchedule(room_id=room.id, scheduled_at=datetime.now(UTC) - timedelta(seconds=1)))
        else:
            db.add(RecordingSession(id=1, room_id=room.id, status="recording"))
    try:
        if trigger == "schedule":
            await main._run_due_schedules()
        else:
            assert await rooms.auto_recover_interrupted_sessions() == [room.id]
        await asyncio.wait_for(started.wait(), 2)
        with get_session() as db:
            saved = db.get(LiveRoom, room.id)
            assert not any((saved.auto_analyze, saved.auto_render, saved.auto_approve, saved.auto_upload))
            if trigger == "recovery":
                assert db.get(RecordingSession, 1).status == "interrupted"
    finally:
        await manager.stop(room.id)


async def test_manual_pause_and_source_disable_preserve_flags_and_finalize_once(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.analysis.room_config import load_room_config
    from app.web.services import rooms

    room = await register_room("123", True, "external")
    manager = rooms.RecorderManager()
    started = asyncio.Event()
    endings: list[int] = []

    async def media(self: Recorder, stream: StreamSpec, directory: Path) -> int:
        started.set()
        await self._stop.wait()
        (directory / "last.ts").write_bytes(b"final")
        await self._register_segment("last.ts,0,1", directory)
        return 0

    async def ended(session_id: int) -> None:
        endings.append(session_id)

    monkeypatch.setattr(Recorder, "_record_once", media)
    monkeypatch.setattr(rooms, "_on_session_end", ended)
    await manager.start(room.id, pipeline=False)
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(
        asyncio.gather(
            manager.stop(room.id, mark_paused=True, pause_auto_restart=True),
            source_registry.unregister_owner("runtime"),
        ),
        3,
    )
    with get_session() as db:
        config = load_room_config(db.get(LiveRoom, room.id))
        assert config["recording_paused"] and config["recording_auto_restart_suppressed"]
        assert db.exec(select(RecordingSession)).one().status == "paused"
        assert len(db.exec(select(RawSegment)).all()) == len(endings) == 1
    assert runtime.closed and not manager.is_running(room.id)


async def test_capture_stop_waits_for_started_database_write(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    room = await register_room("123", True, "external")
    source = CommentSource()
    monkeypatch.setattr(settings, "collect_danmaku", True)
    capture = DanmakuCapture(source, room_source(room), room.id, 1)
    writing = threading.Event()
    release = threading.Event()
    original = capture._save_sync

    def delayed(payload: str) -> None:
        if json.loads(payload)["status"] == "available" and not json.loads(payload)["ended_at"]:
            writing.set()
            assert release.wait(5)
        original(payload)

    monkeypatch.setattr(capture, "_save_sync", delayed)
    await capture.start()
    stop_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(writing.wait, 2)
        stop_task = asyncio.create_task(capture.stop())
        await asyncio.sleep(0.02)
        assert not stop_task.done()
    finally:
        release.set()
        if stop_task is not None:
            await asyncio.wait_for(stop_task, 2)
        else:
            await capture.stop()
    with get_session() as db:
        assert read_evidence(db, 1).ended_at is not None


def test_orphan_comment_history_has_unknown_identity(temp_db: None) -> None:
    from app.web.services.dashboard import danmaku_overview

    with get_session() as db:
        db.add(Danmaku(session_id=999, room_id=888, content="历史文本"))
    result = danmaku_overview(session_id=999)
    assert result["sessions"][0]["source_label"] == "未知来源"
    assert result["sessions"][0]["count"] == 1


async def test_repeated_cancellation_waits_for_session_finalization(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = await register_room("123", True, "external")
    recording = asyncio.Event()
    finalizing = asyncio.Event()
    release = asyncio.Event()
    ended_ids: list[int] = []

    async def media(self: Recorder, stream: StreamSpec, directory: Path) -> int:
        recording.set()
        await asyncio.Event().wait()
        return 0

    async def ended(session_id: int) -> None:
        finalizing.set()
        await release.wait()
        ended_ids.append(session_id)

    monkeypatch.setattr(Recorder, "_record_once", media)
    recorder = Recorder(room_source(room), room.id, on_end=ended)
    task = asyncio.create_task(recorder.run())
    await asyncio.wait_for(recording.wait(), 2)
    task.cancel()
    await asyncio.wait_for(finalizing.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert source_registry._entries["external"].users
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ended_ids == [recorder.session_id]
    assert not source_registry._entries["external"].users


async def test_repeated_cancel_waits_for_initial_capture_write_before_releasing_source(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    room = await register_room("123", True, "external")
    writing = threading.Event()
    release = threading.Event()
    original = DanmakuCapture._save_sync

    def delayed(self: DanmakuCapture, payload: str) -> None:
        if not json.loads(payload)["ended_at"]:
            writing.set()
            assert release.wait(5)
        original(self, payload)

    monkeypatch.setattr(DanmakuCapture, "_save_sync", delayed)
    recorder = Recorder(room_source(room), room.id)
    task = asyncio.create_task(recorder.run())
    try:
        assert await asyncio.to_thread(writing.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert source_registry._entries["external"].users
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    with get_session() as db:
        assert read_evidence(db, recorder.session_id).ended_at is not None
    assert not source_registry._entries["external"].users
