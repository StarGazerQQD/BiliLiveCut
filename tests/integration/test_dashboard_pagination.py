from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert
from sqlalchemy.engine import Connection, ExecutionContext
from sqlmodel import Session

from app.db.entities import AppSetting, Danmaku, LiveRoom, RawSegment, RecordingSession, SegmentStatus
from app.db.session import get_session
from app.plugins.live_source import DanmakuStatus
from app.recording.danmaku_evidence import CaptureInterval, DanmakuEvidence
from app.web.services.dashboard import danmaku_overview, pipeline_progress

_START = datetime(2026, 10, 25, tzinfo=UTC)


def _sessions(count: int, *, local: bool = False) -> None:
    with get_session() as db:
        db.add(LiveRoom(id=1, input_url="pagination", room_id=1, platform="local" if local else "bilibili"))
        db.add_all([RecordingSession(id=index, room_id=1) for index in range(1, count + 1)])


def test_pages_keep_global_counts_old_evidence_and_orphan_sources(temp_db: None) -> None:
    _sessions(30)
    with get_session() as db:
        evidence = DanmakuEvidence(
            status=DanmakuStatus.FAILED,
            intervals=[CaptureInterval(start=_START, end=_START + timedelta(seconds=30))],
        )
        db.add(AppSetting(key="session_danmaku:1", value=evidence.model_dump_json()))
        db.add_all([Danmaku(session_id=sid, room_id=1, content=str(sid)) for sid in [30, 31]])
    first = danmaku_overview(limit=1, session_limit=12)
    assert first["available"] is True and first["total"] == 2 and len(first["recent"]) == 1
    assert [item["session_id"] for item in first["sessions"]] == list(range(31, 19, -1))
    assert first["sessions"][0]["source_label"] == "未知来源"
    assert first["session_pagination"] == {"offset": 0, "limit": 12, "total": 31, "has_more": True}
    last = danmaku_overview(limit=0, session_limit=12, session_offset=24)
    assert [item["session_id"] for item in last["sessions"]] == list(range(7, 0, -1))
    assert last["available"] is True and last["total"] == 2 and not last["session_pagination"]["has_more"]
    assert last["sessions"][-1]["count"] == 0 and last["sessions"][-1]["intensity"] == 0
    assert last["sessions"][-1]["evidence"]["state"] == "failed"
    empty = danmaku_overview(session_limit=12, session_offset=48)
    assert empty["sessions"] == [] and empty["available"] is True and empty["total"] == 2
    scoped = danmaku_overview(session_id=1)
    assert scoped["total"] == 0 and scoped["available"] is True
    assert [item["session_id"] for item in scoped["sessions"]] == [1]


@pytest.mark.parametrize("capture", [None, "", "{broken", "empty", "failed_with_interval"])
@pytest.mark.parametrize("local_value", [True, False, 1, "true"])
def test_batch_evidence_preserves_capture_priority_and_explicit_local_flag(
    temp_db: None, capture: str | None, local_value: bool | int | str
) -> None:
    _sessions(2, local=True)
    with get_session() as db:
        db.add(AppSetting(key="local_source:1", value=json.dumps({"has_comments": local_value})))
        if capture is not None:
            value = capture
            if capture in {"empty", "failed_with_interval"}:
                value = DanmakuEvidence(
                    status=DanmakuStatus.FAILED,
                    intervals=[CaptureInterval(start=_START)] if capture == "failed_with_interval" else [],
                ).model_dump_json()
            db.add(AppSetting(key="session_danmaku:1", value=value))
    expected = capture == "failed_with_interval" or (
        capture not in {"empty", "failed_with_interval"} and local_value is True
    )
    page = danmaku_overview(session_limit=1)
    assert page["sessions"][0]["session_id"] == 2
    assert page["available"] is expected
    old = danmaku_overview(session_id=1)
    assert old["available"] is expected and old["sessions"][0]["evidence"]["available"] is expected


def test_corrupt_local_metadata_is_not_silently_treated_as_no_comments(temp_db: None) -> None:
    _sessions(2, local=True)
    with get_session() as db:
        db.add(AppSetting(key="local_source:1", value="{broken"))
    with pytest.raises(json.JSONDecodeError):
        danmaku_overview(session_limit=1)


def test_paged_weights_keep_primary_key_order_for_interleaved_canceling_values(temp_db: None) -> None:
    entries = [(index + 1, 1 + index % 3, value) for index, value in enumerate([1e16, 0.1, -1e16, 0.125, 0.375] * 60)]
    expected: dict[int, float] = {}
    with get_session() as db:
        for row_id, sid, value in reversed(entries):
            db.add(Danmaku(id=row_id, session_id=sid, room_id=1, value=value))
        for _, sid, value in entries:
            expected[sid] = expected.get(sid, 0.0) + float(value)
    for offset in range(3):
        page = danmaku_overview(limit=0, session_limit=1, session_offset=offset)
        entry = page["sessions"][0]
        assert entry["session_id"] == 3 - offset
        assert entry["count"] == 100 and entry["intensity"] == round(expected[3 - offset], 2)
        assert danmaku_overview(session_id=3 - offset)["sessions"][0]["intensity"] == entry["intensity"]


def test_empty_progress_and_missing_session_keep_zero_counts(temp_db: None) -> None:
    assert pipeline_progress() == {
        "total_segments": 0,
        "recorded": 0,
        "transcribed": 0,
        "scored": 0,
        "progress_pct": 0,
        "active_session_id": None,
    }
    assert pipeline_progress(999)["total_segments"] == 0


@pytest.mark.parametrize("scale", [1000, 100000])
def test_dashboard_large_history_keeps_loaded_objects_and_connections_bounded(temp_db: None, scale: int) -> None:
    from app.db.session import engine

    _sessions(200)
    states = [SegmentStatus.RECORDED, SegmentStatus.TRANSCRIBED, SegmentStatus.SCORED, SegmentStatus.ARCHIVED, "other"]
    with get_session() as db:
        db.execute(
            insert(Danmaku),
            [
                {"session_id": index % 200 + 1, "room_id": 1, "content": str(index), "value": 1.0}
                for index in range(scale)
            ],
        )
        db.execute(
            insert(RawSegment),
            [
                {"session_id": index % 2 + 1, "seq": index, "file_path": "unused", "status": states[index % 5]}
                for index in range(scale)
            ],
        )
    loaded: list[type[object]] = []
    connections: list[object] = []
    statements: list[str] = []

    def observe_load(db: Session, instance: object) -> None:
        loaded.append(type(instance))

    def observe_connection(connection: object, record: object, proxy: object) -> None:
        connections.append(connection)

    def observe_query(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(Session, "loaded_as_persistent", observe_load)
    event.listen(engine, "checkout", observe_connection)
    event.listen(engine, "before_cursor_execute", observe_query)
    try:
        progress = pipeline_progress()
        scoped = pipeline_progress(1)
        page = danmaku_overview(limit=7, session_limit=12)
    finally:
        event.remove(Session, "loaded_as_persistent", observe_load)
        event.remove(engine, "checkout", observe_connection)
        event.remove(engine, "before_cursor_execute", observe_query)
    assert progress == {
        "total_segments": scale,
        "recorded": scale // 5,
        "transcribed": scale // 5,
        "scored": scale // 5,
        "progress_pct": 20.0,
        "active_session_id": None,
    }
    assert scoped["total_segments"] == scale // 2 and scoped["scored"] == scale // 10
    assert page["total"] == scale and len(page["sessions"]) == 12
    assert all(item["count"] == scale // 200 and item["intensity"] == scale // 200 for item in page["sessions"])
    assert loaded.count(RawSegment) == 0 and loaded.count(Danmaku) == 7
    assert loaded.count(RecordingSession) <= 19
    assert len(connections) == 3 and len(statements) < 70


@pytest.mark.parametrize("query", ["session_limit=0", "session_limit=201", "session_offset=-1"])
def test_danmaku_api_rejects_invalid_session_pagination(temp_db: None, query: str) -> None:
    from app.web.main import app

    with TestClient(app) as client:
        assert client.get(f"/api/danmaku?{query}").status_code == 422
