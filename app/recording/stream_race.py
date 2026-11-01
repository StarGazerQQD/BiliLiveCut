"""并发竞争真实 FFmpeg 录制连接，胜出进程直接交给录制器继续使用。"""

from __future__ import annotations

import asyncio
import errno
import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from app.analysis.transcription.process_tree import ProcessTree
from app.core.async_cleanup import complete_cleanup
from app.core.config import settings
from app.core.ffmpeg_errors import FfmpegErrorType, classify_ffmpeg_error
from app.core.process_control import ProcessCancelledError, run_cancellable
from app.core.process_spawn import spawn_owned_async, terminate_owned_async, wait_process_exit
from app.core.sanitize import sanitize_diagnostic
from app.plugins.live_source import StreamSpec
from app.recording.stream_failover import route_key
from app.recording.writer_ownership import SessionWriter

CommandBuilder = Callable[[StreamSpec, Path, str, Path], list[str]]
FailureCallback = Callable[[StreamSpec], None]
_RESOURCE_ERRNOS = {errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EAGAIN}


@dataclass
class StreamAttempt:
    """一条候选的独立输出、进程和有界诊断；不触碰数据库和流水线。"""

    stream: StreamSpec = field(repr=False)
    out_dir: Path
    prefix: str
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    tree: ProcessTree | None = field(default=None, repr=False)
    stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)
    progress_task: asyncio.Task[None] | None = field(default=None, repr=False)
    ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    tail: list[str] = field(default_factory=list)
    categories: set[FfmpegErrorType] = field(default_factory=set)
    http_status: int | None = None
    exit_code: int = 1
    global_error: bool = False
    progress_unsupported: bool = False
    last_progress_at: float | None = None
    resource_errno: int | None = None
    _output_error: bool = field(default=False, repr=False)
    _last_video_probe_at: float = field(default=float("-inf"), repr=False)
    _output_root: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """固定输出目录，清理时拒绝被重定向到其他目录的路径。"""
        self._output_root = self.out_dir.resolve()

    @property
    def segment_list(self) -> Path:
        """每个候选使用独立清单，落选文件不能被胜出者登记。"""
        return self.out_dir / f"{self.prefix}segments.csv"

    def remember(self, raw: bytes) -> None:
        """原文仅用于分类，持久诊断仅含脱敏后的有界尾部。"""
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        self.categories.add(classify_ffmpeg_error(1, text))
        lower = text.lower()
        self.progress_unsupported |= bool(
            re.search(
                r"\b(?:unrecognized option\s+['\"]?progress\b|option\s+['\"]?progress['\"]?\s+not found\b)", lower
            )
        )
        self._output_error |= any(
            marker in lower for marker in ("error opening output", "failed to open segment", "error writing output")
        )
        self.global_error |= (
            FfmpegErrorType.DISK_FULL in self.categories
            or (self._output_error and FfmpegErrorType.PERMISSION_DENIED in self.categories)
            or "unrecognized option" in lower
            or "option not found" in lower
        )
        status = re.search(r"(?:server returned|http error)\s+([45]\d\d)", text, re.IGNORECASE)
        if status:
            self.http_status = int(status[1])
        self.tail.append(sanitize_diagnostic(text, secrets=self.stream.headers.values(), limit=512))
        del self.tail[:-20]

    async def drain_stderr(self) -> None:
        """持续排空 stderr，超长行跳过，避免进程因管道填满而卡住。"""
        assert self.process is not None and self.process.stderr is not None
        pending = b""
        oversized = False
        while chunk := await self.process.stderr.read(4096):
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                self.remember(b"[oversized stderr line omitted]" if oversized or len(raw) > 8192 else raw)
                oversized = False
            if len(pending) > 8192:
                pending = b""
                oversized = True
        if pending or oversized:
            self.remember(b"[oversized stderr line omitted]" if oversized else pending)

    async def drain_progress(self) -> None:
        """观测实际视频进度；旧版缺少帧计数时核验本地视频包，胜出后继续排空。"""
        assert self.process is not None and self.process.stdout is not None
        frame = None
        times: dict[str, int] = {}
        baseline: tuple[int, int] | None = None
        legacy_baseline: int | None = None
        progress_high_water: int | None = None
        frame_high_water: int | None = None
        while raw := await self.process.stdout.readline():
            key, _, value = raw.decode("ascii", errors="replace").strip().partition("=")
            if key in {"frame", "out_time_us", "out_time_ms"}:
                try:
                    number = int(value)
                except ValueError:
                    continue
                if key == "frame":
                    frame = number
                else:
                    # FFmpeg 的历史 out_time_ms 字段实际也是微秒，不能再乘 1000。
                    times[key] = number
            elif key == "out_time":
                match = re.fullmatch(r"(-?)(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,6}))?", value)
                if match:
                    sign, hours, minutes, seconds, fraction = match.groups()
                    times[key] = (
                        (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1_000_000
                        + int((fraction or "").ljust(6, "0"))
                    ) * (-1 if sign else 1)
            elif key == "progress":
                media_us = next(
                    (times[name] for name in ("out_time_us", "out_time_ms", "out_time") if name in times), None
                )
                if media_us is not None and media_us >= 0 and (frame is None or frame > 0):
                    if (
                        progress_high_water is not None
                        and media_us > progress_high_water
                        and (frame is None or frame_high_water is not None and frame > frame_high_water)
                    ):
                        self.last_progress_at = time.monotonic()
                    progress_high_water = max(progress_high_water or 0, media_us)
                    if frame is not None:
                        frame_high_water = max(frame_high_water or 0, frame)
                if frame is not None and frame > 0 and media_us is not None:
                    if baseline is None or frame < baseline[0] or media_us < baseline[1]:
                        baseline = (frame, media_us)
                    elif frame > baseline[0] and media_us - baseline[1] >= 500_000:
                        self.ready.set()
                    legacy_baseline = None
                elif frame is None and media_us is not None and not self.ready.is_set():
                    # FFmpeg 6.1 在 -c copy 下没有 frame 字段。仅时间推进也可能只是音频，
                    # 必须额外确认当前候选已写出的 TS 含有持续至少半秒的视频包。
                    if legacy_baseline is None or media_us < legacy_baseline:
                        legacy_baseline = media_us
                    elif media_us - legacy_baseline >= 500_000 and await self._has_video_packets():
                        self.ready.set()
                frame = None
                times.clear()

    async def _has_video_packets(self) -> bool:
        """只探测当前候选的本地 TS，不重新访问 CDN；限制频率、包数和执行时间。"""
        now = time.monotonic()
        if now - self._last_video_probe_at < 1.0:
            return False
        self._last_video_probe_at = now
        paths = [
            path
            for path in self.out_dir.glob(f"{self.prefix}*.ts")
            if not path.is_symlink() and path.is_file() and path.stat().st_size > 0
        ]
        if not paths:
            return False
        # 当前末片可能刚建立且仍为空；优先检查本候选首片已经落盘的视频。
        path = min(paths, key=lambda item: item.name)
        cancelled = threading.Event()
        work = asyncio.create_task(
            asyncio.to_thread(
                run_cancellable,
                [
                    settings.ffprobe_path,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-f",
                    "mpegts",
                    "-probesize",
                    "524288",
                    "-analyzeduration",
                    "1000000",
                    "-read_intervals",
                    "%+#1024",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type:packet=dts_time,pts_time",
                    "-of",
                    "json",
                    str(path),
                ],
                cancel_check=cancelled.is_set,
                timeout=2.0,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        )
        try:
            result = await asyncio.shield(work)
            if result.returncode:
                return False  # 文件仍在写入，下一次报告再核验。
            data = json.loads(result.stdout)
            if not any(stream.get("codec_type") == "video" for stream in data.get("streams", [])):
                return False
            timestamps = []
            for packet in data.get("packets", []):
                value = packet.get("dts_time", packet.get("pts_time"))
                try:
                    stamp = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(stamp):
                    timestamps.append(stamp)
            return len(timestamps) >= 2 and max(timestamps) - min(timestamps) >= 0.5
        except (ProcessCancelledError, subprocess.TimeoutExpired, ValueError):
            return False
        finally:
            cancelled.set()

            async def finish() -> None:
                # 取消也等到探测进程退出，随后才能安全删除落选候选的文件。
                await asyncio.gather(work, return_exceptions=True)

            await complete_cleanup(finish())

    async def close(self, *, discard: bool) -> None:
        """先回收进程和管道，再逐个删除本候选的临时文件。"""
        proc = self.process
        try:
            if proc is not None:
                # 创建子进程期间被取消时，竞争协程可能还没来得及启动读取器。
                # 回收前也要排空管道，避免 wait() 被已填满的 stdout/stderr 卡住。
                if self.stderr_task is None:
                    self.stderr_task = asyncio.create_task(self.drain_stderr())
                if self.progress_task is None:
                    self.progress_task = asyncio.create_task(self.drain_progress())
                await terminate_owned_async(proc, self.tree)
        finally:
            readers = [task for task in (self.stderr_task, self.progress_task) if task is not None]
            if readers:
                try:
                    await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), 2)
                except TimeoutError:
                    for task in readers:
                        task.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
        if discard:
            await self.cleanup_files(discard=True)

    async def cleanup_files(self, *, discard: bool) -> None:
        """回收已停止候选的副产物；胜出者仅在完成末片登记后清理清单。"""
        if self.process is not None and self.process.returncode is None:
            raise RuntimeError("不能清理仍在写入的录制候选")
        # Windows 上杀毒扫描或刚关闭的文件句柄可能暂时阻止删除。
        # 重试有界，永久权限错误留存文件并记录，不拖住录制收尾。
        for retry in range(3):
            failures = await asyncio.to_thread(self._unlink_files, discard)
            if not failures:
                return
            if retry < 2:
                await asyncio.sleep(0.1 * (retry + 1))
        for name, error in failures:
            logger.warning(
                "录制候选临时文件清理失败 file={} errno={} winerror={}",
                name,
                error.errno,
                getattr(error, "winerror", None),
            )

    def _unlink_files(self, discard: bool) -> list[tuple[str, OSError]]:
        """只删除本候选的精确文件名，不递归、不跟随链接。"""
        failures: list[tuple[str, OSError]] = []
        try:
            if self.out_dir.resolve() != self._output_root:
                logger.warning("录制候选输出目录已重定向，拒绝清理 prefix={}", self.prefix)
                return failures
            pattern = re.compile(rf"{re.escape(self.prefix)}(?:[0-9]{{5,}}\.ts|segments\.csv)")
            paths = list(self.out_dir.iterdir()) if discard else [self.segment_list]
            for path in paths:
                if not pattern.fullmatch(path.name):
                    continue
                try:
                    if path.is_symlink() or path.parent.resolve() != self._output_root or not path.is_file():
                        continue
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    failures.append((path.name, exc))
        except FileNotFoundError:
            pass  # 未启动的候选或已被回收的目录没有输出。
        except OSError as exc:
            failures.append((self.out_dir.name, exc))
        return failures


class StreamRace:
    """有界并发启动候选；首个正常出流者胜出，其进程保持运行。"""

    def __init__(
        self,
        streams: list[StreamSpec],
        out_dir: Path,
        seq: int,
        build_command: CommandBuilder,
        stop: asyncio.Event,
        on_failure: FailureCallback,
        *,
        parallel: int,
        timeout_s: float,
        budget_s: float,
        launch_delay_s: float = 0.25,
        writer: SessionWriter | None = None,
    ) -> None:
        # 精确重复才去重。同主机不同路径或凭据可能对应不同的可用端点。
        seen = set()
        tiers: dict[str | None, list[StreamSpec]] = {}
        for stream in streams:
            key = (
                stream.url,
                stream.transport,
                stream.container,
                stream.codec,
                stream.quality_id,
                tuple(sorted((name.lower(), value) for name, value in stream.headers.items())),
                stream.expires_at,
            )
            if key not in seen:
                seen.add(key)
                tiers.setdefault(stream.quality_id, []).append(stream)
        ordered = []
        for tier in tiers.values():
            primary, variants = [], []
            families = set()
            for stream in tier:
                key = route_key(stream)[:5]
                (variants if key in families else primary).append(stream)
                families.add(key)
            ordered.extend(primary + variants)
        self.attempts = [
            StreamAttempt(stream, out_dir, f"part{seq:03d}_{uuid.uuid4().hex[:12]}_") for stream in ordered
        ]
        self.build_command = build_command
        self.stop = stop
        self.on_failure = on_failure
        self.parallel = max(1, parallel)
        self.timeout_s = timeout_s
        self.budget_s = budget_s
        self.launch_delay_s = max(0.0, launch_delay_s)
        self.writer = writer
        self.winner: StreamAttempt | None = None
        self.failures: list[StreamAttempt] = []

    async def _compete(self, attempt: StreamAttempt) -> bool:
        if attempt.stream.expires_at is not None and attempt.stream.expires_at <= datetime.now(UTC):
            attempt.categories.add(FfmpegErrorType.TRANSIENT_NETWORK)
            attempt.remember(b"Candidate URL expired while waiting for a race slot")
            return False
        cmd = self.build_command(attempt.stream, attempt.out_dir, attempt.prefix, attempt.segment_list)
        # 保留默认进度周期，避免可选的 -stats_period 提高 FFmpeg 版本要求。
        cmd[1:1] = ["-progress", "pipe:1"]

        async def start_process() -> tuple[asyncio.subprocess.Process, ProcessTree]:
            with self.writer.spawn_options() if self.writer is not None else nullcontext({}) as ownership:
                return await spawn_owned_async(
                    cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **ownership,
                    **({"creationflags": 0x00000200} if os.name == "nt" else {}),
                )

        spawn = asyncio.create_task(start_process())
        waits: list[asyncio.Task[object]] = []
        try:
            # 取消也必须拿到已启动的进程句柄，否则取消窗口可能留下孤儿 FFmpeg。
            try:
                attempt.process, attempt.tree = await asyncio.shield(spawn)
            except asyncio.CancelledError:

                async def finish_spawn() -> None:
                    try:
                        attempt.process, attempt.tree = await spawn
                    except OSError:
                        pass

                await complete_cleanup(finish_spawn())
                raise
            attempt.stderr_task = asyncio.create_task(attempt.drain_stderr())
            attempt.progress_task = asyncio.create_task(attempt.drain_progress())
            ready = asyncio.create_task(attempt.ready.wait())
            exited = asyncio.create_task(wait_process_exit(attempt.process))
            waits = [ready, exited]
            done, _ = await asyncio.wait(
                [ready, exited, attempt.stderr_task, attempt.progress_task],
                timeout=self.timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for reader in (attempt.stderr_task, attempt.progress_task):
                if reader in done:
                    reader.result()
            if attempt.ready.is_set() and attempt.process.returncode is None and not attempt.global_error:
                return True
            if not done:
                attempt.categories.add(FfmpegErrorType.TRANSIENT_NETWORK)
                attempt.remember(b"Candidate timed out before usable video progress")
            else:
                attempt.exit_code = attempt.process.returncode or 1
                attempt.remember(b"Candidate ended before usable video progress")
        except OSError as exc:
            if exc.errno in _RESOURCE_ERRNOS:
                attempt.resource_errno = exc.errno
            attempt.global_error |= (
                exc.errno in {errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOSPC}
                or attempt.resource_errno is not None
            )
            attempt.categories.add(
                {
                    errno.ENOENT: FfmpegErrorType.MISSING_BINARY,
                    errno.EACCES: FfmpegErrorType.PERMISSION_DENIED,
                    errno.EPERM: FfmpegErrorType.PERMISSION_DENIED,
                    errno.ENOSPC: FfmpegErrorType.DISK_FULL,
                }.get(exc.errno, FfmpegErrorType.UNKNOWN)
            )
            attempt.remember(
                (
                    f"Candidate process/read failure: {type(exc).__name__} "
                    f"errno={errno.errorcode.get(exc.errno, 'UNKNOWN')}"
                ).encode()
            )
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        return False

    async def run(self) -> StreamAttempt | None:
        """返回仍运行的胜出进程；停止、失败及预算到期都先关闭其他候选。"""
        pending: dict[asyncio.Task[bool], StreamAttempt] = {}
        remaining = deque(self.attempts)
        stopped = asyncio.create_task(self.stop.wait())
        deadline = time.monotonic() + self.budget_s
        next_launch_at = time.monotonic()
        try:
            while not self.stop.is_set() and time.monotonic() < deadline:

                def can_launch() -> bool:
                    return (
                        bool(remaining)
                        and len(pending) < self.parallel
                        and (
                            not pending
                            or next(iter(pending.values())).stream.quality_id == remaining[0].stream.quality_id
                        )
                    )

                if can_launch() and (not pending or time.monotonic() >= next_launch_at):
                    attempt = remaining.popleft()
                    pending[asyncio.create_task(self._compete(attempt))] = attempt
                    next_launch_at = time.monotonic() + self.launch_delay_s
                if not pending:
                    return None
                timeout = max(0, deadline - time.monotonic())
                if can_launch():
                    timeout = min(timeout, max(0, next_launch_at - time.monotonic()))
                done, _ = await asyncio.wait(
                    [*pending, stopped],
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stopped in done or (not done and time.monotonic() >= deadline):
                    if not self.stop.is_set():
                        for attempt in pending.values():
                            attempt.categories.add(FfmpegErrorType.TRANSIENT_NETWORK)
                            attempt.remember(b"Stream race time budget exhausted")
                            self.failures.append(attempt)
                    return None
                if not done:
                    continue  # 错峰启动计时到期；尚未耗尽整轮预算。
                completed = [task for task in pending if task in done]
                successes = []
                fatal = False
                for task in completed:
                    attempt = pending.pop(task)
                    if task.result():
                        successes.append(attempt)
                    else:
                        self.failures.append(attempt)
                        self.on_failure(attempt.stream)
                        logger.info(
                            "录制候选失败 endpoint={} transport={} container={} stderr={}",
                            sanitize_diagnostic(attempt.stream.url),
                            attempt.stream.transport,
                            attempt.stream.container,
                            " | ".join(attempt.tail[-3:]),
                        )
                        fatal |= attempt.global_error
                        await complete_cleanup(attempt.close(discard=True))
                        next_launch_at = time.monotonic()  # 明确失败后立即补位。
                if fatal:
                    return None
                for attempt in successes:
                    if attempt.process is not None and attempt.process.returncode is None:
                        self.winner = attempt
                        break
                if self.winner is not None:
                    break
        finally:

            async def cleanup() -> None:
                stopped.cancel()
                for task in pending:
                    task.cancel()
                await asyncio.gather(stopped, *pending, return_exceptions=True)
                await self.close(keep_winner=True)

            await complete_cleanup(cleanup())

        # 回收落选者期间，胜出进程也可能已经退出；交接前再次检查。
        winner = self.winner
        if winner is not None and (
            self.stop.is_set()
            or winner.global_error
            or winner.process is None
            or winner.process.returncode is not None
            or any(task is None or task.done() for task in (winner.stderr_task, winner.progress_task))
        ):
            if not self.stop.is_set():
                winner.remember(b"Winning stream ended or failed during process handoff")
                self.failures.append(winner)
                self.on_failure(winner.stream)
            self.winner = None
            await complete_cleanup(winner.close(discard=True))
            return None
        return winner

    async def close(self, *, keep_winner: bool = False) -> None:
        """所有句柄始终由 race 持有，交接期间取消也能回收胜出者。"""
        results = await asyncio.gather(
            *(
                attempt.close(discard=attempt is not self.winner)
                for attempt in self.attempts
                if not (keep_winner and attempt is self.winner)
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
