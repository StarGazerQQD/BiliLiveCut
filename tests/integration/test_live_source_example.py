"""真实独立插件 + 本地 HTTP + FFmpeg + 宿主持久流水线的离线契约验收。"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from loguru import logger
from sqlmodel import select

from app.core.config import settings
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession, SegmentTask
from app.db.session import get_session
from app.pipeline.orchestrator import make_pipeline_callback
from app.plugins.live_source import SourceUnavailable
from app.plugins.manager import PluginManager
from app.recording.danmaku import read_evidence
from app.recording.recorder import Recorder
from app.sources.registry import SourceRegistry, source_registry
from app.sources.rooms import register_room, room_source, room_source_view

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ID = "live-source-example"


class MediaService:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.origin = ""
        self.transport = "flv"
        self.keep_live = False
        self.playlist_reads = 0
        self.stream_calls = 0
        self.title = "示例标题 A"
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.key = "example-header-secret"

    def response(self, raw_path: str, headers: dict[str, str]) -> tuple[int, str, bytes]:
        self.requests.append((raw_path, headers))
        if headers.get("X-Example-Key") != self.key:
            return 403, "text/plain", b"credential required"
        parsed = urlsplit(raw_path)
        path = unquote(parsed.path)
        payload: object
        if path == "/resolve":
            value = parse_qs(parsed.query)["value"][0]
            if value not in {"room:alpha", self.origin + "/s/alpha", self.origin + "/room/room%3Aalpha"}:
                return 404, "text/plain", b"unknown room"
            payload = {
                "platform": "example_live",
                "source_id": "room:alpha",
                "canonical_url": self.origin + "/room/room%3Aalpha",
            }
        elif path == "/rooms/room:alpha/info":
            payload = {"status": "live", "title": self.title, "uploader_name": "示例主播"}
        elif path == "/rooms/room:alpha/streams":
            self.stream_calls += 1
            filename = "sample.flv" if self.transport == "flv" else "index.m3u8"
            item = {
                "url": f"{self.origin}/media/token-{self.stream_calls}/{filename}",
                "transport": self.transport,
                "container": "flv" if self.transport == "flv" else "ts",
                "quality_id": "original",
                "quality_label": "原始质量",
            }
            payload = [
                {
                    **item,
                    "url": self.origin + "/expired",
                    "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                },
                item,
            ]
        elif path.startswith(f"/media/token-{self.stream_calls}/"):
            filename = path.rsplit("/", 1)[-1]
            allowed = {file.name for file in self.directory.iterdir()}
            if filename not in allowed:
                return 404, "text/plain", b"missing media"
            content = (self.directory / filename).read_bytes()
            if filename.endswith(".m3u8"):
                if self.keep_live:
                    # 真直播播放列表逐次增加片段；固定末尾列表没有新媒体，不能用于在录停用验收。
                    pieces = re.findall(rb"#EXTINF:[^\n]+\n[^\n]+\n", content)
                    count = min(len(pieces), 3 + self.playlist_reads * 6)
                    self.playlist_reads += 1
                    content = content.split(b"#EXTINF:", 1)[0] + b"".join(pieces[:count])
                return 200, "application/vnd.apple.mpegurl", content
            return 200, "video/x-flv" if filename.endswith(".flv") else "video/mp2t", content
        else:
            return 403, "text/plain", b"expired URL"
        return 200, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def media_service(tmp_path: Path) -> Iterator[MediaService]:
    binary = shutil.which(settings.ffmpeg_path)
    assert binary, "此契约验收必须安装真实 FFmpeg，不能跳过"
    directory = tmp_path / "media"
    directory.mkdir()
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "50",
            "-af",
            "volume='if(between(t,30,42),1,0.02)':eval=frame",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "20",
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-f",
            "flv",
            str(directory / "sample.flv"),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(directory / "sample.flv"),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "0",
            str(directory / "index.m3u8"),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    service = MediaService(directory)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, content_type, body = service.response(self.path, dict(self.headers.items()))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # 停录时客户端关闭正在重读的播放列表。

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    service.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.fixture
def plugin_host(temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginManager:
    directory = tmp_path / "plugins"
    shutil.copytree(ROOT / "plugin" / PLUGIN_ID, directory / PLUGIN_ID)
    monkeypatch.setattr(source_registry, "_entries", SourceRegistry()._entries)
    monkeypatch.setattr(settings, "segment_duration_s", 50)
    monkeypatch.setattr(settings, "collect_danmaku", True)
    monkeypatch.setattr(settings, "bilibili_cookie", "other-platform-cookie")
    return PluginManager(directory, registry=source_registry)


async def _enable(manager: PluginManager, service: MediaService) -> None:
    await manager.start()
    await manager.set_enabled(PLUGIN_ID, True)
    payload = manager.update_settings(PLUGIN_ID, {"api_origin": service.origin, "access_key": service.key})
    password = next(field for field in payload["fields"] if field["key"] == "access_key")
    assert password["value"] == "" and password["configured"] is True


@pytest.mark.parametrize("transport", ["flv", "hls"])
async def test_loaded_plugin_real_ffmpeg_reconnect_and_durable_pipeline(
    plugin_host: PluginManager,
    media_service: MediaService,
    transport: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_service.transport = transport
    await _enable(plugin_host, media_service)
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        room = await register_room(media_service.origin + "/s/alpha", True)
        duplicate = await register_room("room:alpha", True, "example_live")
        assert duplicate.id == room.id and room.room_id is None
        assert room_source(room).source_id == "room:alpha"
        with get_session() as db:
            saved = db.get(LiveRoom, room.id)
            assert not any(
                (saved.auto_record, saved.auto_analyze, saved.auto_render, saved.auto_approve, saved.auto_upload)
            )
            saved.auto_analyze = True
            db.add(saved)
        callback = make_pipeline_callback(room_id=room.id)

        async def received(segment: RawSegment) -> None:
            await callback(segment)
            await callback(segment)  # 正式回调重复提交也不重复生成持久任务。
            if media_service.stream_calls >= 2:
                recorder.stop()

        recorder = Recorder(room_source(room), room.id, on_segment=received)
        await asyncio.wait_for(recorder.run(), 30)
        with get_session() as db:
            recording = db.get(RecordingSession, recorder.session_id)
            segments = db.exec(select(RawSegment).order_by(RawSegment.seq)).all()
            tasks = db.exec(select(SegmentTask)).all()
            assert recording.status == "stopped" and recording.reconnect_count == 1
            assert recording.stream_url is None and recording.ended_at is not None
            assert len(segments) >= 2 and len(tasks) == len(segments)
            assert len({item.file_path for item in segments}) == len(segments)
            assert all(Path(item.file_path).stat().st_size > 1024 and item.duration_s > 1 for item in segments)
            evidence = read_evidence(db, recording.id)
            assert evidence.status == "unsupported" and not evidence.intervals
            assert not db.get(LiveRoom, room.id).auto_approve and not db.get(LiveRoom, room.id).auto_upload
            normal_metadata = " ".join(
                row.value for row in db.exec(select(AppSetting)).all() if not row.key.startswith("plugin.")
            )
            assert "token-" not in normal_metadata
            assert media_service.key not in normal_metadata
        media_requests = [(path, headers) for path, headers in media_service.requests if path.startswith("/media/")]
        assert any("token-1" in path for path, _ in media_requests)
        assert any("token-2" in path for path, _ in media_requests)
        assert not any(path == "/expired" for path, _ in media_service.requests)
        assert all(
            headers.get("X-Example-Key") == media_service.key and "Cookie" not in headers
            for _, headers in media_requests
        )
        assert media_service.key not in " ".join(messages) and "token-" not in " ".join(messages)
        if transport == "flv":
            await asyncio.to_thread(_advance_real_pipeline, room.id, recorder.session_id, monkeypatch)
        await plugin_host.stop()
        assert not room_source_view(room)["source_available"]
        with pytest.raises(SourceUnavailable):
            await source_registry.get_room_info(room_source(room))
        # 模拟新进程生命周期，实际加载同一独立文件并读取保存的设置、房间绑定。
        restarted = PluginManager(plugin_host.root, registry=source_registry)
        await restarted.start()
        try:
            assert (await register_room(room.input_url, True)).id == room.id
            assert (await source_registry.get_room_info(room_source(room))).title == media_service.title
        finally:
            await restarted.stop()
    finally:
        logger.remove(sink)
        await plugin_host.stop()


async def test_disable_loaded_plugin_drains_active_real_ffmpeg(
    plugin_host: PluginManager,
    media_service: MediaService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_service.transport = "hls"
    media_service.keep_live = True
    monkeypatch.setattr(settings, "segment_duration_s", 10)
    await _enable(plugin_host, media_service)
    room = await register_room("room:alpha", True, "example_live")
    first_segment = asyncio.Event()
    callback = make_pipeline_callback(room_id=room.id)
    endings: list[int] = []
    with get_session() as db:
        saved = db.get(LiveRoom, room.id)
        saved.auto_analyze = True
        db.add(saved)

    async def received(segment: RawSegment) -> None:
        await callback(segment)
        first_segment.set()

    async def ended(session_id: int) -> None:
        endings.append(session_id)

    recorder = Recorder(room_source(room), room.id, on_segment=received, on_end=ended)
    task = asyncio.create_task(recorder.run())
    try:
        await asyncio.wait_for(first_segment.wait(), 20)
        assert not task.done() and recorder._active_process is not None
        await asyncio.wait_for(plugin_host.set_enabled(PLUGIN_ID, False), 10)
        await task
        assert not source_registry.available("example_live")
        assert recorder._active_process is None and endings == [recorder.session_id]
        assert plugin_host.descriptor(PLUGIN_ID)["loaded"] is False
        with get_session() as db:
            segments = db.exec(select(RawSegment)).all()
            assert len(segments) == len(db.exec(select(SegmentTask)).all()) > 0
            assert db.get(RecordingSession, recorder.session_id).ended_at is not None
            assert db.get(LiveRoom, room.id) is not None
    finally:
        recorder.stop()
        await asyncio.wait_for(task, 5)
        await plugin_host.stop()


def _advance_real_pipeline(room_id: int, session_id: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.analysis import llm
    from app.analysis.transcription.backends import FasterWhisperBackend
    from app.analysis.transcription.models import ASRSegmentResult, ASRTranscriptResult, Word
    from app.clipping.core import probe_media
    from app.db.entities import FinalClip, HighlightCandidate, HotspotEvent, TaskStatus, Transcript
    from app.pipeline import scheduler
    from app.pipeline.approval import approve_event_and_task
    from app.pipeline.claiming import pop_and_claim
    from app.publishing.copywriter import generate_copy

    for key, value in {
        "asr_primary": "whisper",
        "asr_sensevoice": False,
        "asr_sensevoice_enabled": False,
        "asr_funasr_review": False,
        "asr_fallback_whisper": False,
        "hotspot_asr_enabled": False,
        "clip_vertical": False,
        "clip_subtitle": False,
        "clip_remove_silence": False,
        "clip_preset": "ultrafast",
        "hotspot_bucket_s": 5,
        "hotspot_min_baseline_buckets": 2,
        "hotspot_detector_tick_s": 15,
        "hotspot_baseline_window_s": 60,
        "hotspot_detection_threshold": 0.15,
    }.items():
        monkeypatch.setattr(settings, key, value)
    asr_inputs: list[str] = []
    llm_prompts: list[str] = []
    text = "主播观察对手位置之后果断出击，这波配合完成了漂亮的五杀。随后解释关键操作与技能释放顺序。"

    def recognize(
        self: FasterWhisperBackend, audio_path: str, initial_prompt: str | None = None
    ) -> ASRTranscriptResult:
        assert Path(audio_path).is_file()
        asr_inputs.append(audio_path)
        return ASRTranscriptResult(
            text=text,
            backend="whisper",
            model_id="offline-contract-fixture",
            audio_duration=50,
            segments=[ASRSegmentResult(start=30, end=42, text=text, words=[Word(word=text, start=30, end=42)])],
        )

    def language_model(prompt: str, **kwargs: object) -> None:
        llm_prompts.append(prompt)
        return None  # 外部模型服务边界：真实分析执行其规则回退。

    monkeypatch.setattr(FasterWhisperBackend, "transcribe", recognize)
    monkeypatch.setattr(llm, "call_text", language_model)
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        room.highlight_threshold = 0.01
        room.review_threshold = 0.01
        room.auto_render = True
        db.add(room)
    for _ in range(15):
        scheduler.advance_recorded()
        scheduler.advance_transcribed()
        progressed = False
        for stage in (TaskStatus.QUEUED_FOR_ANALYSIS, TaskStatus.QUEUED_FOR_TRANS):
            while (claimed := pop_and_claim(stage)) is not None:
                progressed = True
                scheduler.execute_task(claimed.id, claimed.stage, claimed.lease_token)
        scheduler.advance_candidate()
        if not progressed:
            break
    with get_session() as db:
        tasks = db.exec(select(SegmentTask)).all()
        assert not any(task.stage in {TaskStatus.TRANSIENT_FAILED, TaskStatus.FAILED} for task in tasks), [
            (t.stage, t.last_error) for t in tasks
        ]
        assert len(db.exec(select(Transcript)).all()) >= 2
        events = db.exec(select(HotspotEvent)).all()
        candidates = db.exec(select(HighlightCandidate)).all()
        assert events and candidates, [(t.stage, t.last_error) for t in tasks]
        assert all(event.session_id == session_id for event in events)
        assert all(candidate.session_id == session_id for candidate in candidates)
        pending = [task for task in tasks if task.stage == TaskStatus.AWAITING_REVIEW]
        assert pending, [(t.stage, t.last_error) for t in tasks]
        assert not db.get(LiveRoom, room_id).auto_approve and not db.get(LiveRoom, room_id).auto_upload
        assert not db.exec(select(FinalClip)).all()
    assert asr_inputs and llm_prompts
    for task in pending:
        assert approve_event_and_task(
            task_id=task.id,
            event_id=task.event_id,
            source="human",
            approved_by="contract-test",
            review_decision="approved_solo",
        )
    scheduler.advance_approved()
    while (claimed := pop_and_claim(TaskStatus.QUEUED_FOR_RENDER)) is not None:
        scheduler.execute_task(claimed.id, claimed.stage, claimed.lease_token)
    with get_session() as db:
        clips = db.exec(select(FinalClip)).all()
        tasks = db.exec(select(SegmentTask)).all()
        assert clips, [(t.stage, t.last_error) for t in tasks]
        assert any(task.stage == TaskStatus.RENDERED for task in tasks)
        for clip in clips:
            duration, width, height = probe_media(clip.file_path)
            assert duration >= 1 and width > 0 and height > 0
            assert Path(clip.file_path).stat().st_size > 1024
    for clip in clips:
        assert generate_copy(clip.id).title
    scheduler.advance_rendered()
    with get_session() as db:
        assert not db.exec(select(SegmentTask).where(SegmentTask.stage == TaskStatus.QUEUED_FOR_PUBLISH)).all()
