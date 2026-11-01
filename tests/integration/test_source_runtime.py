from __future__ import annotations

import asyncio
import errno
import json
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from loguru import logger
from sqlmodel import select

from app.analysis.source_policy import session_danmaku_lag_s, session_has_danmaku
from app.core.config import settings
from app.core.paths import resolve_storage_path
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
from app.recording.writer_ownership import SessionWriter
from app.sources.registry import SourceRegistry, source_registry
from app.sources.rooms import register_room, room_source
from tests.process_fixtures import ExternalProcess, install_process_boundary


@pytest.fixture
def recording_writer(tmp_path: Path) -> Iterator[SessionWriter]:
    with SessionWriter.create(tmp_path / "writers") as writer:
        yield writer


@pytest.mark.parametrize("failures", [1, 2])
async def test_failed_danmaku_checkpoint_freezes_last_confirmed_coverage(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, failures: int
) -> None:
    from app.recording.danmaku import CaptureInterval

    room = await register_room("123", True, "external")
    capture = DanmakuCapture(runtime, room_source(room), room.id, 999)
    confirmed = datetime.now(UTC) - timedelta(seconds=20)
    capture.evidence.status = DanmakuStatus.AVAILABLE
    capture.evidence.confirmed_until = confirmed
    capture.evidence.intervals = [CaptureInterval(start=confirmed - timedelta(seconds=30))]
    original = capture._save_sync
    calls = 0

    def fail_then_recover(payload: str) -> None:
        nonlocal calls
        calls += 1
        if calls <= failures:
            raise OSError("coverage database unavailable")
        original(payload)

    monkeypatch.setattr(capture, "_save_sync", fail_then_recover)
    failed = asyncio.create_task(capture._save())
    failed.add_done_callback(capture._observe_task)
    await asyncio.gather(failed, return_exceptions=True)
    await asyncio.sleep(0)
    await capture.stop()
    assert capture.evidence.status == DanmakuStatus.FAILED
    assert capture.evidence.intervals[-1].end == confirmed
    with get_session() as db:
        stored = read_evidence(db, 999)
        assert stored.status == DanmakuStatus.FAILED
        assert stored.intervals[-1].end == confirmed and stored.ended_at == confirmed


async def test_danmaku_failure_waits_for_older_checkpoint_before_saving_final_state(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from app.recording.danmaku import CaptureInterval

    room = await register_room("123", True, "external")
    capture = DanmakuCapture(runtime, room_source(room), room.id, 999)
    confirmed = datetime.now(UTC) - timedelta(seconds=20)
    capture.evidence.status = DanmakuStatus.AVAILABLE
    capture.evidence.confirmed_until = confirmed
    capture.evidence.intervals = [CaptureInterval(start=confirmed - timedelta(seconds=30))]
    started, release = threading.Event(), threading.Event()
    original = capture._save_sync
    writes: list[str] = []

    def delayed_first_write(payload: str) -> None:
        if not started.is_set():
            started.set()
            assert release.wait(5), "test did not release pending write"
        original(payload)
        writes.append(payload)

    async def checkpoint() -> None:
        async with capture._state_lock:
            await capture._save()

    async def failed_helper() -> None:
        raise OSError("capture helper failed")

    monkeypatch.setattr(capture, "_save_sync", delayed_first_write)
    capture._task = asyncio.create_task(checkpoint())
    try:
        assert await asyncio.to_thread(started.wait, 3)
        assert capture.evidence.confirmed_until == confirmed
        capture._heartbeat_task = asyncio.create_task(failed_helper())
        capture._heartbeat_task.add_done_callback(capture._observe_task)
        await asyncio.gather(capture._heartbeat_task, return_exceptions=True)
        await asyncio.sleep(0)
        assert capture.evidence.status == DanmakuStatus.FAILED
        assert not writes
    finally:
        release.set()
        await capture.stop()
    with get_session() as db:
        stored = read_evidence(db, 999)
        assert stored.status == DanmakuStatus.FAILED
        assert stored.ended_at == confirmed and stored.confirmed_until == confirmed
        assert stored.intervals[-1].end == confirmed


@pytest.mark.parametrize("method,stage", [("_watch_segments", "segments"), ("_monitor_disk", "disk")])
async def test_recording_supervises_required_helpers(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, method: str, stage: str
) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)

    class Process(ExternalProcess):
        stdin = None
        returncode: int | None = None

        def __init__(self) -> None:
            self.stderr = asyncio.StreamReader()
            self.exited = asyncio.Event()

        async def wait(self) -> int:
            await self.exited.wait()
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            self.returncode = 1
            self.stderr.feed_eof()
            self.exited.set()

    process = Process()

    async def spawn(*args: object, **kwargs: object) -> Process:
        return process

    async def broken(*args: object) -> None:
        raise PermissionError("helper cannot write token=private-helper")

    install_process_boundary(monkeypatch, spawn)
    monkeypatch.setattr(recorder, method, broken)
    before = asyncio.all_tasks()
    with pytest.raises(PermissionError):
        await asyncio.wait_for(recorder.run(), 2)
    assert process.exited.is_set() and recorder.recording_action_required
    assert stage in recorder.recording_error and "PermissionError" in recorder.recording_error
    assert "private-helper" not in recorder.recording_error
    assert not (asyncio.all_tasks() - before)
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "error" and session.ended_at is not None


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
        assert session.status == "stopped" and session.ended_at and "stream_url" not in session.model_dump()
        assert session.reconnect_count == 1
        assert len(db.exec(select(RawSegment)).all()) == len(db.exec(select(SegmentTask)).all()) == 2
        assert not db.get(LiveRoom, room.id).auto_approve and not db.get(LiveRoom, room.id).auto_upload
        persisted = " ".join(row.value for row in db.exec(select(AppSetting)).all())
        assert "secret" not in persisted and "original" in persisted
    assert not session_has_danmaku(recorder.session_id)


async def test_recording_rotates_cdn_and_formats_with_fresh_credentials_and_finite_budget(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import urlsplit

    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    received: list[tuple[str, str, str]] = []
    waits: list[float] = []
    messages: list[str] = []
    routes = [
        ("a.invalid", "hls", "ts"),
        ("b.invalid", "hls", "ts"),
        ("c.invalid", "hls", "fmp4"),
        ("d.invalid", "flv", "flv"),
    ]

    async def streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        runtime.stream_calls += 1
        return [
            StreamSpec(
                url=f"https://{host}/private-path-{runtime.stream_calls}?token=private-{runtime.stream_calls}",
                transport=transport,
                container=container,
                headers={"X-Secret": f"header-private-{runtime.stream_calls}"},
            )
            for host, transport, container in routes
        ]

    async def media(stream: StreamSpec, directory: Path) -> int:
        assert runtime.stream_calls == len(received) + 1
        assert stream.url.endswith(f"token=private-{runtime.stream_calls}")
        assert stream.headers["X-Secret"] == f"header-private-{runtime.stream_calls}"
        received.append((urlsplit(stream.url).hostname, stream.transport, stream.container))
        recorder._remember_stderr(b"[https @ 0001] Error reading HTTP response: Error number -138 occurred")
        return 4294967158

    async def wait(seconds: float) -> None:
        if seconds >= settings.room_metadata_refresh_interval_s:
            await recorder._stop.wait()
            return
        waits.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(runtime, "get_streams", streams)
    monkeypatch.setattr(recorder, "_record_once", media)
    monkeypatch.setattr(recorder, "_sleep_or_stop", wait)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 5)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 0)
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        await recorder.run()
    finally:
        logger.remove(sink)
    assert received == [*routes, routes[0]]
    assert all(0 <= delay <= 1 for delay in waits[:3])
    assert recorder.retry_budget_exhausted and runtime.stream_calls == 5
    assert not any("private" in message for message in messages)
    assert sum("category=TRANSIENT_NETWORK" in message for message in messages) == 5
    assert any("candidate=4/4" in message and "container=flv" in message for message in messages)
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "error" and session.ended_at is not None


@pytest.mark.parametrize("unsupported_progress", [False, True])
async def test_serial_reconnect_rotates_same_host_endpoints_with_refreshed_credentials(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, unsupported_progress: bool
) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    received: list[str] = []
    probes: list[str] = []

    async def streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        runtime.stream_calls += 1
        return [
            StreamSpec(
                url=f"https://cdn.invalid/{name}?token=private-{runtime.stream_calls}",
                headers={"X-Key": f"private-header-{runtime.stream_calls}"},
                transport="hls",
                container="ts",
            )
            for name in ("first", "backup")
        ]

    class FailedProcess(ExternalProcess):
        stdin = None
        returncode = 1

        def __init__(self, message: bytes) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(message)
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    async def spawn(*args: str, **kwargs: object) -> FailedProcess:
        url = args[args.index("-i") + 1]
        if "-progress" in args:
            probes.append(url)
            return FailedProcess(b"Unrecognized option 'progress'.\n")
        received.append(url)
        assert f"private-header-{runtime.stream_calls}" in args[args.index("-headers") + 1]
        return FailedProcess(b"[https @ 1] Connection timed out\n")

    monkeypatch.setattr(runtime, "get_streams", streams)
    install_process_boundary(monkeypatch, spawn)
    monkeypatch.setattr(settings, "recording_stream_race_parallel", 2 if unsupported_progress else 1)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 3)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 30)
    await asyncio.wait_for(recorder.run(), 15)
    assert received == [
        "https://cdn.invalid/first?token=private-1",
        "https://cdn.invalid/backup?token=private-2",
        "https://cdn.invalid/first?token=private-3",
    ]
    assert bool(probes) == unsupported_progress
    assert recorder._stream_race_supported != unsupported_progress
    assert runtime.stream_calls == 3 and recorder.retry_budget_exhausted
    assert not recorder.recording_action_required


@pytest.mark.parametrize("stop_during_wait", [False, True])
async def test_failover_resets_after_stable_media_and_respects_stop(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, stop_during_wait: bool
) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    received: list[str] = []

    async def streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        return [StreamSpec(url=f"https://{host}.invalid/live", transport="flv", container="flv") for host in ("a", "b")]

    async def media(stream: StreamSpec, directory: Path) -> int:
        received.append(stream.url)
        if len(received) == 2:
            recorder._seq += 1
            recorder._attempt_media_seconds = settings.segment_duration_s
        if len(received) == 3:
            recorder.stop()
        recorder._remember_stderr(b"Connection timed out")
        return 1

    async def wait(seconds: float) -> None:
        if seconds >= settings.room_metadata_refresh_interval_s:
            await recorder._stop.wait()
            return
        if stop_during_wait:
            recorder.stop()
        await asyncio.sleep(0)

    monkeypatch.setattr(runtime, "get_streams", streams)
    monkeypatch.setattr(recorder, "_record_once", media)
    monkeypatch.setattr(recorder, "_sleep_or_stop", wait)
    await asyncio.wait_for(recorder.run(), 3)
    expected = ["https://a.invalid/live"]
    if not stop_during_wait:
        expected.extend(["https://b.invalid/live", "https://a.invalid/live"])
    assert received == expected
    assert not recorder.retry_budget_exhausted


@pytest.mark.parametrize("stop_in_race,finalization_error", [(False, False), (False, True), (True, False)])
async def test_race_registers_only_winner_and_reaps_all_processes(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, stop_in_race: bool, finalization_error: bool
) -> None:
    from urllib.parse import urlsplit

    from app.pipeline.orchestrator import make_pipeline_callback

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.auto_analyze = True
        db.add(saved)
    processes = []
    received = []
    pipeline = make_pipeline_callback(room_id=room.id)
    recorder = None

    class Process(ExternalProcess):
        stdin = None
        returncode = None

        def __init__(self) -> None:
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.exited = asyncio.Event()

        async def wait(self) -> int:
            await self.exited.wait()
            return self.returncode

        def kill(self) -> None:
            if self.returncode is None:
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.exited.set()

        terminate = kill

    async def source_streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        return [
            StreamSpec(url=f"https://{host}.invalid/live?token=private", transport="flv", container="flv")
            for host in ("slow", "good", "unused")
        ]

    async def spawn(*args: str, **kwargs: object) -> Process:
        proc = Process()
        processes.append(proc)
        output = Path(args[-1].replace("%05d", "00000"))
        output.write_bytes(b"candidate media")
        # 连落选者都已经产出清单，必须确保它没有被登记或驱动分析。
        Path(args[args.index("-segment_list") + 1]).write_text(f"{output.name},0,5\n")
        if stop_in_race:
            recorder.stop()
        elif urlsplit(args[args.index("-i") + 1]).hostname == "good.invalid":
            proc.stdout.feed_data(
                b"frame=1\nout_time_us=0\nprogress=continue\nframe=20\nout_time_us=1000000\nprogress=continue\n"
            )
        return proc

    async def on_segment(segment: RawSegment) -> None:
        received.append(segment)
        await pipeline(segment)
        recorder.stop()

    monkeypatch.setattr(settings, "recording_stream_race_parallel", 2)
    monkeypatch.setattr(runtime, "get_streams", source_streams)
    install_process_boundary(monkeypatch, spawn)
    recorder = Recorder(room_source(room), room.id, on_segment=on_segment, metadata_prepared=True)
    original_scan = recorder._scan_orphan_segments

    async def scan(manifest: Path, directory: Path, *, prefix: str) -> None:
        assert manifest.exists(), "末片登记前不能删除清单"
        assert all(proc.returncode is not None for proc in processes)
        await original_scan(manifest, directory, prefix=prefix)
        if finalization_error:
            raise PermissionError("final registration unavailable")

    monkeypatch.setattr(recorder, "_scan_orphan_segments", scan)
    before = asyncio.all_tasks()
    if finalization_error:
        with pytest.raises(PermissionError, match="final registration"):
            await asyncio.wait_for(recorder.run(), 3)
    else:
        await asyncio.wait_for(recorder.run(), 3)
    assert processes and all(proc.returncode is not None for proc in processes)
    assert not (asyncio.all_tasks() - before)
    with get_session() as db:
        segments = db.exec(select(RawSegment)).all()
        tasks = db.exec(select(SegmentTask)).all()
        assert len(segments) == len(tasks) == len(received) == (0 if stop_in_race else 1)
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == ("error" if finalization_error else "stopped") and session.ended_at is not None
        if not stop_in_race:
            assert recorder._selected_stream.url.startswith("https://good.invalid/")
            media = resolve_storage_path(segments[0].file_path)
            folder = media.parent
            assert list(folder.glob("*.ts")) == [media]
            assert len(list(folder.glob("*.csv"))) == (1 if finalization_error else 0)


@pytest.mark.parametrize("handoff", ["stop", "save_error"])
async def test_unaccepted_winner_is_discarded(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handoff: str,
    recording_writer: SessionWriter,
) -> None:
    from app.recording.stream_race import StreamAttempt, StreamRace

    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    recorder._stream_candidates = [StreamSpec(url="https://cdn.invalid/live", transport="flv", container="flv")]
    recorder._writer = recording_writer

    class Process:
        returncode: int | None = None
        pid = 42

        def __init__(self) -> None:
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()

        def kill(self) -> None:
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    process = Process()

    async def select_candidate(race: StreamRace) -> StreamAttempt:
        attempt = race.attempts[0]
        attempt.process = process
        attempt.stderr_task = asyncio.create_task(attempt.drain_stderr())
        attempt.progress_task = asyncio.create_task(attempt.drain_progress())
        (tmp_path / f"{attempt.prefix}00000.ts").write_bytes(b"unaccepted media")
        attempt.segment_list.write_text("pending manifest")
        race.winner = attempt
        if handoff == "stop":
            recorder.stop()
        return attempt

    def fail_save(stream: StreamSpec) -> None:
        raise PermissionError("selection save unavailable")

    monkeypatch.setattr(StreamRace, "run", select_candidate)
    monkeypatch.setattr(recorder, "_save_stream_selection", fail_save)
    old = tmp_path / "part_old.ts"
    old.write_bytes(b"existing recording")
    before = asyncio.all_tasks()
    if handoff == "stop":
        assert await recorder._record_race(tmp_path) == -1
    else:
        with pytest.raises(PermissionError, match="selection save"):
            await recorder._record_race(tmp_path)
    assert process.returncode is not None and not (asyncio.all_tasks() - before)
    assert list(tmp_path.glob("*.ts")) == [old]
    assert not list(tmp_path.glob("*.csv"))
    with get_session() as db:
        assert not db.exec(select(RawSegment)).all()


async def test_initial_failed_race_is_included_in_elapsed_retry_budget(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.recording import recorder as recorder_module
    from app.recording.stream_race import StreamRace

    room = await register_room("123", True, "external")
    now = 100.0
    calls = 0

    async def source_streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        nonlocal calls
        calls += 1
        return [StreamSpec(url=f"https://{host}.invalid/live", transport="flv", container="flv") for host in ("a", "b")]

    async def failed_race(self: StreamRace) -> None:
        nonlocal now
        now += 10  # 首轮竞争已把预算用尽，不能再开一轮。

    monkeypatch.setattr(runtime, "get_streams", source_streams)
    monkeypatch.setattr(StreamRace, "run", failed_race)
    monkeypatch.setattr(recorder_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(settings, "recording_stream_race_parallel", 2)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 0)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 10)
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    await asyncio.wait_for(recorder.run(), 3)
    assert calls == 1 and recorder.retry_budget_exhausted


@pytest.mark.parametrize(
    "message,permanent",
    [
        (b"Error opening input: Invalid argument", False),
        (b"HTTP error 403\nError opening input: Permission denied", False),
        (b"Error opening output: No space left on device", True),
    ],
)
async def test_race_input_errors_retry_but_local_output_fault_stops_room(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, message: bytes, permanent: bool
) -> None:
    from app.recording.stream_race import StreamAttempt, StreamRace

    room = await register_room("123", True, "external")
    calls = 0

    async def source_streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        nonlocal calls
        calls += 1
        return [StreamSpec(url=f"https://{host}.invalid/live", transport="flv", container="flv") for host in ("a", "b")]

    async def fail(self: StreamRace, attempt: StreamAttempt) -> bool:
        attempt.remember(message)
        return False

    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)

    async def wait(seconds: float) -> None:
        if seconds >= settings.room_metadata_refresh_interval_s:
            await recorder._stop.wait()
        else:
            await asyncio.sleep(0)

    monkeypatch.setattr(runtime, "get_streams", source_streams)
    monkeypatch.setattr(StreamRace, "_compete", fail)
    monkeypatch.setattr(recorder, "_sleep_or_stop", wait)
    monkeypatch.setattr(settings, "recording_stream_race_parallel", 2)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 2)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 0)
    await asyncio.wait_for(recorder.run(), 3)
    assert calls == (1 if permanent else 2)
    assert recorder.recording_action_required is permanent
    assert recorder.retry_budget_exhausted is not permanent


async def test_unsupported_progress_falls_back_once_and_keeps_legacy_recording(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.recording.stream_race import StreamRace

    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    fetched = 0
    races = 0
    recorded = []

    async def source_streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        nonlocal fetched
        fetched += 1
        return [
            StreamSpec(url=f"https://{host}.invalid/live?token={fetched}", transport="flv", container="flv")
            for host in ("a", "b")
        ]

    async def unsupported(self: StreamRace) -> None:
        nonlocal races
        races += 1
        attempt = self.attempts[0]
        attempt.remember(b"Unrecognized option 'progress'.")
        self.failures.append(attempt)

    async def single(stream: StreamSpec, out_dir: Path) -> int:
        recorded.append(stream.url)
        if len(recorded) == 2:
            recorder.stop()
            return 0
        recorder._remember_stderr(b"Connection timed out")
        return 1

    async def wait(seconds: float) -> None:
        if seconds >= settings.room_metadata_refresh_interval_s:
            await recorder._stop.wait()
        else:
            await asyncio.sleep(0)

    monkeypatch.setattr(settings, "recording_stream_race_parallel", 2)
    monkeypatch.setattr(runtime, "get_streams", source_streams)
    monkeypatch.setattr(StreamRace, "run", unsupported)
    monkeypatch.setattr(recorder, "_record_single", single)
    monkeypatch.setattr(recorder, "_sleep_or_stop", wait)
    await asyncio.wait_for(recorder.run(), 3)
    assert races == 1 and fetched == len(recorded) == 2
    assert recorded == ["https://a.invalid/live?token=1", "https://b.invalid/live?token=2"]
    assert not recorder.recording_action_required and not recorder._stream_race_supported
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "stopped" and session.ended_at is not None


@pytest.mark.parametrize("stop_stage", ["selection", "guard_cleanup"])
async def test_stop_during_compatibility_fallback_never_starts_media(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stop_stage: str,
    recording_writer: SessionWriter,
) -> None:
    from app.recording.stream_race import StreamRace

    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    recorder._stream_candidates = [
        StreamSpec(url=f"https://{host}.invalid/live", transport="flv", container="flv") for host in ("a", "b")
    ]
    guard_started = asyncio.Event()
    recorder._writer = recording_writer

    async def unsupported(self: StreamRace) -> None:
        await guard_started.wait()
        attempt = self.attempts[0]
        attempt.remember(b"Unrecognized option 'progress'.")
        self.failures.append(attempt)
        if stop_stage == "selection":
            recorder.stop()

    async def guard() -> None:
        guard_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            if stop_stage == "guard_cleanup":
                recorder.stop()

    async def forbidden(*args: object) -> int:
        pytest.fail("停止后不应通过兼容回退启动媒体进程")

    monkeypatch.setattr(StreamRace, "run", unsupported)
    monkeypatch.setattr(recorder, "_monitor_disk", guard)
    monkeypatch.setattr(recorder, "_record_single", forbidden)
    assert await asyncio.wait_for(recorder._record_race(tmp_path), 2) == -1


async def test_compatibility_fallback_does_not_mask_other_global_errors(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recording_writer: SessionWriter,
) -> None:
    from app.recording.stream_race import StreamRace

    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    recorder._stream_candidates = [
        StreamSpec(url=f"https://{host}.invalid/live", transport="flv", container="flv") for host in ("a", "b")
    ]

    async def failed(self: StreamRace) -> None:
        for attempt, message in zip(
            self.attempts, (b"Unrecognized option 'progress'.", b"Unrecognized option 'other'."), strict=True
        ):
            attempt.remember(message)
            self.failures.append(attempt)

    recorder._writer = recording_writer

    async def forbidden(*args: object) -> int:
        pytest.fail("其他全局参数错误不应被兼容回退掩盖")

    monkeypatch.setattr(StreamRace, "run", failed)
    monkeypatch.setattr(recorder, "_record_single", forbidden)
    assert await asyncio.wait_for(recorder._record_race(tmp_path), 2) == 1
    assert recorder._stream_race_supported
    assert "other" in " ".join(recorder._stderr_tail)


@pytest.mark.parametrize("resource_errno", [errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EAGAIN])
async def test_race_resource_exhaustion_uses_existing_local_failure_cleanup(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, resource_errno: int
) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    calls = 0

    async def source_streams(room: SourceRoom, preference: StreamPreference) -> list[StreamSpec]:
        return [StreamSpec(url=f"https://{host}.invalid/live", transport="flv", container="flv") for host in ("a", "b")]

    async def spawn(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise OSError(resource_errno, "private details")

    monkeypatch.setattr(settings, "recording_stream_race_parallel", 2)
    monkeypatch.setattr(runtime, "get_streams", source_streams)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(OSError) as captured:
        await asyncio.wait_for(recorder.run(), 3)
    assert captured.value.errno == resource_errno and calls == 1
    assert recorder.recording_action_required and not recorder.retry_budget_exhausted
    assert errno.errorcode[resource_errno] in recorder.recording_error
    assert "private" not in recorder.recording_error
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "error" and session.ended_at is not None


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


@pytest.mark.parametrize("disable_recording", [True, False])
async def test_automatic_start_rechecks_switches_after_metadata(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, disable_recording: bool
) -> None:
    from app.web.services.rooms import RecorderManager, update_room

    room = await register_room("123", True, "external")
    update_room(room.id, {"auto_record": True, "auto_analyze": True, "auto_render": True})
    runtime.requested.clear()
    runtime.info_gate = asyncio.Event()
    manager = RecorderManager()
    callbacks: list[object] = []

    async def record(self: Recorder) -> None:
        callbacks.append(self.on_segment)

    monkeypatch.setattr(Recorder, "run", record)
    starting = asyncio.create_task(manager.start(room.id, pipeline=True, produce=True, automatic=True))
    await asyncio.wait_for(runtime.requested.wait(), 2)
    update_room(room.id, {"auto_record": not disable_recording, "auto_analyze": False, "auto_render": False})
    runtime.info_gate.set()
    await starting
    await asyncio.sleep(0)
    assert callbacks == ([] if disable_recording else [None])
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        assert not saved.auto_analyze and not saved.auto_render


async def test_auth_failure_ends_without_exhausting_retry_budget(runtime: RuntimeSource) -> None:
    room = await register_room("123", True, "external")
    runtime.failure = SourceAuthenticationError("credential-secret")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    await recorder.run()
    assert runtime.stream_calls == 1 and not recorder.retry_budget_exhausted
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "error" and "authentication" in session.error_message
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
    from app.web.services import rooms
    from app.web.services import rooms as rooms_service

    room = await register_room("123", True, "external")
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.auto_record = True
        db.add(saved)
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms_service, "recorder_manager", manager)
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
        monkeypatch.setattr(rooms_service, "recorder_manager", manager)
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


@pytest.mark.parametrize(
    "failure,category",
    [
        ("No space left on device", "DISK_FULL"),
        ("Permission denied", "PERMISSION_DENIED"),
        ("Unrecognized option 'bad'", "INVALID_ARGUMENT"),
        ("Unknown encoder 'bad'", "UNSUPPORTED_CODEC"),
        (errno.ENOENT, "MISSING_BINARY"),
        (errno.EACCES, "PERMISSION_DENIED"),
    ],
)
async def test_ffmpeg_permanent_failure_persists_error_and_blocks_automatic_restart(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, failure: str | int, category: str
) -> None:
    from app.analysis.room_config import load_room_config
    from app.core import logging as app_logging
    from app.pipeline.live_monitor import LiveMonitor
    from app.web.services import rooms
    from app.web.services.logs import list_logs

    room = await register_room("123", True, "external")
    rooms.update_room(room.id, {"auto_record": True})
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms, "recorder_manager", manager)
    spawn_count = 0

    class Process(ExternalProcess):
        returncode = 1
        stdin = None

        def __init__(self) -> None:
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(f"Connection reset\n{failure}\n".encode())
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode

    async def spawn(*args: object, **kwargs: object) -> Process:
        nonlocal spawn_count
        spawn_count += 1
        assert spawn_count == 1
        if isinstance(failure, int):
            raise OSError(failure, "process start failed")
        return Process()

    async def ended(session_id: int) -> None:
        return None

    install_process_boundary(monkeypatch, spawn)
    monkeypatch.setattr(rooms, "_on_session_end", ended)
    monitor = LiveMonitor()
    monitor._stop = asyncio.Event()
    # 完整测试中 CLI 可能已初始化异步 DB sink；不能再注册第二个。
    sink = (
        None
        if app_logging._CONFIGURED
        else logger.add(app_logging._db_sink, level="WARNING", filter=app_logging._filter_db_sink)
    )
    try:
        for _ in range(3):
            await monitor._check_all()
            await asyncio.gather(*list(manager._tasks.values()))
        assert spawn_count == 1
        with get_session() as db:
            sessions = db.exec(select(RecordingSession)).all()
            assert len(sessions) == 1 and sessions[0].status == "error"
            assert category in sessions[0].error_message and sessions[0].ended_at is not None
            saved = db.get(LiveRoom, room.id)
            assert load_room_config(saved)["recording_auto_restart_suppressed"]
        await logger.complete()
        messages = [entry["message"] for entry in list_logs() if "录制异常" in entry["message"]]
        assert len(messages) == 1
        assert all(
            field in messages[0]
            for field in ("session=", "exit_code=1", f"category={category}", "retry=False", "attempt=", "reason=")
        )
        assert manager.status(room.id)["state"] == "error"
    finally:
        if sink is not None:
            logger.remove(sink)
        await manager.stop_all()
        await monitor.stop()


@pytest.mark.parametrize(
    "stderr",
    [
        "Connection timed out",
        "Server returned 403",
        "unrecognized upstream failure",
        "Packet corrupt\nConnection timed out",
    ],
)
async def test_short_fragments_do_not_reset_transient_or_unknown_failure_budget(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    attempts = 0
    waits: list[float] = []

    async def media(stream: StreamSpec, directory: Path) -> int:
        nonlocal attempts
        attempts += 1
        assert attempts <= 3
        recorder._seq += 1
        recorder._attempt_media_seconds = 0.1
        recorder._remember_stderr(stderr.encode())
        return 1

    async def wait(seconds: float) -> None:
        if seconds >= settings.room_metadata_refresh_interval_s:
            await recorder._stop.wait()
            return
        waits.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 3)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 0)
    monkeypatch.setattr(recorder, "_record_once", media)
    monkeypatch.setattr(recorder, "_sleep_or_stop", wait)
    await recorder.run()
    assert attempts == runtime.stream_calls == 3 and len(waits) == 2
    assert 0.8 <= waits[0] <= 1.2 and 1.6 <= waits[1] <= 2.4
    assert recorder.retry_budget_exhausted and not recorder.recording_action_required
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "error" and session.ended_at is not None


@pytest.mark.parametrize("manual_stop", [True, False])
async def test_manual_stop_and_clean_eof_do_not_report_recording_failure(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, manual_stop: bool
) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    messages: list[str] = []
    attempts = 0

    async def media(stream: StreamSpec, directory: Path) -> int:
        nonlocal attempts
        attempts += 1
        recorder._remember_stderr(b"Immediate exit requested\n")
        if manual_stop:
            recorder.stop()
        return 255 if manual_stop else 0

    async def wait(seconds: float) -> None:
        if seconds >= settings.room_metadata_refresh_interval_s:
            await recorder._stop.wait()
            return
        recorder.stop()

    monkeypatch.setattr(recorder, "_record_once", media)
    monkeypatch.setattr(recorder, "_sleep_or_stop", wait)
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        await recorder.run()
    finally:
        logger.remove(sink)
    assert attempts == 1 and not recorder.retry_budget_exhausted
    assert not any("录制异常" in message or "录制中断" in message for message in messages)
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "stopped" and not session.error_message


async def test_stderr_is_drained_with_bounded_redacted_tail_and_final_unterminated_line(
    runtime: RuntimeSource,
) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id)

    class Process:
        stderr = asyncio.StreamReader(limit=1024)

    process = Process()
    process.stderr.feed_data(b"Cookie: " + b"private-long-secret" * 100000 + b"\n")
    for _ in range(100):
        process.stderr.feed_data(b"HTTP error 403 https://u:p@cdn.invalid/signed-path?signature=private-sig\n")
    process.stderr.feed_data(b"Authorization: private-auth\nCookie: private-cookie\n")
    process.stderr.feed_data(b"Permission denied: https://proxy-user:proxy-pass@proxy.invalid/secret")
    process.stderr.feed_eof()
    await asyncio.wait_for(recorder._drain_stderr(process), 2)
    tail = "\n".join(recorder._stderr_tail)
    assert len(recorder._stderr_tail) <= 20 and len(tail) <= 20 * 513
    assert "Permission denied" in tail
    assert all(secret not in tail for secret in ("private-", "proxy-pass", "proxy-user", "u:p@", "signed-path"))
    assert recorder._classify_recording_exit(1, recorder._stderr_tail).name == "PERMISSION_DENIED"


def test_disabling_both_recording_budgets_still_has_a_finite_safety_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 0)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 0)
    assert Recorder._reconnect_limits() == (20, 0)


@pytest.mark.parametrize("exit_race", [False, True])
async def test_stderr_read_failure_terminates_process_and_finishes_session(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, exit_race: bool
) -> None:
    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)

    class Process(ExternalProcess):
        stdin = None
        returncode: int | None = None

        def __init__(self) -> None:
            self.stderr = asyncio.StreamReader()
            self.stderr.set_exception(OSError(errno.EIO, "broken stderr pipe"))
            self.exited = asyncio.Event()

        async def wait(self) -> int:
            await self.exited.wait()
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            self.returncode = 1
            self.exited.set()
            if exit_race:
                raise ProcessLookupError("already exited")

    process = Process()

    async def spawn(*args: object, **kwargs: object) -> Process:
        return process

    install_process_boundary(monkeypatch, spawn)
    tasks_before = asyncio.all_tasks()
    with pytest.raises(OSError, match="broken stderr pipe"):
        await asyncio.wait_for(recorder.run(), 2)
    assert process.exited.is_set() and recorder.recording_action_required
    assert recorder._active_process is None and not recorder._diagnostic_secrets
    assert not (asyncio.all_tasks() - tasks_before)
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session.status == "error" and session.ended_at is not None


async def test_source_retry_after_is_clamped_to_remaining_recording_budget(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.recording import recorder as recorder_module

    room = await register_room("123", True, "external")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    runtime.stream_failure = SourceTemporaryError("slow upstream", retry_after=3600)
    now = 100.0
    waits: list[float] = []

    async def wait(seconds: float) -> None:
        nonlocal now
        if seconds == settings.room_metadata_refresh_interval_s:
            await recorder._stop.wait()
            return
        waits.append(seconds)
        now += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(recorder_module, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 10)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 0)
    monkeypatch.setattr(recorder, "_sleep_or_stop", wait)
    await recorder.run()
    assert waits == [10.0] and recorder.retry_budget_exhausted
    assert runtime.stream_calls == 1


async def test_monitor_keeps_unknown_and_failures_and_polls_healthy_platform_again(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.pipeline.live_monitor import LiveMonitor
    from app.web.services import rooms as rooms_service
    from app.web.services.rooms import RecorderManager

    other = RuntimeSource("healthy")
    source_registry.register_many("healthy-plugin", [other])
    rooms = [await register_room("123", True, platform) for platform in ("external", "healthy")]
    with get_session() as db:
        for room in rooms:
            room.auto_record = True
            db.add(room)
    manager = RecorderManager()
    monkeypatch.setattr(rooms_service, "recorder_manager", manager)
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
    from click.testing import CliRunner

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
    from app.web import main
    from app.web.services import rooms

    room = await register_room("123", True, "external")
    manager = rooms.RecorderManager()
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
            from app.core.paths import storage_root
            from app.recording import intents

            with SessionWriter.create(storage_root() / "writers") as writer:
                intent = intents.accept(db, room.id, room_source(room), pipeline=False, produce=False, origin="manual")
                intents.bind_session(db, 1, room.id, intent.generation, writer.identity)
    try:
        if trigger == "schedule":
            await main._run_due_schedules()
        else:
            await rooms.settle_interrupted_recordings()
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
