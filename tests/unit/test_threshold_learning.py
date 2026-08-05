"""房间阈值反馈采样与推荐算法回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.analysis.threshold_learning import (
    compute_recommended_threshold,
    feedback_summary,
    record_feedback,
    sync_candidate_feedback,
)
from app.db.models import HighlightCandidate, LiveRoom, RecordingSession, ThresholdFeedback
from app.db.session import get_session


def _seed_candidates(scores: list[float], *, auto_threshold_enabled: bool = True) -> tuple[int, list[int]]:
    """创建同一房间下的一组可学习候选。"""
    now = datetime(2026, 8, 5, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(
            input_url="threshold-test",
            room_id=300,
            highlight_threshold=0.4,
            auto_threshold_enabled=auto_threshold_enabled,
        )
        db.add(room)
        db.flush()
        assert room.id is not None
        session = RecordingSession(room_id=room.id, status="stopped", started_at=now, ended_at=now)
        db.add(session)
        db.flush()
        assert session.id is not None
        candidate_ids: list[int] = []
        for index, score in enumerate(scores):
            candidate = HighlightCandidate(
                session_id=session.id,
                peak_ts=now + timedelta(seconds=index),
                start_ts=now,
                end_ts=now + timedelta(seconds=30),
                highlight_score=score,
                dedup_hash=f"threshold-{index}",
            )
            db.add(candidate)
            db.flush()
            assert candidate.id is not None
            candidate_ids.append(candidate.id)
        return room.id, candidate_ids


def test_feedback_upserts_decision_and_reports_rejected_only_sample(temp_db: None) -> None:
    """同一候选改变决策时只保留一条样本，拒绝样本也必须出现在摘要中。"""
    room_id, candidate_ids = _seed_candidates([0.72])

    first = sync_candidate_feedback(candidate_ids[0], decision="approved_solo")
    second = sync_candidate_feedback(candidate_ids[0], decision="rejected")

    assert first["action"] == "approved"
    assert second["action"] == "rejected"
    with get_session() as db:
        rows = db.exec(select(ThresholdFeedback).where(ThresholdFeedback.candidate_id == candidate_ids[0])).all()
    assert len(rows) == 1
    assert rows[0].action == "rejected"
    summary = feedback_summary(room_id)
    assert summary["samples"] == 1
    assert summary["approved_range"] is None
    assert summary["rejected_range"] == [0.72, 0.72]


def test_recommendation_uses_separable_rejected_ceiling_without_losing_approved_recall(temp_db: None) -> None:
    """可分离正负样本应在拒绝最高分和认可 P15 之间给出安全阈值。"""
    scores = [0.60 + index * 0.01 for index in range(10)] + [0.30]
    room_id, candidate_ids = _seed_candidates(scores)
    for candidate_id in candidate_ids[:10]:
        record_feedback(room_id, candidate_id, "approved")
    record_feedback(room_id, candidate_ids[-1], "rejected")

    recommended = compute_recommended_threshold(room_id)

    assert recommended == pytest.approx(0.457, abs=0.001)
    summary = feedback_summary(room_id)
    assert summary["ready"] is True
    assert summary["samples"] == 11
    assert summary["approved_range"] == [0.6, 0.69]
    assert summary["rejected_range"] == [0.3, 0.3]


def test_room_switch_disables_feedback_persistence(temp_db: None) -> None:
    """房间未启用自学习时，审核结果不应写入阈值样本表。"""
    _room_id, candidate_ids = _seed_candidates([0.8], auto_threshold_enabled=False)

    result = sync_candidate_feedback(candidate_ids[0], decision="approved_solo")

    assert result["enabled"] is False
    with get_session() as db:
        assert db.exec(select(ThresholdFeedback)).all() == []
