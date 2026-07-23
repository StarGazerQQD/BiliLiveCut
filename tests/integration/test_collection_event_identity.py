"""合集链路必须区分 HighlightEvent.id 与 HighlightCandidate.id。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def test_collection_events_resolve_candidate_through_event(temp_db: None) -> None:
    """合集关联使用真实 event_id，不把它误当 candidate_id。"""
    from app.db.models import (
        HighlightCandidate,
        HighlightEvent,
        HighlightTopic,
        LiveRoom,
        RecordingSession,
        Topic,
    )
    from app.db.session import get_session
    from app.pipeline.collection import get_collection_events

    now = datetime.now(UTC).replace(microsecond=0)
    with get_session() as db:
        room = LiveRoom(input_url="collection", authorized=True)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id)
        db.add(session)
        db.flush()
        first = HighlightCandidate(
            session_id=session.id,
            peak_ts=now,
            start_ts=now - timedelta(seconds=10),
            end_ts=now + timedelta(seconds=10),
        )
        target = HighlightCandidate(
            session_id=session.id,
            peak_ts=now + timedelta(seconds=30),
            start_ts=now + timedelta(seconds=20),
            end_ts=now + timedelta(seconds=40),
            highlight_score=0.88,
        )
        db.add(first)
        db.add(target)
        db.flush()
        event = HighlightEvent(candidate_id=target.id, session_id=session.id)
        topic = Topic(session_id=session.id, title="identity")
        db.add(event)
        db.add(topic)
        db.flush()
        db.add(HighlightTopic(event_id=event.id, topic_id=topic.id, sort_order=0))
        event_id = event.id
        candidate_id = target.id
        topic_id = topic.id
    assert event_id is not None and candidate_id is not None and topic_id is not None
    assert event_id != candidate_id

    rows = get_collection_events(topic_id)

    assert rows[0]["event_id"] == event_id
    assert rows[0]["candidate_id"] == candidate_id
    assert rows[0]["score"] == 0.88
