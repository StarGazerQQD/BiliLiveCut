"""真实 FFmpeg 与本机 HTTP 流验证，无需 B 站账号或外网。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from app.plugins.live_source import SourceRoom, StreamSpec
from app.recording.recorder import Recorder
from app.recording.stream_race import StreamAttempt, StreamRace


@pytest.mark.parametrize("outcome", ["recovers", "budget-exhausted", "cancelled"])
async def test_recorder_recovers_from_hls_init_failure_through_real_source_chain(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from loguru import logger
    from sqlmodel import select

    from app.db.entities import LiveRoom, RawSegment, RecordingSession
    from app.db.session import get_session
    from tests.integration.test_network_recovery import http_boundary
    from tests.source_fixtures import bind_bilibili_room

    binary = shutil.which(settings.ffmpeg_path)
    assert binary is not None
    recover = outcome == "recovers"
    for name, start, live in (("a", 0, True), ("b", 3, True), ("healthy", 0, False)):
        subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=15",
                "-t",
                "3" if name == "a" else "12",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "15",
                "-sc_threshold",
                "0",
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_list_size",
                "0",
                "-hls_segment_type",
                "fmp4",
                "-hls_fmp4_init_filename",
                f"init-{name}.mp4",
                "-start_number",
                str(start),
                *(["-hls_flags", "omit_endlist"] if live else []),
                "-hls_segment_filename",
                str(tmp_path / f"{name}-%d.m4s"),
                str(tmp_path / f"{name}.m3u8"),
            ],
            capture_output=True,
            check=True,
            timeout=20,
            cwd=tmp_path,
        )
    requests: list[str] = []
    switched = threading.Event()
    failures = threading.Event()
    release_failure = threading.Event()
    processes: list[asyncio.subprocess.Process] = []
    diagnostics: list[str] = []
    fetches = 0

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            if self.path == "/init-b.mp4":
                failures.set()
                if outcome == "cancelled":
                    release_failure.wait(5)
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            name = self.path.removeprefix("/")
            if name == "live.m3u8":
                name = "b.m3u8" if switched.is_set() else "a.m3u8"
            path = tmp_path / name
            if not path.is_file():
                self.send_error(404)
                return
            payload = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
                self.wfile.flush()
                if name == "a-2.m4s":
                    switched.set()
            except OSError:
                pass  # 录制取消时允许关闭正在读取的连接。

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    original_spawn = asyncio.create_subprocess_exec

    async def spawn(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    with get_session() as db:
        room = LiveRoom(input_url="123", room_id=123, authorized=True)
        db.add(room)
        db.flush()
        bind_bilibili_room(db, room)
        room_id = room.id
    monkeypatch.setattr(settings, "segment_duration_s", 5)
    monkeypatch.setattr(settings, "collect_danmaku", False)
    monkeypatch.setattr(settings, "recording_stream_race_parallel", 1)
    monkeypatch.setattr(settings, "recording_reconnect_max_attempts", 2)
    monkeypatch.setattr(settings, "recording_reconnect_max_elapsed_s", 20)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)

    async def segment_ready(segment: RawSegment) -> None:
        if recover and fetches >= 2 and segment.duration_s >= 5:
            recorder.stop()

    recorder = Recorder(
        SourceRoom(platform="bilibili", source_id="123", canonical_url="https://live.bilibili.com/123"),
        room_id,
        on_segment=segment_ready,
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal fetches
            if request.url.path.endswith("room_init"):
                data = {"room_id": 123, "uid": 1, "live_status": 1}
            elif request.url.path.endswith("getInfoByRoom"):
                data = {"room_info": {"title": "HLS recovery"}, "anchor_info": {"base_info": {"uname": "fixture"}}}
            else:
                assert request.url.path.endswith("getRoomPlayInfo")
                fetches += 1
                path = "/healthy.m3u8" if recover and fetches > 1 else "/live.m3u8"
                data = {
                    "live_status": 1,
                    "playurl_info": {
                        "playurl": {
                            "stream": [
                                {
                                    "protocol_name": "http_hls",
                                    "format": [
                                        {
                                            "format_name": "fmp4",
                                            "codec": [
                                                {
                                                    "codec_name": "avc",
                                                    "current_qn": 10000,
                                                    "base_url": path,
                                                    "url_info": [
                                                        {"host": f"http://127.0.0.1:{server.server_port}", "extra": ""}
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                }
            return httpx.Response(200, json={"code": 0, "data": data})

        http_boundary(monkeypatch, respond)
        sink = logger.add(lambda message: diagnostics.append(str(message)), level="DEBUG")
        recording = asyncio.create_task(recorder.run())
        try:
            if outcome == "cancelled":
                async with asyncio.timeout(15):
                    while not failures.is_set():
                        assert not recording.done()
                        await asyncio.sleep(0.01)
                recorder.stop()
                release_failure.set()
            await asyncio.wait_for(asyncio.shield(recording), 35)
        finally:
            recorder.stop()
            release_failure.set()
            if not recording.done():
                await asyncio.wait_for(recording, 12)
            logger.remove(sink)
            server.shutdown()
            thread.join(timeout=3)
    assert failures.is_set() and switched.is_set(), requests
    assert fetches == (1 if outcome == "cancelled" else 2), (fetches, requests, diagnostics[-12:])
    assert all(process.returncode is not None for process in processes)
    assert all(process.returncode not in {3221226505, -1073740791, -6, -11} for process in processes)
    assert not any("Assertion v->input failed" in message for message in diagnostics)
    with get_session() as db:
        session = db.get(RecordingSession, recorder.session_id)
        assert session is not None and session.reconnect_count == (0 if outcome == "cancelled" else 1)
        segments = db.exec(select(RawSegment).where(RawSegment.session_id == recorder.session_id)).all()
    assert len({segment.file_path for segment in segments}) == len(segments)
    if recover:
        complete = [segment for segment in segments if segment.duration_s >= 5]
        assert complete and "/healthy.m3u8" in requests
        probe = subprocess.run(
            [
                settings.ffprobe_path,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                complete[0].file_path,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert "h264" in probe.stdout
    elif outcome == "budget-exhausted":
        assert recorder.recording_error is not None and "预算耗尽" in recorder.recording_error
    else:
        assert recorder.recording_error is None


@pytest.mark.parametrize("legacy_progress", [False, True])
@pytest.mark.parametrize("timer_resolution_s", [0.0, 0.05], ids=["normal-clock", "coarse-clock"])
async def test_real_ffmpeg_keeps_winning_http_connection_and_writes_playable_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_progress: bool, timer_resolution_s: float
) -> None:
    binary = shutil.which(settings.ffmpeg_path)
    assert binary is not None, "录制集成测试需要 FFmpeg"
    source = tmp_path / "fixture.ts"
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=15",
            "-t",
            "60",
            "-c:v",
            "mpeg2video",
            "-b:v",
            "500k",
            "-minrate",
            "500k",
            "-maxrate",
            "500k",
            "-bufsize",
            "1000k",
            "-g",
            "15",
            "-f",
            "mpegts",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    media = source.read_bytes()
    requests: list[str] = []
    sent_bytes: dict[str, int] = {}
    progress_reports: dict[str, deque[tuple[float, dict[str, str]]]] = {}
    started = time.monotonic()
    shutdown = threading.Event()
    probes = []
    original_probe = StreamAttempt._has_video_packets

    async def probe(attempt: StreamAttempt) -> bool:
        result = await original_probe(attempt)
        probes.append(result)
        return result

    monkeypatch.setattr(StreamAttempt, "_has_video_packets", probe)
    original_drain = StreamAttempt.drain_progress

    async def observe_progress(attempt: StreamAttempt) -> None:
        assert attempt.process is not None and attempt.process.stdout is not None
        reader = attempt.process.stdout
        original_readline = reader.readline
        reports = progress_reports.setdefault(attempt.stream.url, deque(maxlen=6))
        fields: dict[str, str] = {}

        async def readline() -> bytes:
            while line := await original_readline():
                key, _, value = line.decode("ascii", errors="replace").strip().partition("=")
                if key in {"frame", "out_time_us", "out_time", "progress"}:
                    fields[key] = value
                if key == "progress":
                    reports.append((round(time.monotonic() - started, 3), fields.copy()))
                    fields.clear()
                if not legacy_progress or not line.startswith(b"frame="):
                    return line
            return b""

        monkeypatch.setattr(reader, "readline", readline)
        await original_drain(attempt)

    monkeypatch.setattr(StreamAttempt, "drain_progress", observe_progress)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            try:
                if self.path == "/slow":
                    shutdown.wait(15)  # HTTP 200 但没有媒体，不能胜出。
                    return
                # 连续时间轴，避免重复拼接短 TS 引入倒退时间戳和损坏包。
                stream_started = time.monotonic()
                chunk_size = 1316
                bytes_per_second = chunk_size / 0.01
                for offset in range(0, len(media), chunk_size):
                    self.wfile.write(media[offset : offset + chunk_size])
                    self.wfile.flush()
                    sent_bytes[self.path] = min(offset + chunk_size, len(media))
                    # 按绝对发送时刻补偿唤醒延迟，避免粗定时器把测试流降到媒体码率以下。
                    remaining = stream_started + sent_bytes[self.path] / bytes_per_second - time.monotonic()
                    wait_s = max(remaining, timer_resolution_s) if remaining > 0 else 0.0
                    if shutdown.wait(wait_s):
                        return
                shutdown.wait(15)
            except OSError:
                pass  # 落选连接或测试结束后主动断开。

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    monkeypatch.setattr(settings, "ffmpeg_path", binary)
    monkeypatch.setattr(settings, "segment_duration_s", 5)
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)
    recorder = Recorder(SourceRoom(platform="external", source_id="1", canonical_url="https://source.invalid/1"), 1)
    output = tmp_path / "race"
    output.mkdir()
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        candidates = [
            StreamSpec(url=f"http://127.0.0.1:{server.server_port}/{name}", transport="flv", container="flv")
            for name in ("slow", "good")
        ]
        race = StreamRace(
            candidates,
            output,
            0,
            recorder._build_ffmpeg_cmd,
            asyncio.Event(),
            lambda _: None,
            parallel=2,
            timeout_s=10,
            budget_s=12,
        )
        try:
            winner = await asyncio.wait_for(race.run(), 12)
            assert winner is not None and winner.stream.url.endswith("/good"), (
                f"requests={requests}, sent_bytes={sent_bytes}, fixture_bytes={len(media)}, "
                f"progress={progress_reports}, probes={probes}"
            )
            process = winner.process
            assert process is not None and process.returncode is None
            pid = process.pid
            first_size = sum(path.stat().st_size for path in output.glob(f"{winner.prefix}*.ts"))
            for _ in range(30):
                await asyncio.sleep(0.1)
                if sum(path.stat().st_size for path in output.glob(f"{winner.prefix}*.ts")) > first_size:
                    break
            assert process.pid == pid and process.returncode is None
            assert sum(path.stat().st_size for path in output.glob(f"{winner.prefix}*.ts")) > first_size
            assert requests.count("/good") == 1, "胜出线路不应重新连接"
            if legacy_progress:
                assert any(probes), "旧版无 frame 报告必须核验真实本地视频包"
            assert race.attempts[0].process.returncode is not None
            assert not list(output.glob(f"{race.attempts[0].prefix}*"))
            process.stdin.write(b"q\n")
            await process.stdin.drain()
            await asyncio.wait_for(process.wait(), 5)
        finally:
            await race.close()
            shutdown.set()
            server.shutdown()
            thread.join(timeout=3)
    files = list(output.glob("*.ts"))
    assert files
    probe = subprocess.run(
        [
            settings.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(files[0]),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert "mpeg2video" in probe.stdout


@pytest.mark.parametrize("parallel", [1, 2], ids=["serial", "race"])
async def test_shutdown_flushes_final_segment_without_reconnecting(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parallel: int
) -> None:
    from sqlmodel import select

    from app.db.entities import LiveRoom, RawSegment
    from app.db.session import get_session
    from app.pipeline import storage_lifecycle
    from app.pipeline.lifecycle import shutdown_event
    from app.recording import recorder as module

    binary = shutil.which(settings.ffmpeg_path)
    assert binary is not None
    with get_session() as db:
        room = LiveRoom(input_url="https://example.invalid/1", room_id=1, authorized=True)
        db.add(room)
        db.flush()
        room_id = room.id
    recorder = Recorder(
        SourceRoom(platform="external", source_id="1", canonical_url="https://example.invalid/1"), room_id
    )
    recorder._session_id = recorder._create_session()
    fetches = []
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    async def no_danmaku():
        pass

    async def fetch():
        fetches.append(1)
        recorder._stream_candidates = [
            StreamSpec(url=f"https://example.invalid/{i}", transport="hls", container="ts") for i in range(parallel)
        ]
        return recorder._stream_candidates[0]

    def command(stream, out_dir, prefix, segment_list):
        return [
            binary,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-c:v",
            "mpeg2video",
            "-g",
            "10",
            "-f",
            "segment",
            "-segment_time",
            "60",
            "-reset_timestamps",
            "1",
            "-segment_list",
            str(segment_list),
            "-segment_list_type",
            "csv",
            str(out_dir / f"{prefix}%05d.ts"),
        ]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(module, "session_raw_dir", lambda _: tmp_path)
    monkeypatch.setattr(storage_lifecycle, "should_stop_recording", lambda: False)
    monkeypatch.setattr(settings, "recording_stream_race_parallel", parallel)
    monkeypatch.setattr(recorder, "_start_danmaku", no_danmaku)
    monkeypatch.setattr(recorder, "_fetch_stream", fetch)
    monkeypatch.setattr(recorder, "_build_ffmpeg_cmd", command)
    recording = asyncio.create_task(recorder._run_session())
    try:
        async with asyncio.timeout(15):
            while recorder._active_process is None:
                assert not recording.done(), "录制在关闭前意外退出"
                await asyncio.sleep(0.05)
            await asyncio.sleep(2)
            # 模拟 lifespan 已发出关闭信号、但监控器仍在异步退出的窗口。
            shutdown_event.set()
            await recording
        with get_session() as db:
            segments = db.exec(select(RawSegment).where(RawSegment.session_id == recorder.session_id)).all()
        assert len(segments) == 1
        assert 0 < segments[0].duration_s < 60 and segments[0].size_bytes > 0
        assert Path(segments[0].file_path).is_file()
        assert fetches == [1] and not recorder._last_exit_abnormal
        assert all(process.returncode is not None for process in processes)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", segments[0].file_path],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert "video" in probe.stdout
    finally:
        recorder.stop()
        if not recording.done():
            await asyncio.wait_for(recording, 12)
