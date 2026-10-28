from __future__ import annotations

import logging

import pytest

from app.db.entities import LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline import scheduler


@pytest.mark.parametrize("stage", [TaskStatus.RECORDED, TaskStatus.TRANSCRIBED, TaskStatus.AWAITING_REVIEW])
def test_waiting_logs_only_when_reason_counts_change(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    monkeypatch.setattr(scheduler, "_waiting_counts", {})
    advance = {
        TaskStatus.RECORDED: scheduler.advance_recorded,
        TaskStatus.TRANSCRIBED: scheduler.advance_transcribed,
        TaskStatus.AWAITING_REVIEW: scheduler.advance_awaiting_review,
    }[stage]
    with get_session() as db:
        room = LiveRoom(input_url="log-test", auto_analyze=False, auto_approve=False)
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id)
        db.add(session)
        db.flush()
        for index in range(20):
            segment = RawSegment(session_id=session.id, seq=index, file_path=f"segment-{index}.ts", status="recorded")
            db.add(segment)
            db.flush()
            db.add(SegmentTask(segment_id=segment.id, session_id=session.id, stage=stage))
    log = logging.getLogger(scheduler.__name__)
    monkeypatch.setattr(log, "propagate", False)
    log.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=scheduler.__name__):
            for _ in range(30):
                advance()
            records = [record for record in caplog.records if "scheduler_waiting" in record.message]
            assert len(records) == 1 and "20" in records[0].message
            with get_session() as db:
                task = db.get(SegmentTask, 1)
                assert task is not None and task.stage == stage
                task.stage = TaskStatus.COMPLETED
                db.add(task)
            advance()
            records = [record for record in caplog.records if "scheduler_waiting" in record.message]
            assert len(records) == 2 and "19" in records[-1].message
            if stage == TaskStatus.AWAITING_REVIEW:
                with get_session() as db:
                    room = db.get(LiveRoom, 1)
                    assert room is not None
                    room.auto_approve = True
                    room.auto_approve_threshold = 0
                    db.add(room)
                advance()
                advance()
                records = [record for record in caplog.records if "scheduler_waiting" in record.message]
                assert len(records) == 3 and records[-1].levelno == logging.WARNING
                assert "missing_event" in records[-1].message
                assert "sample_task_ids=(2, 3, 4)" in records[-1].message
    finally:
        log.removeHandler(caplog.handler)
