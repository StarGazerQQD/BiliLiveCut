from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import select

from app.db.entities import HighlightCandidate, HighlightEvent, LiveRoom, SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.approval import approve_event_and_task


def seed_automatic_task(
    media: Path, monkeypatch: pytest.MonkeyPatch, *, stage: str = TaskStatus.AWAITING_REVIEW
) -> int:
    """真实评分、编辑与审批链生成完整来源；仅替换模型 HTTP 边界。"""
    from app.pipeline.workers.analyze import score_hotspot_event
    from tests.unit.test_editorial_review import _seed, _service

    hotspot_id, segment_id = _seed(media, heat=1.0)
    with get_session() as db:
        room = db.exec(select(LiveRoom)).one()
        room.auto_approve = False
        room.auto_render = room.auto_upload = room.authorized = True
        room.auto_approve_threshold = room.auto_publish_threshold = 0.2
        db.add(room)
    _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(hotspot_id)
    assert candidate is not None
    with get_session() as db:
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate.id)).one()
        task = SegmentTask(
            segment_id=segment_id,
            session_id=candidate.session_id,
            candidate_id=candidate.id,
            event_id=event.id,
            stage=TaskStatus.AWAITING_REVIEW,
        )
        db.add(task)
        db.flush()
        task_id, event_id = task.id, event.id
    assert task_id is not None and event_id is not None
    if stage != TaskStatus.AWAITING_REVIEW:
        assert approve_event_and_task(task_id=task_id, event_id=event_id, source="auto")
        with get_session() as db:
            task = db.get(SegmentTask, task_id)
            assert db.get(HighlightCandidate, task.candidate_id).status == "approved"
            task.stage = stage
            db.add(task)
    return task_id
