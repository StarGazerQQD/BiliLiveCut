from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from loguru import logger

from app.core.config import settings
from app.db.entities import RawSegment, RecordingSession
from app.db.session import get_session
from app.recording.recorder import Recorder
from app.sources.registry import source_registry
from app.sources.rooms import register_room, room_source
from tests.integration.test_source_runtime import RuntimeSource
from tests.integration.test_source_runtime import runtime as runtime

_ACTOR = r"""
import csv
import os
import sys
import threading
from pathlib import Path

mode, count, listing, output, media = sys.argv[1:]
if mode in {"stable", "orphan"} and count == "1":
    print("Connection timed out", file=sys.stderr, flush=True)
    sys.exit(1)
path = Path(output.replace("%05d", "00000"))
payload = Path(media).read_bytes()
if mode == "orphan":
    path.write_bytes(payload)
    sys.exit(1)
if mode == "assertion":
    print("Assertion packet->size >= 0 failed", file=sys.stderr, flush=True)
elif mode == "http":
    print("Server returned 503 Service Unavailable", file=sys.stderr, flush=True)
stopping = threading.Event()
threading.Thread(target=lambda: (sys.stdin.buffer.readline(), stopping.set()), daemon=True).start()
if mode in {"touch", "stable"}:
    path.write_bytes(payload)
if mode == "stable":
    with Path(listing).open("w", newline="", encoding="utf-8") as stream:
        csv.writer(stream).writerow([path.name, 0, 1])
while not stopping.wait(0.05):
    if mode == "touch":
        os.utime(path, None)
        Path(listing).touch()
    elif mode in {"grow", "other"}:
        target = path if mode == "grow" else path.with_name("another_attempt.ts")
        with target.open("ab") as stream:
            stream.write(payload)
"""


@pytest.fixture
def video(tmp_path: Path) -> Path:
    target = tmp_path / "one-second.ts"
    subprocess.run(
        [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "10",
            "-f",
            "mpegts",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=20,
    )
    return target


def _process_boundary(monkeypatch: pytest.MonkeyPatch, video: Path, mode: str) -> list[asyncio.subprocess.Process]:
    original = asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def launch(*args: str, **options: object) -> asyncio.subprocess.Process:
        listing = args[args.index("-segment_list") + 1]
        assert f"token=secret-{len(processes) + 1}" in args[args.index("-i") + 1]
        command = [sys.executable, "-c", _ACTOR, mode, str(len(processes) + 1), listing, args[-1], str(video)]
        if os.name != "nt":
            # 保留真实 POSIX 父死亡监管与 exec 握手，仅替换外部 FFmpeg。
            command = [*args[:7], sys.executable, *command]
        process = await original(*command, **options)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    return processes


@pytest.mark.parametrize(
    "mode,attempts,elapsed,expected",
    [
        ("empty", 2, 0, 2),
        ("touch", 2, 0, 2),
        ("other", 2, 0, 2),
        ("empty", 0, 0.5, 1),
    ],
)
async def test_live_process_without_own_media_growth_exhausts_finite_retries(
    runtime: RuntimeSource,
    monkeypatch: pytest.MonkeyPatch,
    video: Path,
    mode: str,
    attempts: int,
    elapsed: float,
    expected: int,
) -> None:
    room = await register_room("123", True, "external")
    processes = _process_boundary(monkeypatch, video, mode)
    monkeypatch.setattr(settings, "recording_stream_race_timeout_s", 1.0)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", attempts)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", elapsed)
    monkeypatch.setattr(settings, "reconnect_max_backoff_s", 0.01)
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    await asyncio.wait_for(recorder.run(), 15)
    assert len(processes) == runtime.stream_calls == expected
    assert all(process.returncode is not None for process in processes)
    assert recorder.retry_budget_exhausted and not recorder.recording_action_required
    assert "预算耗尽" in recorder.recording_error
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session is not None and session.status == "error" and session.ended_at is not None


async def test_exited_process_final_segment_does_not_report_reconnected(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, video: Path
) -> None:
    room = await register_room("123", True, "external")
    processes = _process_boundary(monkeypatch, video, "orphan")
    monkeypatch.setattr(settings, "segment_duration_s", 1)
    monkeypatch.setattr(settings, "reconnect_max_backoff_s", 0.01)
    observed: list[str] = []

    async def segment_ready(_segment: RawSegment) -> None:
        assert processes[-1].returncode is not None
        assert recorder._attempt_stable
        with get_session() as db:
            session = db.get(RecordingSession, recorder.session_id)
            assert session is not None and session.last_reconnected_at is None
            observed.append(session.status)
        recorder.stop()

    recorder = Recorder(room_source(room), room.id, on_segment=segment_ready, metadata_prepared=True)
    await asyncio.wait_for(recorder.run(), 10)
    assert len(processes) == runtime.stream_calls == 2
    assert observed == ["reconnecting"]
    assert all(process.returncode is not None for process in processes)


@pytest.mark.parametrize("mode,category", [("assertion", "PROCESS_CRASH"), ("http", "UPSTREAM_UNAVAILABLE")])
async def test_media_watchdog_keeps_specific_process_failure(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, video: Path, mode: str, category: str
) -> None:
    room = await register_room("123", True, "external")
    processes = _process_boundary(monkeypatch, video, mode)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 1)
    monkeypatch.setattr(settings, "recording_stream_race_timeout_s", 0.8)
    messages: list[str] = []
    sink = logger.add(messages.append, format="{message}")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    try:
        await asyncio.wait_for(recorder.run(), 10)
    finally:
        logger.remove(sink)
    assert len(processes) == runtime.stream_calls == 1 and processes[0].returncode is not None
    assert recorder._media_stalled and recorder.retry_budget_exhausted
    summaries = [message for message in messages if "录制异常 room=" in message]
    assert len(summaries) == 1 and f"category={category}" in summaries[0]


async def test_repeated_cancellation_reaps_live_media_guard_and_process(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, video: Path
) -> None:
    room = await register_room("123", True, "external")
    processes = _process_boundary(monkeypatch, video, "grow")
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    existing = asyncio.all_tasks()
    task = asyncio.create_task(recorder.run())
    try:
        async with asyncio.timeout(3):
            while not processes or recorder._active_process is None:
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 12)
        assert processes[0].returncode is not None and recorder._active_process is None
        assert not source_registry._entries["external"].users
        assert not [pending for pending in asyncio.all_tasks() - existing if not pending.done()]
    finally:
        recorder.stop()
        await asyncio.gather(task, return_exceptions=True)


async def test_healthy_long_segment_can_outlive_failure_budget(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, video: Path
) -> None:
    room = await register_room("123", True, "external")
    processes = _process_boundary(monkeypatch, video, "grow")
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 0.1)
    monkeypatch.setattr(settings, "recording_stream_race_timeout_s", 0.8)
    recorder = Recorder(room_source(room), room.id, metadata_prepared=True)
    task = asyncio.create_task(recorder.run())
    try:
        async with asyncio.timeout(3):
            while not processes:
                await asyncio.sleep(0.01)
        await asyncio.sleep(1.2)
        assert not task.done() and processes[0].returncode is None
        assert not recorder.retry_budget_exhausted and runtime.stream_calls == 1
    finally:
        recorder.stop()
        await asyncio.wait_for(task, 12)
    assert processes[0].returncode == 0 and recorder.recording_error is None


async def test_stable_segment_updates_recovery_while_process_remains_alive(
    runtime: RuntimeSource, monkeypatch: pytest.MonkeyPatch, video: Path
) -> None:
    room = await register_room("123", True, "external")
    processes = _process_boundary(monkeypatch, video, "stable")
    monkeypatch.setattr(settings, "segment_duration_s", 1)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 2)
    monkeypatch.setattr(settings, "recording_stream_race_timeout_s", 3)
    monkeypatch.setattr(settings, "reconnect_max_backoff_s", 0.01)
    registered = asyncio.Event()

    async def segment_ready(_segment: RawSegment) -> None:
        registered.set()

    recorder = Recorder(room_source(room), room.id, on_segment=segment_ready, metadata_prepared=True)
    task = asyncio.create_task(recorder.run())
    try:
        await asyncio.wait_for(registered.wait(), 5)
        assert len(processes) == runtime.stream_calls == 2 and processes[-1].returncode is None
        with get_session() as db:
            session = db.get(RecordingSession, recorder.session_id)
            assert session is not None and session.status == "recording"
            assert session.last_reconnected_at is not None and session.ended_at is None
        assert recorder._reconnect_budget.started_at is None and recorder._reconnect_budget.failures == 0
    finally:
        recorder.stop()
        await asyncio.wait_for(task, 12)
    assert all(process.returncode is not None for process in processes)
