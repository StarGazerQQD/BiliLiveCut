"""Verify automatic live startup through real monitoring and task registration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from sqlmodel import select

from app.plugins.live_source import SourceRoom

if TYPE_CHECKING:
    from pytest import MonkeyPatch

    from app.recording.recorder import SegmentCallback, SessionEndCallback, StateCallback


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
) -> None:
    """Exercise real startup and queue advancement with controlled external boundaries."""
    from app.db.entities import LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline import live_monitor as module
    from app.pipeline.scheduler import advance_recorded
    from app.sources.bilibili.client import BilibiliLiveClient
    from app.web import service
    from app.web.services import rooms

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
        db_id = room.id
    assert db_id is not None
    live_status = 1 if already_live else 0
    emitted = asyncio.Event()
    finish = asyncio.Event()
    recorders: list[BoundaryRecorder] = []
    failures: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/room/v1/Room/room_init":
            data = {"room_id": 202, "uid": 123, "live_status": live_status}
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

    class BoundaryRecorder:
        """Replace only stream capture; persist a completed segment for real callbacks."""

        def __init__(
            self,
            source_room: SourceRoom,
            db_room_id: int,
            on_segment: SegmentCallback | None = None,
            on_end: SessionEndCallback | None = None,
            on_state: StateCallback | None = None,
            metadata_prepared: bool = False,
        ) -> None:
            assert source_room.source_id == "202" and source_room.platform == "bilibili" and db_room_id == db_id
            self.on_segment = on_segment
            self.session_id: int | None = None
            self.retry_budget_exhausted = False
            recorders.append(self)

        async def run(self) -> None:
            """Simulate the media boundary and deliver a duplicated segment event."""
            path = tmp_path / "segment.ts"
            path.write_bytes(b"audit-boundary-placeholder")
            with get_session() as db:
                session = RecordingSession(room_id=db_id, status="recording")
                db.add(session)
                db.flush()
                self.session_id = session.id
                assert self.session_id is not None
                segment = RawSegment(session_id=self.session_id, seq=0, file_path=str(path), duration_s=300)
                db.add(segment)
                db.flush()
                db.refresh(segment)
            if self.on_segment is not None:
                await self.on_segment(segment)
                await self.on_segment(segment)
            emitted.set()
            await finish.wait()

        def fail(self, reason: str) -> None:
            """Expose unexpected failures instead of hiding them in manager logging."""
            failures.append(reason)

    monkeypatch.setattr("app.sources.bilibili.source.BilibiliLiveClient", TransportClient)
    monkeypatch.setattr("app.sources.bilibili.source.get_bilibili_cookie", lambda: "")
    monkeypatch.setattr(rooms, "Recorder", BoundaryRecorder)
    manager = rooms.RecorderManager()
    monkeypatch.setattr(service, "recorder_manager", manager)
    monitor = module.LiveMonitor()
    monitor._stop = asyncio.Event()
    try:
        if not already_live:
            await monitor._check_all()
            assert not recorders and not manager.is_running(db_id)
        live_status = 1
        await monitor._check_all()
        if auto_record:
            await asyncio.wait_for(emitted.wait(), timeout=3)
            assert manager.is_running(db_id)
        await monitor._check_all()
        assert len(recorders) == int(auto_record)
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
        assert not failures
        print(
            f"already_live={already_live} auto_record={auto_record} auto_analyze={auto_analyze}: "
            f"sessions={len(sessions)}, segments={len(segments)}, analysis_tasks={len(tasks)}; "
            "repeated polling and segment events do not duplicate work"
        )
    finally:
        finish.set()
        await asyncio.gather(*list(manager._tasks.values()))
