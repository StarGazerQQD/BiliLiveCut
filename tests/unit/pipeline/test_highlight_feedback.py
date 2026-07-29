"""人工审核到高光插件反馈的宿主适配测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from app.db.models import (
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
)
from app.db.session import get_session
from app.pipeline.highlight_feedback import build_highlight_feedback, record_candidate_review_feedback
from app.plugins import HighlightFeedbackDispatch
from app.plugins.manager import plugin_manager

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _seed_plugin_candidate(*, valid_metadata: bool = True) -> int:
    started_at = datetime(2026, 1, 4, 12, 0, 0)
    with get_session() as db:
        room = LiveRoom(input_url="feedback", room_id=123)
        db.add(room)
        db.flush()
        assert room.id is not None
        recording = RecordingSession(room_id=room.id, started_at=started_at)
        db.add(recording)
        db.flush()
        assert recording.id is not None
        segment = RawSegment(
            session_id=recording.id,
            seq=0,
            file_path="feedback.mp4",
            start_ts=started_at + timedelta(seconds=60),
            end_ts=started_at + timedelta(seconds=90),
            duration_s=30.0,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        metadata = (
            {
                "highlight_plugin": {
                    "plugin_id": "bililivecut-highlight",
                    "prediction": {
                        "schema_version": "1.0.0",
                        "schema_fingerprint": "fingerprint",
                        "feature_values": {"duration_s": 30.0, "audio_prominence": None},
                    },
                }
            }
            if valid_metadata
            else {"highlight_plugin": {"plugin_id": "bililivecut-highlight", "prediction": {}}}
        )
        candidate = HighlightCandidate(
            session_id=recording.id,
            peak_ts=segment.start_ts + timedelta(seconds=15),
            start_ts=segment.start_ts,
            end_ts=segment.end_ts,
            highlight_score=0.9,
            features_json=json.dumps(metadata),
        )
        db.add(candidate)
        db.flush()
        assert candidate.id is not None
        db.add(
            HighlightEvent(
                candidate_id=candidate.id,
                session_id=recording.id,
                segment_id=segment.id,
            )
        )
        candidate_id = candidate.id
    return candidate_id


@pytest.mark.parametrize(
    ("decision", "expected_label"),
    [
        (ReviewStatus.APPROVED_SOLO, 1),
        (ReviewStatus.REJECTED, 0),
        (ReviewStatus.HOLD, None),
        (ReviewStatus.PENDING, None),
    ],
)
def test_build_feedback_maps_only_explicit_content_labels(
    temp_db: None,
    decision: str,
    expected_label: int | None,
) -> None:
    candidate_id = _seed_plugin_candidate()

    feedback = build_highlight_feedback(candidate_id, decision=decision, reviewed_by="alice")

    assert feedback is not None
    assert feedback.plugin_id == "bililivecut-highlight"
    assert feedback.sample_id == f"candidate:{candidate_id}"
    assert feedback.label == expected_label
    assert feedback.label_source == f"human:alice:{decision}"
    assert feedback.feature_values == {"duration_s": 30.0, "audio_prominence": None}


def test_feedback_delivery_is_non_blocking_and_targets_prediction_plugin(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    candidate_id = _seed_plugin_candidate()
    received = []

    def fake_record(feedback):
        received.append(feedback)
        return HighlightFeedbackDispatch(plugin_id=feedback.plugin_id, delivered=True)

    monkeypatch.setattr(plugin_manager, "record_highlight_feedback", fake_record)

    dispatch = record_candidate_review_feedback(
        candidate_id,
        decision=ReviewStatus.REJECTED,
        reviewed_by="alice",
    )

    assert dispatch is not None and dispatch.delivered is True
    assert len(received) == 1
    assert received[0].plugin_id == "bililivecut-highlight"
    assert received[0].label == 0


def test_feedback_skips_candidate_without_auditable_feature_snapshot(temp_db: None) -> None:
    candidate_id = _seed_plugin_candidate(valid_metadata=False)

    assert (
        build_highlight_feedback(
            candidate_id,
            decision=ReviewStatus.APPROVED_SOLO,
            reviewed_by="alice",
        )
        is None
    )
