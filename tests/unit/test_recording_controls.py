"""录制停止状态、人工暂停和直播打点测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


@pytest.fixture(autouse=True)
def _isolate_notifications() -> Iterator[None]:
    """避免人工打点通知泄漏到其他测试。"""
    from app.web.services import notifications

    notifications._NOTIFICATIONS.clear()  # noqa: SLF001
    yield
    notifications._NOTIFICATIONS.clear()  # noqa: SLF001


class _FakeRecorder:
    """供 RecorderManager 生命周期测试使用的最小录制器。"""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self.stop_event = asyncio.Event()
        self.force_called = False
        self.failures: list[str] = []

    def stop(self) -> None:
        self.stop_event.set()

    def force_stop(self) -> None:
        self.force_called = True
        self.stop_event.set()

    def fail(self, message: str) -> None:
        self.failures.append(message)

    async def run(self) -> None:
        await self.stop_event.wait()


@pytest.mark.asyncio
async def test_start_uses_pipeline_default_and_enables_room_analysis(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    """Web 默认 Pipeline 开启时必须同步 auto_analyze，否则片段回调会被跳过。"""
    from app.db.models import LiveRoom
    from app.db.session import get_session
    from app.web.services import rooms

    with get_session() as db:
        room = LiveRoom(input_url="pipeline-default", room_id=202, authorized=True, auto_analyze=False)
        db.add(room)
        db.flush()
        room_id = room.id
    assert room_id is not None

    callback_args: dict[str, object] = {}

    def fake_callback(**kwargs: object) -> object:
        callback_args.update(kwargs)
        return object()

    class StartRecorder(_FakeRecorder):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__(session_id=77)

    monkeypatch.setattr(rooms.settings_store, "recording_pipeline_enabled", lambda: True)
    monkeypatch.setattr("app.pipeline.orchestrator.make_pipeline_callback", fake_callback)
    monkeypatch.setattr(rooms, "Recorder", StartRecorder)

    manager = rooms.RecorderManager()
    await manager.start(room_id, pipeline=None, produce=False)
    try:
        with get_session() as db:
            updated = db.get(LiveRoom, room_id)
            assert updated is not None
            assert updated.auto_analyze is True
        assert manager.status(room_id)["pipeline_enabled"] is True
        assert callback_args == {"produce": False, "room_id": room_id}
    finally:
        await manager.stop(room_id, mode="force")


def _seed_room_session(tmp_path: Path) -> tuple[int, int, datetime]:
    """创建房间和活动会话。"""
    from app.db.models import LiveRoom, RecordingSession, SessionStatus
    from app.db.session import get_session

    now = datetime.now(UTC).replace(microsecond=0)
    with get_session() as db:
        room = LiveRoom(input_url="control", room_id=200, authorized=True, auto_record=True, enabled=True)
        db.add(room)
        db.flush()
        session = RecordingSession(
            room_id=room.id,
            status=SessionStatus.RECORDING,
            started_at=now - timedelta(seconds=30),
        )
        db.add(session)
        db.flush()
        room_id = room.id
        session_id = session.id
    assert room_id is not None
    assert session_id is not None
    return room_id, session_id, now


def test_dashboard_uses_shared_recording_runtime(temp_db: None) -> None:
    """仪表盘必须读取录制控制 API 使用的同一份运行时状态。"""
    from app.db.models import LiveRoom, SessionStatus
    from app.db.session import get_session
    from app.web import service
    from app.web.services.rooms import recorder_manager

    with get_session() as db:
        room = LiveRoom(input_url="dashboard-runtime", room_id=201, authorized=True)
        db.add(room)
        db.flush()
        room_id = room.id
    assert room_id is not None

    recorder_manager._set_state(room_id, SessionStatus.RECORDING, session_id=42)  # noqa: SLF001
    try:
        payload = service.dashboard_state()
        room_payload = next(item for item in payload["rooms"] if item["id"] == room_id)
        assert room_payload["running"] is False
        assert room_payload["recording_state"] == SessionStatus.RECORDING
        assert room_payload["active_session_id"] == 42
    finally:
        recorder_manager._runtime.pop(room_id, None)  # noqa: SLF001


@pytest.mark.asyncio
async def test_graceful_stop_persists_pause_and_cancels_pending(
    temp_db: None,
    tmp_path: Path,
) -> None:
    """人工停止完成收尾、暂停自动拉起并按需取消下游任务。"""
    from sqlmodel import select

    from app.analysis.room_config import load_room_config
    from app.db.models import LiveRoom, RawSegment, RecordingSession, SegmentTask, SessionStatus, TaskStatus
    from app.db.session import get_session
    from app.web.services.rooms import RecorderManager

    room_id, session_id, now = _seed_room_session(tmp_path)
    media_path = tmp_path / "segment.ts"
    media_path.touch()
    with get_session() as db:
        segment = RawSegment(
            session_id=session_id,
            seq=0,
            file_path=str(media_path),
            start_ts=now - timedelta(seconds=20),
            end_ts=now,
            duration_s=20,
        )
        db.add(segment)
        db.flush()
        db.add(
            SegmentTask(
                segment_id=segment.id,
                session_id=session_id,
                stage=TaskStatus.QUEUED_FOR_TRANS,
                pipeline_key=f"pipeline:{segment.id}",
            )
        )

    manager = RecorderManager()
    recorder = _FakeRecorder(session_id)
    manager._recorders[room_id] = recorder  # type: ignore[assignment]  # noqa: SLF001
    manager._tasks[room_id] = asyncio.create_task(recorder.run())  # noqa: SLF001

    result = await manager.stop(
        room_id,
        mode="graceful",
        pause_auto_restart=True,
        mark_paused=True,
        cancel_pending=True,
    )

    assert result == {
        "state": SessionStatus.PAUSED,
        "session_id": session_id,
        "forced": False,
        "cancelled_tasks": 1,
    }
    assert manager.status(room_id)["state"] == SessionStatus.PAUSED
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        session = db.get(RecordingSession, session_id)
        task = db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).one()
        assert load_room_config(room)["recording_paused"] is True
        assert session.status == SessionStatus.PAUSED
        assert task.stage == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_manual_stop_suppresses_restart_without_showing_paused(temp_db: None, tmp_path: Path) -> None:
    """“停止并收尾”阻止监控器重启，但会话与界面均应显示已停止。"""
    from app.analysis.room_config import load_room_config
    from app.db.models import LiveRoom, RecordingSession, SessionStatus
    from app.db.session import get_session
    from app.web.services.rooms import RecorderManager

    room_id, session_id, _ = _seed_room_session(tmp_path)
    manager = RecorderManager()
    recorder = _FakeRecorder(session_id)
    manager._recorders[room_id] = recorder  # type: ignore[assignment]  # noqa: SLF001
    manager._tasks[room_id] = asyncio.create_task(recorder.run())  # noqa: SLF001

    result = await manager.stop(
        room_id,
        mode="graceful",
        pause_auto_restart=True,
        mark_paused=False,
    )

    assert result["state"] == SessionStatus.STOPPED
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        session = db.get(RecordingSession, session_id)
        room_config = load_room_config(room)
        assert room_config["recording_paused"] is False
        assert room_config["recording_auto_restart_suppressed"] is True
        assert session.status == SessionStatus.STOPPED


@pytest.mark.asyncio
async def test_force_stop_reports_forced_state(temp_db: None, tmp_path: Path) -> None:
    """强制停止会立即终止录制器并返回明确状态。"""
    from app.web.services.rooms import RecorderManager

    room_id, session_id, _ = _seed_room_session(tmp_path)
    manager = RecorderManager()
    recorder = _FakeRecorder(session_id)
    manager._recorders[room_id] = recorder  # type: ignore[assignment]  # noqa: SLF001
    manager._tasks[room_id] = asyncio.create_task(recorder.run())  # noqa: SLF001

    result = await manager.stop(room_id, mode="force")

    assert recorder.force_called is True
    assert result["forced"] is True
    assert result["state"] == "force_stopped"


@pytest.mark.asyncio
async def test_paused_stopping_session_is_not_auto_recovered(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """进程在人工停止期间退出时,重启不会把房间再次拉起。"""
    from app.analysis.room_config import merge_room_config
    from app.db.models import LiveRoom, RecordingSession, SessionStatus
    from app.db.session import get_session
    from app.web.services import rooms

    room_id, session_id, _ = _seed_room_session(tmp_path)
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        room.room_config_json = json.dumps(merge_room_config(room, {"recording_paused": True}), ensure_ascii=False)
        session = db.get(RecordingSession, session_id)
        session.status = SessionStatus.STOPPING
        db.add(room)
        db.add(session)

    async def unexpected_start(*args: object, **kwargs: object) -> None:
        raise AssertionError("人工暂停房间不应自动恢复")

    monkeypatch.setattr(rooms.recorder_manager, "start", unexpected_start)
    recovered = await rooms.auto_recover_interrupted_sessions()

    assert recovered == []
    with get_session() as db:
        assert db.get(RecordingSession, session_id).status == SessionStatus.PAUSED


@pytest.mark.asyncio
async def test_manual_marker_is_persisted_and_clamped_to_media(temp_db: None, tmp_path: Path) -> None:
    """直播打点生成待审候选,会话结束时按真实媒体终点收敛。"""
    from sqlmodel import select

    from app.db.models import HighlightCandidate, HighlightEvent, RawSegment
    from app.db.session import get_session
    from app.web.services.rooms import RecorderManager, _finalize_manual_markers

    room_id, session_id, now = _seed_room_session(tmp_path)
    manager = RecorderManager()
    recorder = _FakeRecorder(session_id)
    manager._recorders[room_id] = recorder  # type: ignore[assignment]  # noqa: SLF001
    manager._tasks[room_id] = asyncio.create_task(recorder.run())  # noqa: SLF001

    marker = manager.mark_highlight(
        room_id,
        pre_roll_s=20,
        post_roll_s=40,
        note="五杀",
    )
    with get_session() as db:
        candidate = db.get(HighlightCandidate, marker["candidate_id"])
        event = db.get(HighlightEvent, marker["event_id"])
        assert candidate.reason == "人工打点: 五杀"
        assert event.review_by == "manual_marker"
        metadata = json.loads(candidate.features_json)
        assert metadata["state"] == "waiting_for_media"

        media_path = tmp_path / "manual-marker.ts"
        media_path.touch()
        db.add(
            RawSegment(
                session_id=session_id,
                seq=0,
                file_path=str(media_path),
                start_ts=now - timedelta(seconds=30),
                end_ts=now + timedelta(seconds=5),
                duration_s=35,
            )
        )

    _finalize_manual_markers(session_id)
    with get_session() as db:
        candidate = db.get(HighlightCandidate, marker["candidate_id"])
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate.id)).one()
        assert candidate.end_ts == (now + timedelta(seconds=5)).replace(tzinfo=None)
        assert event.adjusted_end_ts == candidate.end_ts
        assert json.loads(candidate.features_json)["state"] == "ready"

    recorder.stop()
    await manager._tasks[room_id]  # noqa: SLF001


def test_recorder_force_stop_kills_active_process(monkeypatch: MonkeyPatch) -> None:
    """Recorder 强制停止会终止活动 FFmpeg 进程。"""
    from app.recording.recorder import Recorder

    class _Process:
        returncode = None

        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    recorder = Recorder(room_id=1, db_room_id=1)
    process = _Process()
    recorder._active_process = process  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(recorder, "_update_session", lambda **kwargs: None)

    recorder.force_stop()

    assert process.killed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie", ["", "SESSDATA=valid; DedeUserID=456"])
async def test_recorder_passes_cookie_to_fallback_capable_danmaku_client(
    monkeypatch: MonkeyPatch,
    cookie: str,
) -> None:
    """录制器应传入当前 Cookie；无 Cookie 时仍必须启动匿名采集。"""
    from app.recording import recorder as recorder_module
    from app.sources.bilibili import danmaku as danmaku_module

    created: list[tuple[int, int, str]] = []

    class _FallbackDanmakuClient:
        def __init__(self, room_id: int, session_id: int, cookie: str) -> None:
            created.append((room_id, session_id, cookie))

        async def run(self) -> None:
            return

        def stop(self) -> None:
            return

    monkeypatch.setattr(recorder_module.settings, "collect_danmaku", True)
    monkeypatch.setattr(recorder_module, "get_bilibili_cookie", lambda: cookie)
    monkeypatch.setattr(danmaku_module, "DanmakuClient", _FallbackDanmakuClient)

    recorder = recorder_module.Recorder(room_id=856077, db_room_id=1)
    recorder._session_id = 9  # noqa: SLF001
    recorder._start_danmaku()  # noqa: SLF001
    await asyncio.sleep(0)

    assert created == [(856077, 9, cookie)]
    assert recorder._danmaku_task is not None  # noqa: SLF001
    await recorder._stop_danmaku()  # noqa: SLF001


def test_reconnect_budget_honors_count_time_and_reset() -> None:
    """连续重试预算应按次数或时长耗尽，并在成功后完全归零。"""
    from app.recording.recorder import _ReconnectBudget

    budget = _ReconnectBudget()
    budget.record_failure(10.0)
    assert budget.exhaustion_reason(20.0, max_attempts=2, max_elapsed_s=30) is None

    budget.record_failure(20.0)
    assert "2/2 次" in (budget.exhaustion_reason(20.0, max_attempts=2, max_elapsed_s=30) or "")

    budget.reset()
    budget.begin(100.0)
    assert budget.exhaustion_reason(129.9, max_attempts=0, max_elapsed_s=30) is None
    assert "30.0/30 秒" in (budget.exhaustion_reason(130.0, max_attempts=0, max_elapsed_s=30) or "")


@pytest.mark.asyncio
async def test_recorder_auto_stops_after_consecutive_retry_limit(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """持续无流达到次数上限后应正常收尾，而不是无限轮询。"""
    from app.pipeline import storage_lifecycle
    from app.recording import recorder as recorder_module

    class _ClientContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    recorder = recorder_module.Recorder(room_id=23771139, db_room_id=1)
    updates: list[dict[str, object]] = []
    fetch_calls = 0

    def create_session() -> int:
        return 91

    def update_session(**kwargs: object) -> None:
        updates.append(kwargs)

    async def fetch_stream(_client: object) -> None:
        nonlocal fetch_calls
        fetch_calls += 1
        return None

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(recorder_module, "BilibiliLiveClient", lambda **_kwargs: _ClientContext())
    monkeypatch.setattr(recorder_module, "session_raw_dir", lambda _session_id: tmp_path)
    monkeypatch.setattr(storage_lifecycle, "should_stop_recording", lambda: False)
    monkeypatch.setattr(recorder_module.settings, "collect_danmaku", False)
    monkeypatch.setattr(recorder_module.settings, "recording_reconnect_max_attempts", 3)
    monkeypatch.setattr(recorder_module.settings, "recording_reconnect_max_elapsed_s", 0)
    monkeypatch.setattr(recorder, "_create_session", create_session)
    monkeypatch.setattr(recorder, "_update_session", update_session)
    monkeypatch.setattr(recorder, "_fetch_stream", fetch_stream)
    monkeypatch.setattr(recorder, "_sleep_or_stop", no_wait)

    await recorder.run()

    assert fetch_calls == 3
    assert any("3/3 次" in str(update.get("error_message", "")) for update in updates)
    assert [update.get("status") for update in updates[-2:]] == ["finalizing", "stopped"]


@pytest.mark.asyncio
async def test_recorder_resets_retry_limit_after_productive_reconnect(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """重连后产出片段应清零旧失败，下一次断流获得完整重试预算。"""
    from types import SimpleNamespace

    from app.pipeline import storage_lifecycle
    from app.recording import recorder as recorder_module

    class _ClientContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    recorder = recorder_module.Recorder(room_id=23771139, db_room_id=1)
    stream = SimpleNamespace(url="https://example.invalid/live.m3u8", protocol="hls", quality=10000)
    streams: list[object | None] = [None, stream, None, None]
    updates: list[dict[str, object]] = []

    def create_session() -> int:
        return 92

    def update_session(**kwargs: object) -> None:
        updates.append(kwargs)

    async def fetch_stream(_client: object) -> object | None:
        return streams.pop(0)

    async def record_once(_stream: object, _out_dir: Path) -> int:
        recorder._seq += 1  # noqa: SLF001
        return 1

    async def no_wait(_seconds: float) -> None:
        return None

    def classify_exit(_exit_code: int, _stderr_tail: str | None) -> None:
        return None

    def increment_reconnect() -> None:
        return None

    monkeypatch.setattr(recorder_module, "BilibiliLiveClient", lambda **_kwargs: _ClientContext())
    monkeypatch.setattr(recorder_module, "session_raw_dir", lambda _session_id: tmp_path)
    monkeypatch.setattr(storage_lifecycle, "should_stop_recording", lambda: False)
    monkeypatch.setattr(recorder_module.settings, "collect_danmaku", False)
    monkeypatch.setattr(recorder_module.settings, "recording_reconnect_max_attempts", 2)
    monkeypatch.setattr(recorder_module.settings, "recording_reconnect_max_elapsed_s", 0)
    monkeypatch.setattr(recorder, "_create_session", create_session)
    monkeypatch.setattr(recorder, "_update_session", update_session)
    monkeypatch.setattr(recorder, "_fetch_stream", fetch_stream)
    monkeypatch.setattr(recorder, "_record_once", record_once)
    monkeypatch.setattr(recorder, "_classify_recording_exit", classify_exit)
    monkeypatch.setattr(recorder, "_increment_reconnect", increment_reconnect)
    monkeypatch.setattr(recorder, "_sleep_or_stop", no_wait)

    await recorder.run()

    assert streams == []
    assert any("2/2 次" in str(update.get("error_message", "")) for update in updates)


@pytest.mark.asyncio
async def test_manager_cleans_up_naturally_finished_recorder(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """录制器自行结束后应移除运行任务、关闭房间标记并恢复网感采集。"""
    from app.db.models import LiveRoom
    from app.db.session import get_session
    from app.trends.scheduler import trend_scheduler
    from app.web.services.rooms import RecorderManager

    room_id, session_id, _ = _seed_room_session(tmp_path)
    recorder = _FakeRecorder(session_id)
    recorder.stop_event.set()
    resumed: list[bool] = []

    def resume_after_recording() -> None:
        resumed.append(True)

    monkeypatch.setattr(trend_scheduler, "resume_after_recording", resume_after_recording)
    manager = RecorderManager()
    manager._recorders[room_id] = recorder  # type: ignore[assignment]  # noqa: SLF001
    task = asyncio.create_task(manager._run_recorder(room_id, recorder))  # type: ignore[arg-type]  # noqa: SLF001
    manager._tasks[room_id] = task  # noqa: SLF001

    await task

    assert room_id not in manager._recorders  # noqa: SLF001
    assert room_id not in manager._tasks  # noqa: SLF001
    assert resumed == [True]
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        assert room is not None
        assert room.enabled is False


@pytest.mark.asyncio
async def test_retry_exhaustion_waits_for_a_real_offline_transition(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """取流预算耗尽后不得立即拉起，只有确认离线后才允许下一次开播。"""
    from types import SimpleNamespace

    from app.analysis.room_config import load_room_config, merge_room_config
    from app.db.models import LiveRoom
    from app.db.session import get_session
    from app.pipeline import live_monitor as live_monitor_module
    from app.web import service as service_module
    from app.web.services.rooms import RecorderManager

    room_id, session_id, _ = _seed_room_session(tmp_path)
    recorder = _FakeRecorder(session_id)
    recorder.retry_budget_exhausted = True
    recorder.stop_event.set()
    manager = RecorderManager()
    task = asyncio.create_task(manager._run_recorder(room_id, recorder))  # type: ignore[arg-type]  # noqa: SLF001
    manager._tasks[room_id] = task  # noqa: SLF001
    manager._recorders[room_id] = recorder  # type: ignore[assignment]  # noqa: SLF001
    await task

    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        assert room is not None
        assert load_room_config(room)["recording_wait_for_next_live"] is True
        room.auto_record = True
        room.enabled = True
        room.room_config_json = json.dumps(
            merge_room_config(room, {"recording_wait_for_next_live": True}),
            ensure_ascii=False,
        )
        db.add(room)

    live_state = {"value": 1}

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get_room_info(self, _room_id: str, *, include_detail: bool) -> SimpleNamespace:
            assert isinstance(include_detail, bool)
            return SimpleNamespace(live_status=live_state["value"], title="测试直播", uploader_name="测试主播")

    starts: list[int] = []

    async def fake_start(db_id: int, _auto_analyze: bool, _auto_render: bool) -> None:
        starts.append(db_id)

    monkeypatch.setattr(live_monitor_module, "BilibiliLiveClient", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(live_monitor_module, "get_bilibili_cookie", lambda: "")
    monkeypatch.setattr(service_module, "recorder_manager", manager)
    monitor = live_monitor_module.LiveMonitor()
    monitor._stop = asyncio.Event()  # noqa: SLF001
    monkeypatch.setattr(monitor, "_start_recording", fake_start)

    await monitor._check_all()  # noqa: SLF001
    assert starts == []
    live_state["value"] = 0
    await monitor._check_all()  # noqa: SLF001
    assert starts == []
    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        assert room is not None
        assert load_room_config(room)["recording_wait_for_next_live"] is False

    live_state["value"] = 1
    await monitor._check_all()  # noqa: SLF001
    assert starts == [room_id]
