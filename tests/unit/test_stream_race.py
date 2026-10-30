from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
import threading
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from app.core.async_cleanup import complete_cleanup
from app.core.ffmpeg_errors import FfmpegErrorType
from app.core.process_control import ProcessCancelledError
from app.plugins.live_source import StreamSpec
from app.recording.stream_race import StreamAttempt, StreamRace


@pytest.mark.parametrize("evidence", ["video", "audio_only", "single_frame", "invalid_json", "timestamp_na"])
async def test_progress_without_frame_requires_actual_video_packets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    (tmp_path / "unique_00000.ts").write_bytes(b"local candidate")
    (tmp_path / "unique_00001.ts").touch()  # 分段切换后的空末片不能遮住已落盘的视频。
    process = FakeProcess(1)
    attempt.process = process

    def probe(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[-1] == str(tmp_path / "unique_00000.ts")
        assert command[command.index("-protocol_whitelist") + 1] == "file"
        assert kwargs["timeout"] == 2.0
        data = {
            "streams": [{"codec_type": "audio" if evidence == "audio_only" else "video"}],
            "packets": [{"dts_time": "1.0"}, {"dts_time": "1.6"}],
        }
        if evidence == "single_frame":
            data["packets"] = [{"dts_time": "1.0"}]
        if evidence == "timestamp_na":
            data["packets"] = [{"dts_time": "N/A"}, {"dts_time": "nan"}]
        return subprocess.CompletedProcess(command, 0, "{" if evidence == "invalid_json" else json.dumps(data))

    monkeypatch.setattr("app.recording.stream_race.run_cancellable", probe)
    process.stdout.feed_data(b"out_time_us=1000000\nprogress=continue\nout_time_us=1600000\nprogress=continue\n")
    process.stdout.feed_eof()
    await attempt.drain_progress()
    assert attempt.ready.is_set() is (evidence == "video")
    process.kill()


async def test_missing_frame_without_own_output_cannot_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    (tmp_path / "other_00000.ts").write_bytes(b"not this candidate")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("不能读取其他候选或历史录制文件")

    monkeypatch.setattr("app.recording.stream_race.run_cancellable", forbidden)
    assert not await attempt._has_video_packets()


async def test_local_video_probe_is_reaped_before_cancellation_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    (tmp_path / "unique_00000.ts").write_bytes(b"local candidate")
    entered, exited = threading.Event(), threading.Event()

    def probe(command: list[str], *, cancel_check, **kwargs: object) -> None:
        entered.set()
        while not cancel_check():
            exited.wait(0.01)
        exited.set()
        raise ProcessCancelledError("test cancellation")

    monkeypatch.setattr("app.recording.stream_race.run_cancellable", probe)
    task = asyncio.create_task(attempt._has_video_packets())
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()


class FakeProcess:
    registry: dict[int, FakeProcess] = {}

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.registry[pid] = self
        self.loop = asyncio.get_running_loop()
        self.stdin = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.exited = asyncio.Event()

    async def wait(self) -> int:
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        output, error = await asyncio.gather(self.stdout.read(), self.stderr.read())
        await self.wait()
        return output, error

    def kill(self) -> None:
        if self.returncode is None:
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

    def terminate(self) -> None:
        self.kill()

    def progress(self, *, frames: int = 20, media_us: int = 1_000_000) -> None:
        self.stdout.feed_data(
            f"frame={frames}\nout_time_us=0\nprogress=continue\n"
            f"frame={frames + 1 if frames else 0}\nout_time_us={media_us}\nprogress=continue\n".encode()
        )


@pytest.fixture(autouse=True)
def external_process_ownership(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.analysis.transcription import process_tree

    class ExternalTree:
        def __init__(self, pid: int) -> None:
            self.process = FakeProcess.registry[pid]
            self.parent_pipe: int | None = None

        def own_parent_pipe(self, descriptor: int) -> None:
            self.parent_pipe = descriptor

        def resume_suspended(self) -> None:
            pass

        def terminate(self) -> None:
            self.process.loop.call_soon_threadsafe(self.process.kill)
            if self.parent_pipe is not None:
                os.close(self.parent_pipe)
                self.parent_pipe = None

    FakeProcess.registry.clear()
    monkeypatch.setattr(process_tree, "ProcessTree", ExternalTree)
    yield
    FakeProcess.registry.clear()


def install_process_boundary(monkeypatch: pytest.MonkeyPatch, spawn: Callable[..., Awaitable[FakeProcess]]) -> None:
    async def external_spawn(*args: str, **kwargs: object) -> FakeProcess:
        if os.name != "nt":
            process = await spawn(*args[8:], **kwargs)
            os.write(kwargs["pass_fds"][-1], b'{"kind":"ready","guardian":987654321}\n')
            return process
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", external_spawn)


def streams(*names: str) -> list[StreamSpec]:
    return [
        StreamSpec(url=f"https://{name}.invalid/signed?token=private", transport="flv", container="flv")
        for name in names
    ]


def command(stream: StreamSpec, directory: Path, prefix: str, manifest: Path) -> list[str]:
    return ["ffmpeg", "-i", stream.url, "-segment_list", str(manifest), str(directory / f"{prefix}%05d.ts")]


def race_for(tmp_path: Path, candidates: list[StreamSpec]) -> StreamRace:
    return StreamRace(
        candidates, tmp_path, 0, command, asyncio.Event(), lambda _: None, parallel=2, timeout_s=0.03, budget_s=2
    )


@pytest.mark.parametrize("discard", [False, True])
async def test_cleanup_removes_only_exact_owned_files(tmp_path: Path, discard: bool) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "part007_abc123_")
    media = tmp_path / f"{attempt.prefix}00000.ts"
    large_index = tmp_path / f"{attempt.prefix}100000.ts"
    unrelated = [tmp_path / name for name in ("other_00000.ts", f"{attempt.prefix}notes.ts", "segments.csv")]
    directory = tmp_path / f"{attempt.prefix}00001.ts"
    directory.mkdir()
    (directory / "keep.txt").write_text("user data")
    for path in [media, large_index, attempt.segment_list, *unrelated]:
        path.write_bytes(b"keep unless owned")
    await attempt.cleanup_files(discard=discard)
    await attempt.cleanup_files(discard=discard)  # 重复收尾幂等。
    assert not attempt.segment_list.exists()
    assert media.exists() is not discard and large_index.exists() is not discard
    assert all(path.read_bytes() == b"keep unless owned" for path in unrelated)
    assert (directory / "keep.txt").read_text() == "user data"


@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_cleanup_retries_busy_files_without_losing_other_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: int
) -> None:
    from loguru import logger

    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    media = tmp_path / "unique_00000.ts"
    media.write_bytes(b"candidate")
    attempt.segment_list.write_text("manifest")
    original_unlink = Path.unlink
    calls = 0
    messages: list[str] = []

    def busy(path: Path, missing_ok: bool = False) -> None:
        nonlocal calls
        if path == media:
            calls += 1
            if calls <= failures:
                raise PermissionError(errno.EACCES, "sharing violation")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", busy)
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        await attempt.cleanup_files(discard=True)
    finally:
        logger.remove(sink)
    assert calls == min(failures + 1, 3)
    assert media.exists() is (failures == 3)
    assert not attempt.segment_list.exists()
    assert any(media.name in message and "errno=13" in message for message in messages) is (failures == 3)
    if failures == 3:
        await attempt.cleanup_files(discard=True)
        assert not media.exists()  # 下一次收尾会再次尝试，没有被标成已清理。


async def test_cleanup_refuses_running_process(tmp_path: Path) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    attempt.process = FakeProcess(1)
    attempt.segment_list.write_text("still writing")
    with pytest.raises(RuntimeError, match="仍在写入"):
        await attempt.cleanup_files(discard=True)
    assert attempt.segment_list.read_text() == "still writing"
    await attempt.close(discard=True)
    assert not attempt.segment_list.exists()


async def test_cleanup_keeps_link_and_redirected_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    media = tmp_path / "unique_00000.ts"
    media.write_bytes(b"link target")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == media or original_is_symlink(path))
    await attempt.cleanup_files(discard=True)
    assert media.read_bytes() == b"link target"
    attempt.segment_list.write_text("redirect target")
    original_resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, *args, **kwargs: (
            tmp_path / "redirect" if path == tmp_path else original_resolve(path, *args, **kwargs)
        ),
    )
    await attempt.cleanup_files(discard=False)
    assert attempt.segment_list.read_text() == "redirect target"


async def test_repeated_cancellation_waits_for_candidate_file_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    media = tmp_path / "unique_00000.ts"
    media.write_bytes(b"candidate")
    entered, release = threading.Event(), threading.Event()
    original_unlink = Path.unlink

    def delayed(path: Path, missing_ok: bool = False) -> None:
        if path == media:
            entered.set()
            assert release.wait(5), "cleanup not released"
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", delayed)
    task = asyncio.create_task(complete_cleanup(attempt.close(discard=True)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and media.exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not media.exists()


async def test_winner_process_is_reused_and_only_loser_files_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[FakeProcess] = []

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        proc = FakeProcess(len(processes) + 1)
        processes.append(proc)
        output = Path(args[-1].replace("%05d", "00000"))
        output.write_bytes(b"candidate media")
        Path(args[args.index("-segment_list") + 1]).write_text(f"{output.name},0,3\n")
        if urlsplit(args[args.index("-i") + 1]).hostname == "good.invalid":
            proc.progress()
        return proc

    install_process_boundary(monkeypatch, spawn)
    before = asyncio.all_tasks()
    race = race_for(tmp_path, streams("slow", "good", "unused"))
    existing = tmp_path / "part_old.ts"
    existing.write_bytes(b"existing recording")
    try:
        winner = await race.run()
        assert winner is race.attempts[1] and winner.process is processes[1]
        assert len(processes) == 2 and processes[0].returncode is not None
        assert processes[1].returncode is None
        assert not list(tmp_path.glob(f"{race.attempts[0].prefix}*"))
        assert list(tmp_path.glob(f"{winner.prefix}*.ts"))
        assert winner.progress_task is not None and not winner.progress_task.done()
    finally:
        await race.close()
    assert all(proc.returncode is not None for proc in processes)
    assert not (asyncio.all_tasks() - before)
    assert existing.read_bytes() == b"existing recording"
    assert list(tmp_path.glob(f"{race.winner.prefix}*.ts"))


async def test_timeout_and_invalid_progress_refill_slots_with_bounded_concurrency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[FakeProcess] = []
    peak = 0

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        nonlocal peak
        proc = FakeProcess(len(processes))
        processes.append(proc)
        peak = max(peak, sum(item.returncode is None for item in processes))
        host = urlsplit(args[args.index("-i") + 1]).hostname
        proc.progress(frames=0 if host == "audio.invalid" else 1, media_us=0 if host == "empty.invalid" else 1_000_000)
        return proc

    install_process_boundary(monkeypatch, spawn)
    race = race_for(tmp_path, streams("audio", "empty", "good"))
    try:
        winner = await race.run()
        assert winner is race.attempts[2]
        assert peak <= 2 and len(race.failures) == 2
        assert all(FfmpegErrorType.TRANSIENT_NETWORK in attempt.categories for attempt in race.failures)
    finally:
        await race.close()


@pytest.mark.parametrize("cancel", [False, True])
async def test_stop_or_cancel_during_process_creation_reaps_late_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    processes: list[FakeProcess] = []

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        entered.set()
        await release.wait()
        proc = FakeProcess(len(processes))
        processes.append(proc)
        return proc

    install_process_boundary(monkeypatch, spawn)
    before = asyncio.all_tasks()
    race = race_for(tmp_path, streams("slow", "slow2"))
    task = asyncio.create_task(race.run())
    await entered.wait()
    if cancel:
        task.cancel()
    else:
        race.stop.set()
    await asyncio.sleep(0)
    if cancel:
        task.cancel()  # 收尾期间的再次取消也不能遗留正在创建的进程。
    release.set()
    try:
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
    finally:
        await race.close()
    assert processes and all(proc.returncode is not None for proc in processes)
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("failure", ["permission", "pipe", "all_timeout", "budget"])
async def test_failure_paths_reap_all_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    processes: list[FakeProcess] = []

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        if failure == "permission":
            raise OSError(errno.EACCES, "private")
        proc = FakeProcess(len(processes))
        processes.append(proc)
        if failure == "pipe":
            proc.stderr.set_exception(OSError(errno.EIO, "private"))
        return proc

    install_process_boundary(monkeypatch, spawn)
    before = asyncio.all_tasks()
    race = race_for(tmp_path, streams("a", "b", "c"))
    if failure == "budget":
        race.timeout_s, race.budget_s = 1, 0.02
    try:
        assert await race.run() is None
        assert race.failures
        if failure == "permission":
            assert FfmpegErrorType.PERMISSION_DENIED in race.failures[0].categories
    finally:
        await race.close()
    assert all(proc.returncode is not None for proc in processes)
    assert not (asyncio.all_tasks() - before)
    assert all("private" not in " ".join(attempt.tail) for attempt in race.attempts)


async def test_diagnostics_are_bounded_and_redacted(tmp_path: Path) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    proc = FakeProcess(1)
    attempt.process = proc
    proc.stderr.feed_data(b"Cookie: private-cookie" * 10000 + b"\n")
    for _ in range(50):
        proc.stderr.feed_data(b"HTTP error 403 https://a.invalid/private?token=private\n")
    proc.stderr.feed_data(b"[tls @ 001] Unable to read from socket")
    proc.stderr.feed_eof()
    await attempt.drain_stderr()
    assert len(attempt.tail) == 20 and all(len(line) <= 512 for line in attempt.tail)
    assert "private" not in " ".join(attempt.tail)
    assert attempt.http_status == 403
    assert FfmpegErrorType.TRANSIENT_NETWORK in attempt.categories
    proc.kill()


@pytest.mark.parametrize("expired", [False, True])
async def test_quality_tiers_and_expired_queued_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    candidates = streams("high", "low")
    candidates[0] = candidates[0].model_copy(update={"quality_id": "10000"})
    candidates[1] = candidates[1].model_copy(update={"quality_id": "400"})
    if expired:
        candidates[0] = candidates[0].model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    launched = []

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        launched.append(urlsplit(args[args.index("-i") + 1]).hostname)
        proc = FakeProcess(len(launched))
        proc.progress()
        return proc

    install_process_boundary(monkeypatch, spawn)
    race = race_for(tmp_path, candidates)
    try:
        winner = await race.run()
        assert winner is race.attempts[1 if expired else 0]
        assert launched == ["low.invalid" if expired else "high.invalid"]
    finally:
        await race.close()


async def test_zero_budget_or_already_stopped_never_spawns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("停止或预算已耗尽后不能启动拉流进程")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    for stopped in (False, True):
        race = race_for(tmp_path, streams("a", "b"))
        if stopped:
            race.stop.set()
        else:
            race.budget_s = 0
        assert await race.run() is None
        await race.close()


async def test_ready_requires_relative_media_and_video_progress(tmp_path: Path) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    proc = FakeProcess(1)
    attempt.process = proc
    reader = asyncio.create_task(attempt.drain_progress())

    async def sample(frames: int, media_us: int) -> None:
        proc.stdout.feed_data(f"frame={frames}\nout_time_us={media_us}\nprogress=continue\n".encode())
        await asyncio.sleep(0)

    try:
        await sample(1, 3_600_000_000)  # 大绝对时间戳不是成功出流证据。
        assert not attempt.ready.is_set()
        await sample(1, 3_601_000_000)  # 仅音频推进，视频没有继续输出。
        assert not attempt.ready.is_set()
        await sample(2, 10)  # 时间戳回退，重新建立基线。
        assert not attempt.ready.is_set()
        await sample(3, 400_010)
        assert not attempt.ready.is_set()
        await sample(4, 500_010)
        assert attempt.ready.is_set()
    finally:
        proc.kill()
        await reader


@pytest.mark.parametrize("failure", ["invalid_input", "http_permission"])
async def test_same_host_different_paths_are_tried_and_input_failure_is_not_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    first = streams("cdn")[0]
    second = first.model_copy(update={"url": "https://cdn.invalid/other-path?token=private"})
    launched = []

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        url = args[args.index("-i") + 1]
        launched.append(url)
        proc = FakeProcess(len(launched))
        if url == first.url:
            proc.stderr.feed_data(
                b"Error opening input: Invalid argument\n"
                if failure == "invalid_input"
                else b"HTTP error 403\nError opening input: Permission denied\n"
            )
            proc.kill()
            proc.returncode = 1
        else:
            proc.progress()
        return proc

    install_process_boundary(monkeypatch, spawn)
    race = race_for(tmp_path, [first, first, second])
    try:
        winner = await race.run()
        assert len(race.attempts) == 2 and winner.stream is second
        assert launched == [first.url, second.url]
        assert len(race.failures) == 1 and not race.failures[0].global_error
    finally:
        await race.close()


@pytest.mark.parametrize("message", [b"No space left on device", b"Error opening output file x: Permission denied"])
async def test_real_output_fault_is_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: bytes) -> None:
    launched = []

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        launched.append(args)
        proc = FakeProcess(1)
        proc.stderr.feed_data(message + b"\n")
        proc.kill()
        return proc

    install_process_boundary(monkeypatch, spawn)
    race = race_for(tmp_path, streams("a", "b"))
    try:
        assert await race.run() is None
        assert len(launched) == 1 and race.failures[0].global_error
    finally:
        await race.close()


@pytest.mark.parametrize("first_healthy", [False, True])
async def test_staggered_race_avoids_extra_process_for_fast_primary_but_hedges_stalled_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_healthy: bool
) -> None:
    processes: list[FakeProcess] = []
    clock_s = 0.0

    def advance_to_hedge() -> None:
        nonlocal clock_s
        assert len(processes) == 1, "错峰期限前不能启动备用线路"
        clock_s = race.launch_delay_s

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        if processes:
            assert processes[0].returncode is None, "备用线路应在首选仍等待时启动，无需等到超时"
            assert clock_s == race.launch_delay_s, "备用线路应在错峰期限到达后启动"
        proc = FakeProcess(len(processes))
        processes.append(proc)
        if first_healthy or len(processes) == 2:
            proc.progress()
        else:
            asyncio.get_running_loop().call_soon(advance_to_hedge)
        return proc

    install_process_boundary(monkeypatch, spawn)
    # 仅控制被测调度器的时钟；事件循环仍正常运行，不要求 CI 主机在 10ms 内完成调度。
    monkeypatch.setattr("app.recording.stream_race.time", SimpleNamespace(monotonic=lambda: clock_s))
    race = race_for(tmp_path, streams("a", "b", "c"))
    race.timeout_s, race.launch_delay_s = 1, 0.01
    try:
        assert await race.run() is not None
        assert len(processes) == (1 if first_healthy else 2)
    finally:
        await race.close()


@pytest.mark.parametrize("output_fault", [False, True])
async def test_winner_that_fails_during_loser_cleanup_is_not_handed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_fault: bool
) -> None:
    processes = []
    race = race_for(tmp_path, streams("slow", "good"))
    race.timeout_s, race.launch_delay_s = 1, 0.01

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        proc = FakeProcess(len(processes))
        processes.append(proc)
        if len(processes) == 2:
            proc.progress()
        return proc

    original_close = StreamAttempt.close

    async def close(attempt: StreamAttempt, *, discard: bool) -> None:
        if race.winner is not None and attempt is not race.winner and discard:
            if output_fault:
                race.winner.remember(b"Error writing output: No space left on device")
            else:
                race.winner.process.kill()
        await original_close(attempt, discard=discard)

    install_process_boundary(monkeypatch, spawn)
    monkeypatch.setattr(StreamAttempt, "close", close)
    try:
        assert await race.run() is None
        assert race.winner is None and race.failures
        assert all(process.returncode is not None for process in processes)
    finally:
        await race.close()


@pytest.mark.parametrize("style", ["old_ms", "formatted", "na_us", "prefer_us"])
async def test_legacy_progress_fields_keep_microsecond_units(tmp_path: Path, style: str) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    process = FakeProcess(1)
    attempt.process = process
    reader = asyncio.create_task(attempt.drain_progress())

    async def sample(frame: int, microseconds: int) -> None:
        formatted = f"00:00:00.{microseconds:06d}"
        if style == "old_ms":
            fields = f"out_time_ms={microseconds}"
        elif style == "formatted":
            fields = f"out_time={formatted}"
        elif style == "na_us":
            fields = f"out_time_us=N/A\nout_time_ms={microseconds}\nout_time={formatted}"
        else:
            fields = f"out_time_ms={microseconds * 1000}\nout_time_us={microseconds}\nout_time={formatted}"
        process.stdout.feed_data(f"frame={frame}\n{fields}\nprogress=continue\n".encode())
        await asyncio.sleep(0)

    try:
        await sample(1, 1000)
        await sample(2, 1500)  # 旧版 ms 名称容易被误当毫秒；这里只推进了 500 微秒。
        assert not attempt.ready.is_set()
        await sample(3, 501000)
        assert attempt.ready.is_set()
    finally:
        process.kill()
        await reader


async def test_legacy_ffmpeg_works_without_new_optional_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes = []

    async def spawn(*args: str, **kwargs: object) -> FakeProcess:
        assert "-stats_period" not in args
        assert args[args.index("-progress") + 1] == "pipe:1"
        process = FakeProcess(len(processes))
        processes.append(process)
        process.stdout.feed_data(
            b"frame=1\nout_time_ms=0\nprogress=continue\nframe=20\nout_time_ms=1000000\nprogress=continue\n"
        )
        return process

    install_process_boundary(monkeypatch, spawn)
    race = race_for(tmp_path, streams("a", "b"))
    try:
        winner = await race.run()
        assert winner.process is processes[0] and len(processes) == 1
    finally:
        await race.close()


@pytest.mark.parametrize(
    "message,unsupported",
    [
        (b"Unrecognized option 'progress'.", True),
        (b"Option progress not found.", True),
        (b"Unrecognized option 'other'.", False),
        (b"Failed to open progress URL pipe:1: Permission denied", False),
    ],
)
def test_progress_capability_failure_is_precise(tmp_path: Path, message: bytes, unsupported: bool) -> None:
    attempt = StreamAttempt(streams("a")[0], tmp_path, "unique_")
    attempt.remember(message)
    assert attempt.progress_unsupported is unsupported


@pytest.mark.parametrize("resource_errno", [errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EAGAIN])
async def test_local_resource_errors_stop_competition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_errno: int
) -> None:
    calls = 0

    async def spawn(*args: str, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise OSError(resource_errno, "private details")

    install_process_boundary(monkeypatch, spawn)
    race = race_for(tmp_path, streams("a", "b"))
    try:
        assert await race.run() is None
        assert calls == 1 and race.failures[0].resource_errno == resource_errno
        assert race.failures[0].global_error
        assert "private" not in " ".join(race.failures[0].tail)
        assert errno.errorcode[resource_errno] in " ".join(race.failures[0].tail)
    finally:
        await race.close()
