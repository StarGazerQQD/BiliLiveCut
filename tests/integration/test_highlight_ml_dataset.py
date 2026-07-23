"""高光模型数据集构建的真实 SQLite 集成测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.analysis.highlight_ml.context import load_feature_context
from app.analysis.highlight_ml.dataset import build_labeled_dataset
from app.analysis.highlight_ml.types import AudioSnapshot
from app.db.models import (
    Danmaku,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    ThresholdFeedback,
    Transcript,
)


def _seed_dataset() -> tuple[int, int, int]:
    from app.db.session import get_session

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="https://live.bilibili.com/100", room_id=100, authorized=True)
        db.add(room)
        db.flush()
        assert room.id is not None

        recording = RecordingSession(room_id=room.id, started_at=base)
        db.add(recording)
        db.flush()
        assert recording.id is not None

        segments: list[RawSegment] = []
        for seq in range(3):
            start = base + timedelta(seconds=60 * (seq + 1))
            segment = RawSegment(
                session_id=recording.id,
                seq=seq,
                file_path=f"segment-{seq}.mp4",
                start_ts=start,
                end_ts=start + timedelta(seconds=60),
                duration_s=60.0,
            )
            db.add(segment)
            db.flush()
            assert segment.id is not None
            segments.append(segment)

        db.add(
            Transcript(
                segment_id=segments[0].id,
                text="哈哈！这波精彩",
                words_json=json.dumps(
                    [
                        {"w": "哈哈", "start": 0.0, "end": 0.5},
                        {"w": "精彩", "start": 1.5, "end": 2.0},
                    ]
                ),
                avg_logprob=-0.2,
                review_risk_score=0.1,
            )
        )
        for offset, content in ((10, "历史"), (70, "爆了！"), (130, "未来消息")):
            db.add(
                Danmaku(
                    session_id=recording.id,
                    room_id=100,
                    ts=base + timedelta(seconds=offset),
                    content=content,
                    user=f"u{offset}",
                )
            )

        first = HighlightCandidate(
            session_id=recording.id,
            peak_ts=base + timedelta(seconds=80),
            start_ts=base + timedelta(seconds=70),
            end_ts=base + timedelta(seconds=100),
        )
        second = HighlightCandidate(
            session_id=recording.id,
            peak_ts=base + timedelta(seconds=140),
            start_ts=base + timedelta(seconds=130),
            end_ts=base + timedelta(seconds=160),
        )
        third = HighlightCandidate(
            session_id=recording.id,
            peak_ts=base + timedelta(seconds=200),
            start_ts=base + timedelta(seconds=190),
            end_ts=base + timedelta(seconds=220),
        )
        db.add(first)
        db.add(second)
        db.add(third)
        db.flush()
        assert first.id is not None and second.id is not None and third.id is not None

        db.add(
            HighlightEvent(
                candidate_id=first.id,
                session_id=recording.id,
                segment_id=segments[0].id,
                review_status=ReviewStatus.APPROVED_SOLO,
                review_by="manual",
                updated_at=base + timedelta(hours=1),
            )
        )
        db.add(
            HighlightEvent(
                candidate_id=second.id,
                session_id=recording.id,
                segment_id=None,
                review_status=ReviewStatus.NOT_EXCITING,
                review_by="manual",
                updated_at=base + timedelta(hours=1),
            )
        )
        db.add(
            HighlightEvent(
                candidate_id=third.id,
                session_id=recording.id,
                segment_id=segments[2].id,
                review_status=ReviewStatus.START_TOO_LATE,
                review_by="manual",
                updated_at=base + timedelta(hours=1),
            )
        )
        db.add(
            ThresholdFeedback(
                room_id=room.id,
                candidate_id=second.id,
                action="rejected",
                old_threshold=0.65,
                highlight_score=0.4,
                created_at=base + timedelta(hours=2),
            )
        )
        return segments[0].id, segments[1].id, segments[2].id


def test_context_uses_past_baseline_and_current_window_only(temp_db: None) -> None:
    """未来弹幕不会进入当前片段上下文或历史基线。"""
    first_id, _, _ = _seed_dataset()
    from app.db.session import get_session

    with get_session() as db:
        context = load_feature_context(db, first_id)

    assert [item.content for item in context.baseline_danmaku] == ["历史"]
    assert [item.content for item in context.window_danmaku] == ["爆了！"]


def test_dataset_maps_events_fallbacks_and_blind_review_without_pseudo_labels(temp_db: None) -> None:
    """显式事件映射和峰值兜底均生效，边界问题只进入盲审而非负类。"""
    first_id, second_id, third_id = _seed_dataset()
    from app.db.session import get_session

    audio_calls: list[str] = []

    def audio_loader(path: str) -> AudioSnapshot:
        audio_calls.append(path)
        return AudioSnapshot(1.0, 0.25, 0.2, 0.75, 0.1)

    with get_session() as db:
        bundle = build_labeled_dataset(
            db,
            audio_loader=audio_loader,
            blind_review_limit=10,
            blind_review_seed=7,
        )

    assert [sample.segment_id for sample in bundle.samples] == [first_id, second_id]
    assert sorted(bundle.y.tolist()) == [0, 1]
    assert {sample.label_source for sample in bundle.samples} == {
        "highlight_event:approved_solo",
        "threshold_feedback:rejected",
    }
    assert audio_calls == ["segment-0.mp4", "segment-1.mp4"]
    assert bundle.X.shape == (2, len(bundle.feature_names))
    assert {item.segment_id for item in bundle.blind_review_queue} == {third_id}
