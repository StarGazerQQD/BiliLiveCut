"""Verify automatic live startup through real monitoring and task registration."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlmodel import select

from app.core.config import settings
from app.plugins.live_source import LiveStatus
from tests.integration.test_bilibili_client import _WBI_RESPONSE, _play_data
from tests.integration.test_recording_progress import _ACTOR
from tests.integration.test_recording_progress import video as video
from tests.source_fixtures import LiveEventFeed, bind_bilibili_room

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@pytest.mark.asyncio
@pytest.mark.usefixtures("temp_db")
@pytest.mark.parametrize("already_live", [False, True])
@pytest.mark.parametrize("auto_record,auto_analyze", [(True, True), (True, False), (False, True)])
async def test_live_detection_starts_recording_and_respects_analysis_switch(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    already_live: bool,
    auto_record: bool,
    auto_analyze: bool,
    bili_live_feed: LiveEventFeed,
    video: Path,
) -> None:
    """Exercise real startup and queue advancement with controlled external boundaries."""
    from app.db.entities import LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline import live_monitor as module
    from app.pipeline.scheduler import advance_recorded
    from app.sources.bilibili.client import BilibiliLiveClient
    from app.web.services import rooms
    from app.web.services import rooms as rooms_service

    with get_session() as db:
        room = LiveRoom(
            input_url="202",
            room_id=202,
            title="测试直播",
            uploader_name="测试主播",
            authorized=True,
            auto_record=auto_record,
            auto_analyze=auto_analyze,
        )
        db.add(room)
        db.flush()
        bind_bilibili_room(db, room)
        db_id = room.id
    assert db_id is not None
    live_status = 1 if already_live else 0
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args: str, **options: object) -> asyncio.subprocess.Process:
        listing = args[args.index("-segment_list") + 1]
        command = [sys.executable, "-c", _ACTOR, "stable", "2", listing, args[-1], str(video)]
        if os.name != "nt":
            # 保留 POSIX writer 继承与父死亡监管，仅替换外部媒体进程。
            command = [*args[:7], sys.executable, *command]
        process = await spawn(*command, **options)
        processes.append(process)
        return process

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/room/v1/Room/room_init":
            data = {"room_id": 202, "uid": 123, "live_status": live_status}
        elif request.url.path == "/xlive/web-room/v2/index/getRoomPlayInfo":
            data = _play_data(10000)
        elif request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=_WBI_RESPONSE, request=request)
        else:
            assert request.url.path == "/xlive/web-room/v1/index/getInfoByRoom"
            data = {
                "room_info": {"uid": 123, "title": "测试直播"},
                "anchor_info": {"base_info": {"uname": "测试主播"}},
            }
        return httpx.Response(200, json={"code": 0, "data": data}, request=request)

    class TransportClient(BilibiliLiveClient):
        async def __aenter__(self) -> TransportClient:
            await self._client.aclose()
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            return self

    monkeypatch.setattr("app.sources.bilibili.source.BilibiliLiveClient", TransportClient)
    monkeypatch.setattr("app.sources.bilibili.source.get_bilibili_cookie", lambda: "")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    monkeypatch.setattr(settings, "collect_danmaku", False)
    monkeypatch.setattr(rooms, "open_path", lambda path: None)
    manager = rooms.RecorderManager()
    monkeypatch.setattr(rooms_service, "recorder_manager", manager)
    monitor = module.LiveMonitor()
    monitor._stop = asyncio.Event()
    try:
        await monitor._check_all()
        if auto_record:
            await bili_live_feed.ready(monitor, db_id)
        if not already_live:
            assert not processes and not manager.is_running(db_id)
        live_status = 1
        if auto_record:
            await bili_live_feed.send(LiveStatus.LIVE)
            async with asyncio.timeout(3):
                while True:
                    with get_session() as db:
                        segment = db.exec(select(RawSegment)).first()
                        queued = db.exec(select(SegmentTask)).first()
                    if segment is not None and (not auto_analyze or queued is not None):
                        break
                    assert manager.is_running(db_id), manager.status(db_id)
                    await asyncio.sleep(0.05)
            assert manager.is_running(db_id)
            recorder = manager._recorders[db_id]
            if recorder.on_segment is not None:
                await recorder.on_segment(segment)
            await bili_live_feed.send(LiveStatus.LIVE)
        await monitor._check_all()
        assert len(manager._recorders) == len(processes) == int(auto_record)
        advance_recorded()
        with get_session() as db:
            sessions = db.exec(select(RecordingSession)).all()
            segments = db.exec(select(RawSegment)).all()
            tasks = db.exec(select(SegmentTask)).all()
        assert len(sessions) == len(segments) == int(auto_record)
        assert len(tasks) == int(auto_record and auto_analyze)
        if tasks:
            assert tasks[0].stage == TaskStatus.QUEUED_FOR_ANALYSIS
            assert tasks[0].session_id == sessions[0].id
            assert tasks[0].segment_id == segments[0].id
        assert all(session.error_message is None for session in sessions)
        assert all(segment.duration_s > 0 and Path(segment.file_path).is_file() for segment in segments)
        print(
            f"already_live={already_live} auto_record={auto_record} auto_analyze={auto_analyze}: "
            f"sessions={len(sessions)}, segments={len(segments)}, analysis_tasks={len(tasks)}; "
            "repeated live notifications and segment events do not duplicate work"
        )
    finally:
        await monitor.stop()
        await manager.stop_all()
        assert all(process.returncode is not None for process in processes)
