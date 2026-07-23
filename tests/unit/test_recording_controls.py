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
            room_id=room.id, status=SessionStatus.RECORDING, started_at=now - timedelta(seconds=30)
        )
        db.add(session)
        db.flush()
        room_id = room.id
        session_id = session.id
    assert room_id is not None
    assert session_id is not None
    return room_id, session_id, now


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
    manager._recorders[room_id] = recorder  # noqa: SLF001
    manager._tasks[room_id] = asyncio.create_task(recorder.run())  # noqa: SLF001

    result = await manager.stop(
        room_id,
        mode="graceful",
        pause_auto_restart=True,
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
async def test_force_stop_reports_forced_state(temp_db: None, tmp_path: Path) -> None:
    """强制停止会立即终止录制器并返回明确状态。"""
    from app.web.services.rooms import RecorderManager

    room_id, session_id, _ = _seed_room_session(tmp_path)
    manager = RecorderManager()
    recorder = _FakeRecorder(session_id)
    manager._recorders[room_id] = recorder  # noqa: SLF001
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
    manager._recorders[room_id] = recorder  # noqa: SLF001
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
