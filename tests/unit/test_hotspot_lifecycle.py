"""HotspotEvent 跨分段生命周期与代表弹幕回归测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlmodel import select

from app.analysis.hotspot_lifecycle import HotspotLifecycleConfig, reconcile_hotspot_events
from app.analysis.timeline import datetime_epoch, select_representative_danmaku
from app.db.entities import (
    Danmaku,
    HotspotEvent,
    HotspotStatus,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SessionStatus,
)
from app.db.session import get_session

_START = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
_CONFIG = HotspotLifecycleConfig(
    merge_gap_s=30.0,
    confirm_delay_s=60.0,
    semantic_overlap_threshold=0.20,
    recording_gap_tolerance_s=1.0,
)


def _seed_session(*, status: str = SessionStatus.RECORDING) -> int:
    with get_session() as db:
        room = LiveRoom(input_url="hotspot-lifecycle", room_id=8001, auto_analyze=True)
        db.add(room)
        db.flush()
        assert room.id is not None
        session = RecordingSession(room_id=room.id, status=status, started_at=_START)
        db.add(session)
        db.flush()
        assert session.id is not None
        return session.id


def _add_segment(session_id: int, seq: int, start_ts: datetime, end_ts: datetime) -> None:
    with get_session() as db:
        db.add(
            RawSegment(
                session_id=session_id,
                seq=seq,
                file_path=f"segment-{seq}.ts",
                start_ts=start_ts,
                end_ts=end_ts,
                duration_s=(end_ts - start_ts).total_seconds(),
            )
        )


def _payload(
    session_id: int,
    event_key: str,
    start_ts: datetime,
    end_ts: datetime,
    *,
    text: str,
    signal: str = "audio",
    heat_score: float = 0.8,
) -> dict[str, object]:
    peak_ts = start_ts + (end_ts - start_ts) / 2
    evidence = {
        "version": 1,
        "items": [
            {
                "type": signal,
                "start_ts": start_ts.isoformat(),
                "end_ts": end_ts.isoformat(),
                "metrics": {"score": heat_score},
                "excerpts": [text],
            }
        ],
    }
    return {
        "event_key": event_key,
        "session_id": session_id,
        "start_ts": start_ts,
        "peak_ts": peak_ts,
        "end_ts": end_ts,
        "heat_score": heat_score,
        "clip_score": heat_score - 0.1,
        "semantic_confidence": 0.5,
        "evidence_coverage": 0.7,
        "features_json": json.dumps({"detector_version": "test", "ticks": [{"peak": peak_ts.isoformat()}]}),
        "evidence_json": json.dumps(evidence, ensure_ascii=False),
        "transcript_text": text,
    }


def test_same_event_crossing_contiguous_segments_merges_to_one_active_event(temp_db: None) -> None:
    session_id = _seed_session()
    boundary = _START + timedelta(minutes=5)
    _add_segment(session_id, 0, _START, boundary)
    _add_segment(session_id, 1, boundary, boundary + timedelta(minutes=5))
    first = _payload(
        session_id,
        "cross-segment:first",
        boundary - timedelta(seconds=20),
        boundary,
        text="主播宣布周末挑战新规则",
    )
    second = _payload(
        session_id,
        "cross-segment:second",
        boundary,
        boundary + timedelta(seconds=25),
        text="周末挑战新规则正式开始",
        heat_score=0.9,
    )

    with get_session() as db:
        first_ids = reconcile_hotspot_events(
            db,
            [first],
            expected_session_id=session_id,
            observed_through=boundary,
            config=_CONFIG,
        )
    with get_session() as db:
        second_ids = reconcile_hotspot_events(
            db,
            [second],
            expected_session_id=session_id,
            observed_through=boundary + timedelta(seconds=25),
            config=_CONFIG,
        )
    with get_session() as db:
        events = list(
            db.exec(
                select(HotspotEvent).where(HotspotEvent.session_id == session_id).order_by(HotspotEvent.id.asc())
            ).all()
        )

    assert len(events) == 2
    active = [event for event in events if event.status != HotspotStatus.MERGED]
    aliases = [event for event in events if event.status == HotspotStatus.MERGED]
    first_start = first["start_ts"]
    second_end = second["end_ts"]
    assert isinstance(first_start, datetime)
    assert isinstance(second_end, datetime)
    assert len(active) == len(aliases) == 1
    assert first_ids == second_ids == [active[0].id]
    assert aliases[0].merged_into_id == active[0].id
    assert datetime_epoch(active[0].start_ts) == datetime_epoch(first_start)
    assert datetime_epoch(active[0].end_ts) == datetime_epoch(second_end)
    assert "主播宣布周末挑战新规则" in (active[0].transcript_text or "")
    assert "周末挑战新规则正式开始" in (active[0].transcript_text or "")
    assert len(json.loads(active[0].evidence_json or "{}")["items"]) == 2


def test_two_semantically_unrelated_hotspots_in_one_segment_stay_separate(temp_db: None) -> None:
    session_id = _seed_session()
    _add_segment(session_id, 0, _START, _START + timedelta(minutes=5))
    first = _payload(
        session_id,
        "same-segment:cat",
        _START + timedelta(seconds=60),
        _START + timedelta(seconds=80),
        text="小猫突然跳上桌子打翻水杯",
        signal="asr",
    )
    second = _payload(
        session_id,
        "same-segment:match",
        _START + timedelta(seconds=85),
        _START + timedelta(seconds=105),
        text="比赛进入最后决胜局比分反超",
        signal="asr",
    )

    with get_session() as db:
        ids = reconcile_hotspot_events(
            db,
            [first, second],
            expected_session_id=session_id,
            observed_through=_START + timedelta(seconds=105),
            config=_CONFIG,
        )
    with get_session() as db:
        events = db.exec(
            select(HotspotEvent).where(
                HotspotEvent.session_id == session_id,
                HotspotEvent.status != HotspotStatus.MERGED,
            )
        ).all()

    assert len(set(ids)) == 2
    assert len(events) == 2


def test_recording_gap_prevents_merge_even_when_semantics_match(temp_db: None) -> None:
    session_id = _seed_session()
    first_end = _START + timedelta(minutes=5)
    second_start = first_end + timedelta(seconds=20)
    _add_segment(session_id, 0, _START, first_end)
    _add_segment(session_id, 1, second_start, second_start + timedelta(minutes=5))
    first = _payload(
        session_id,
        "gap:first",
        first_end - timedelta(seconds=15),
        first_end,
        text="主播公布周末挑战规则",
    )
    second = _payload(
        session_id,
        "gap:second",
        second_start,
        second_start + timedelta(seconds=15),
        text="周末挑战规则继续说明",
    )

    with get_session() as db:
        reconcile_hotspot_events(
            db,
            [first],
            expected_session_id=session_id,
            observed_through=first_end,
            config=_CONFIG,
        )
    with get_session() as db:
        ids = reconcile_hotspot_events(
            db,
            [second],
            expected_session_id=session_id,
            observed_through=second_start + timedelta(seconds=15),
            config=_CONFIG,
        )
    with get_session() as db:
        active = db.exec(
            select(HotspotEvent).where(
                HotspotEvent.session_id == session_id,
                HotspotEvent.status != HotspotStatus.MERGED,
            )
        ).all()

    assert len(active) == 2
    second_event = next(event for event in active if event.event_key == "gap:second")
    assert ids == [second_event.id]


def test_extended_event_moves_through_enriching_before_confirmation(temp_db: None) -> None:
    session_id = _seed_session()
    _add_segment(session_id, 0, _START, _START + timedelta(minutes=5))
    initial = _payload(
        session_id,
        "lifecycle:extended",
        _START + timedelta(seconds=20),
        _START + timedelta(seconds=40),
        text="主播开始说明挑战规则",
    )
    extended = dict(initial)
    extended["end_ts"] = _START + timedelta(seconds=65)
    extended["peak_ts"] = _START + timedelta(seconds=50)
    extended["transcript_text"] = "主播继续说明挑战规则和奖励"

    with get_session() as db:
        reconcile_hotspot_events(
            db,
            [initial],
            expected_session_id=session_id,
            observed_through=_START + timedelta(seconds=40),
            config=_CONFIG,
        )
    with get_session() as db:
        reconcile_hotspot_events(
            db,
            [extended],
            expected_session_id=session_id,
            observed_through=_START + timedelta(seconds=65),
            config=_CONFIG,
        )
    with get_session() as db:
        event = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == "lifecycle:extended")).one()
        assert event.status == HotspotStatus.ENRICHING
        reconcile_hotspot_events(
            db,
            [],
            expected_session_id=session_id,
            observed_through=_START + timedelta(seconds=126),
            config=_CONFIG,
        )

    with get_session() as db:
        event = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == "lifecycle:extended")).one()
    assert event.status == HotspotStatus.CONFIRMED


def test_terminal_session_forces_last_event_confirmation(temp_db: None) -> None:
    session_id = _seed_session(status=SessionStatus.STOPPED)
    segment_end = _START + timedelta(seconds=120)
    _add_segment(session_id, 0, _START, segment_end)
    payload = _payload(
        session_id,
        "lifecycle:terminal",
        segment_end - timedelta(seconds=20),
        segment_end,
        text="主播结束本场直播",
    )

    with get_session() as db:
        reconcile_hotspot_events(
            db,
            [payload],
            expected_session_id=session_id,
            observed_through=segment_end,
            config=_CONFIG,
        )
    with get_session() as db:
        event = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == "lifecycle:terminal")).one()

    assert event.status == HotspotStatus.CONFIRMED


def test_empty_followup_confirms_event_and_saves_diverse_danmaku(temp_db: None) -> None:
    session_id = _seed_session()
    segment_end = _START + timedelta(seconds=180)
    _add_segment(session_id, 0, _START, segment_end)
    event_start = _START + timedelta(seconds=20)
    event_end = _START + timedelta(seconds=40)
    payload = _payload(
        session_id,
        "confirm:event",
        event_start,
        event_end,
        text="主播说明周六晚上挑战新模式",
    )
    receive_ts = event_start + timedelta(seconds=10, milliseconds=7500)
    messages = ["???"] * 8 + ["666"] * 7 + ["主播说明周六晚上挑战新模式"] * 3 + ["这波节目效果太好笑了"] * 2
    with get_session() as db:
        for message in messages:
            db.add(Danmaku(session_id=session_id, room_id=8001, ts=receive_ts, content=message))
        reconcile_hotspot_events(
            db,
            [payload],
            expected_session_id=session_id,
            observed_through=event_end,
            config=_CONFIG,
        )
    with get_session() as db:
        reconcile_hotspot_events(
            db,
            [],
            expected_session_id=session_id,
            observed_through=event_end + timedelta(seconds=61),
            config=_CONFIG,
        )
    with get_session() as db:
        event = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == "confirm:event")).one()

    representatives = json.loads(event.representative_danmaku_json or "[]")
    assert event.status == HotspotStatus.CONFIRMED
    assert {item["role"] for item in representatives} == {"reaction", "information", "humorous"}
    assert any(item["text"] == "主播说明周六晚上挑战新模式" for item in representatives)
    assert all(isinstance(item["count"], int) and item["count"] > 0 for item in representatives)


def test_representative_selection_does_not_let_two_reactions_crowd_out_context() -> None:
    messages = ["???"] * 10 + ["666"] * 9 + ["主播宣布周六晚上挑战新模式"] * 2

    first = select_representative_danmaku(messages, limit=2, include_role=True)
    second = select_representative_danmaku(messages, limit=2, include_role=True)

    assert first == second
    assert [item["role"] for item in first] == ["reaction", "information"]
    assert first[1]["text"] == "主播宣布周六晚上挑战新模式"
