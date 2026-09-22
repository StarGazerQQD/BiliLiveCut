"""录制入口、标题事实与自动化调度的审计回归。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

from app.db.entities import LiveRoom, RecordingSchedule, RecordingSession, SystemLog
from app.db.session import get_session
from app.plugins.live_source import SourceRoom, StreamSpec
from app.recording import metadata
from app.recording.metadata import SessionMetadata, read_metadata
from app.sources.bilibili import source as bili_source
from app.sources.bilibili.client import BilibiliLiveClient


@pytest.fixture
def platform(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"title": "标题 A", "live": 1, "error": False, "room_id": 202, "requests": 0}

    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        state["requests"] = int(state["requests"]) + 1
        if request.url.path == "/room/v1/Room/room_init":
            data = {"room_id": state["room_id"], "uid": 1, "live_status": state["live"]}
        else:
            if state["error"]:
                return httpx.Response(503)
            data = {"room_info": {"title": state["title"]}, "anchor_info": {"base_info": {"uname": "测试主播"}}}
        return httpx.Response(200, json={"code": 0, "data": data})

    class TransportClient(BilibiliLiveClient):
        def __init__(self, **kwargs: object) -> None:
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(bili_source, "BilibiliLiveClient", TransportClient)
    with get_session() as db:
        db.add(LiveRoom(id=1, input_url="202", room_id=202, title="缓存旧标题", authorized=True, schedule_enabled=True))
    return state


async def test_title_changes_are_observed_once_and_history_is_frozen(platform: dict[str, object]) -> None:
    from app.web.services.source_identity import source_identities_for_sessions

    await metadata.refresh_room_metadata(1)
    with get_session() as db:
        db.add(RecordingSession(id=1, room_id=1, status="recording"))
        db.add(RecordingSession(id=2, room_id=1, status="stopped", ended_at=datetime.now(UTC)))
    metadata.begin_session_metadata(1)
    platform["title"] = "标题 B"
    await metadata.refresh_room_metadata(1)
    await metadata.refresh_room_metadata(1)
    metadata.begin_session_metadata(1)
    metadata.end_session_metadata(1)
    platform["title"] = "标题 C"
    await metadata.refresh_room_metadata(1)
    with get_session() as db:
        snapshot = read_metadata(db, "session_metadata:1", SessionMetadata)
        assert snapshot.start_title == "标题 A" and snapshot.last_title == "标题 B"
        assert snapshot.change_count == 1 and snapshot.ended_at is not None
        assert db.get(LiveRoom, 1).title == "标题 C"
        sources = source_identities_for_sessions(db, [1, 2])
        assert sources[1]["session_title"] == "标题 B"
        assert sources[2]["session_title"] is None
        assert metadata.session_title_at(db, 1, datetime.now(UTC) - timedelta(days=1)) == "标题 A"
        assert metadata.session_title_at(db, 1) == "标题 B"


@pytest.mark.parametrize("failure", ["error", "empty", "wrong_room", "timeout"])
async def test_metadata_failures_preserve_success_timestamp(
    platform: dict[str, object], failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await metadata.refresh_room_metadata(1)
    with get_session() as db:
        original = metadata.room_metadata_view(db, db.get(LiveRoom, 1))
    if failure == "error":
        platform["error"] = True
    elif failure == "empty":
        platform["title"] = " "
    elif failure == "wrong_room":
        platform["room_id"] = 999
    else:

        async def timeout(*args: object, **kwargs: object) -> None:
            await asyncio.sleep(60)

        monkeypatch.setattr(bili_source.BilibiliLiveClient, "get_room_info", timeout)
        monkeypatch.setattr(metadata.settings, "room_metadata_refresh_timeout_s", 0.01)
    await metadata.refresh_room_metadata(1)
    with get_session() as db:
        room = db.get(LiveRoom, 1)
        current = metadata.room_metadata_view(db, room)
        assert room.title == "标题 A"
        assert current["state"] == "stale" and current["error"]
        assert current["observed_at"] == original["observed_at"]


@pytest.mark.parametrize("managed", [False, True])
async def test_recording_refreshes_title_while_media_is_running_and_stops_refresh_on_exit(
    platform: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, managed: bool
) -> None:
    from app.recording.recorder import Recorder
    from app.web.services import rooms

    captured = asyncio.Event()
    finish = asyncio.Event()
    recorders: list[Recorder] = []
    monkeypatch.setattr(metadata.settings, "room_metadata_refresh_interval_s", 0.01)
    monkeypatch.setattr("app.recording.recorder.settings.collect_danmaku", False)
    monkeypatch.setattr("app.recording.recorder.settings.room_metadata_refresh_interval_s", 0.01)
    monkeypatch.setattr("app.pipeline.storage_lifecycle.should_stop_recording", lambda: False)
    monkeypatch.setattr("app.recording.recorder.session_raw_dir", lambda _id: tmp_path)

    async def stream(self: Recorder) -> StreamSpec:
        return StreamSpec(
            url="https://example.invalid/live", transport="hls", quality_id="10000", codec="avc", container="ts"
        )

    async def media(self: Recorder, source: StreamSpec, directory: Path) -> int:
        recorders.append(self)
        captured.set()
        await finish.wait()
        self.stop()
        return 0

    monkeypatch.setattr(Recorder, "_fetch_stream", stream)
    monkeypatch.setattr(Recorder, "_record_once", media)

    # 不弹出文件管理器；其余录制生命周期保持真实。
    async def ended(session_id: int) -> None:
        return None

    monkeypatch.setattr(rooms, "_on_session_end", ended)
    manager = rooms.RecorderManager()
    task: asyncio.Task[None] | None = None
    if managed:
        await asyncio.gather(*(manager.start(1, pipeline=False) for _ in range(8)))
    else:
        task = asyncio.create_task(
            Recorder(
                SourceRoom(platform="bilibili", source_id="202", canonical_url="https://live.bilibili.com/202"), 1
            ).run()
        )
    try:
        await asyncio.wait_for(captured.wait(), 3)
        platform["title"] = "录制中改名"
        for _ in range(100):
            await asyncio.sleep(0.01)
            with get_session() as db:
                if db.get(LiveRoom, 1).title == "录制中改名":
                    break
        with get_session() as db:
            assert db.get(LiveRoom, 1).title == "录制中改名"
            assert len(db.exec(select(RecordingSession)).all()) == 1
            assert read_metadata(db, "session_metadata:1", SessionMetadata).start_title == "标题 A"
        assert len(recorders) == 1
    finally:
        finish.set()
        if task:
            await asyncio.wait_for(task, 3)
        else:
            await asyncio.gather(*list(manager._tasks.values()))
    with get_session() as db:
        assert read_metadata(db, "session_metadata:1", SessionMetadata).ended_at is not None
    requests = platform["requests"]
    await asyncio.sleep(0.04)
    assert platform["requests"] == requests


@pytest.mark.parametrize("running,fail", [(True, False), (False, True), (False, False)])
async def test_daily_schedule_has_one_successor_after_each_outcome(
    platform: dict[str, object], monkeypatch: pytest.MonkeyPatch, running: bool, fail: bool
) -> None:
    from collections import deque

    from app.web import main, service
    from app.web.services.schedules import complete_schedule_occurrence

    monkeypatch.setattr("app.web.services.notifications._NOTIFICATIONS", deque(maxlen=200))
    with get_session() as db:
        db.add(
            RecordingSchedule(id=1, room_id=1, scheduled_at=datetime.now(UTC) - timedelta(days=3), recurrent="daily")
        )
    calls: list[int] = []

    async def start(room_id: int, *, pipeline: bool, produce: bool) -> None:
        calls.append(room_id)
        if fail:
            raise ValueError("未授权")

    monkeypatch.setattr(service.recorder_manager, "is_running", lambda _id: running)
    monkeypatch.setattr(service.recorder_manager, "start", start)
    await main._run_due_schedules()
    await main._run_due_schedules()
    complete_schedule_occurrence(1)
    with get_session() as db:
        rows = db.exec(select(RecordingSchedule).order_by(RecordingSchedule.id)).all()
        assert len(rows) == 2 and rows[0].triggered and not rows[1].triggered
        assert rows[1].scheduled_at.replace(tzinfo=UTC) > datetime.now(UTC)
        assert len(db.exec(select(SystemLog).where(SystemLog.event == "schedule:1")).all()) == 1
    assert len(calls) == int(not running)


async def test_arm_auto_explicitly_resumes_but_plain_switch_keeps_pause(platform: dict[str, object]) -> None:
    from app.analysis.room_config import load_room_config
    from app.pipeline.live_monitor import LiveMonitor
    from app.web.services.rooms import RecorderManager, update_room

    manager = RecorderManager()
    manager._set_recording_flags(1, paused=True, suppress_auto_restart=True)
    update_room(1, {"auto_record": True, "auto_analyze": True})
    with get_session() as db:
        assert load_room_config(db.get(LiveRoom, 1))["recording_paused"]
    await manager.arm_auto_recording(1)
    with get_session() as db:
        room = db.get(LiveRoom, 1)
        assert room.auto_record and room.auto_analyze
        assert not load_room_config(room)["recording_paused"]
        assert LiveMonitor().room_status(room, running=False, runtime_state="idle")["state"] == "waiting_live"


async def test_cancelled_start_releases_lock_without_creating_session(
    platform: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.web.services.rooms import RecorderManager

    requested = asyncio.Event()
    original = bili_source.BilibiliLiveClient.get_room_info

    async def wait_response(self: BilibiliLiveClient, *args: object, **kwargs: object) -> None:
        requested.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bili_source.BilibiliLiveClient, "get_room_info", wait_response)
    manager = RecorderManager()
    starting = asyncio.create_task(manager.start(1, pipeline=False))
    await asyncio.wait_for(requested.wait(), 1)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert not manager.is_running(1)
    assert not manager._controls[1].locked()
    with get_session() as db:
        assert not db.exec(select(RecordingSession)).all()
    monkeypatch.setattr(bili_source.BilibiliLiveClient, "get_room_info", original)
    await metadata.refresh_room_metadata(1)
    with get_session() as db:
        assert db.get(LiveRoom, 1).title == "标题 A"


async def test_monitor_failure_keeps_last_successful_check_visible(platform: dict[str, object]) -> None:
    from app.pipeline.live_monitor import LiveMonitor

    with get_session() as db:
        room = db.get(LiveRoom, 1)
        room.auto_record = True
        db.add(room)
    platform["live"] = 0
    monitor = LiveMonitor()
    monitor._stop = asyncio.Event()
    await monitor._check_all()
    with get_session() as db:
        room = db.get(LiveRoom, 1)
    healthy = monitor.room_status(room, running=False, runtime_state="idle")
    assert healthy["state"] == "waiting_live" and healthy["last_checked_at"]
    platform["room_id"] = 999
    await monitor._check_all()
    failed = monitor.room_status(room, running=False, runtime_state="idle")
    assert failed["state"] == "error" and failed["error"]
    assert failed["last_checked_at"] == healthy["last_checked_at"]
