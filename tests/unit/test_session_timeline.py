"""按录制场次聚合高光时间线的回归测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.db.models import (
    AppSetting,
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
)
from app.db.session import get_session
from app.web.services.timeline import get_session_timeline, list_session_timelines


def _seed_timeline() -> int:
    """创建一个含可见与已拒绝节点的录制时间线。"""
    started_at = datetime(2026, 8, 5, 11, 0, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="timeline", room_id=23771139, uploader_name="测试主播", title="测试直播")
        db.add(room)
        db.flush()
        assert room.id is not None
        session = RecordingSession(
            room_id=room.id,
            status="stopped",
            started_at=started_at,
            ended_at=started_at + timedelta(hours=2),
        )
        db.add(session)
        db.flush()
        assert session.id is not None
        db.add(
            RawSegment(
                session_id=session.id,
                seq=0,
                file_path="timeline.ts",
                start_ts=started_at,
                end_ts=started_at + timedelta(minutes=5),
                duration_s=300,
                status="scored",
            )
        )
        timeline_features = json.dumps(
            {
                "features": {"danmaku": 0.9, "volume": 0.6},
                "timeline": {
                    "analysis_version": 1,
                    "confidence": 0.88,
                    "source_signals": ["弹幕高峰", "音量突增"],
                    "representative_danmaku": [
                        {"text": "笑死", "count": 8},
                        {"text": "名场面", "count": 3},
                    ],
                    "danmaku_lag_s": 7.5,
                    "dynamic_bounds": True,
                    "cross_segment": True,
                },
                "analysis_window": {"segment_id": 1, "precise_transcript": True},
            },
            ensure_ascii=False,
        )
        visible = HighlightCandidate(
            session_id=session.id,
            peak_ts=started_at + timedelta(minutes=45),
            start_ts=started_at + timedelta(minutes=44, seconds=10),
            end_ts=started_at + timedelta(minutes=45, seconds=35),
            rule_score=0.81,
            llm_score=0.93,
            highlight_score=0.87,
            reason="候选梗概",
            features_json=timeline_features,
            dedup_hash="timeline-visible",
        )
        db.add(visible)
        db.flush()
        db.add(
            HighlightEvent(
                candidate_id=visible.id,
                session_id=session.id,
                raw_start_ts=visible.start_ts,
                raw_end_ts=visible.end_ts,
                reason="主播完成关键反转",
                features_json=timeline_features,
                review_status=ReviewStatus.PENDING,
            )
        )
        rejected = HighlightCandidate(
            session_id=session.id,
            peak_ts=started_at + timedelta(minutes=55),
            start_ts=started_at + timedelta(minutes=54),
            end_ts=started_at + timedelta(minutes=56),
            highlight_score=0.7,
            status=CandidateStatus.REJECTED,
            dedup_hash="timeline-rejected",
        )
        db.add(rejected)
        db.flush()
        db.add(
            HighlightEvent(
                candidate_id=rejected.id,
                session_id=session.id,
                raw_start_ts=rejected.start_ts,
                raw_end_ts=rejected.end_ts,
                review_status=ReviewStatus.REJECTED,
                review_by="tester",
            )
        )
        db.add(
            AppSetting(
                key=f"session_reanalysis:{session.id}",
                value=json.dumps({"session_id": session.id, "reason": "session_finalized"}),
            )
        )
        return session.id


def test_session_timeline_exposes_gmt8_summary_danmaku_and_provenance(temp_db: None) -> None:
    """时间点必须同时具备本地钟点、梗概、弹幕、来源信号和可核查评分。"""
    session_id = _seed_timeline()

    payload = get_session_timeline(session_id)

    assert payload["timezone"] == "GMT+8"
    assert payload["session"]["source_label"] == "测试主播 · 房间 23771139"
    assert payload["session"]["processing_state"] == "finalizing"
    assert payload["counts"] == {"visible": 1, "rejected": 1, "total": 2}
    assert len(payload["points"]) == 1
    point = payload["points"][0]
    assert point["clock_gmt8"] == "19:45:00"
    assert point["summary"] == "主播完成关键反转"
    assert point["representative_danmaku"] == [
        {"text": "笑死", "count": 8},
        {"text": "名场面", "count": 3},
    ]
    assert point["source_signals"] == ["弹幕高峰", "音量突增"]
    assert point["confidence"] == 0.88
    assert point["provenance"]["cross_segment"] is True
    assert point["provenance"]["danmaku_lag_s"] == 7.5
    assert point["review_url"].endswith(f"/{point['candidate_id']}")


def test_session_timeline_can_include_rejected_nodes_and_list_overview(temp_db: None) -> None:
    """默认隐藏终态拒绝节点，但显式查询与场次概览应保留拒绝统计。"""
    session_id = _seed_timeline()

    overview = list_session_timelines()
    expanded = get_session_timeline(session_id, include_rejected=True)

    assert len(overview) == 1
    assert overview[0]["highlight_count"] == 1
    assert overview[0]["rejected_count"] == 1
    assert overview[0]["started_at_gmt8"].startswith("2026-08-05T19:00:00")
    assert len(expanded["points"]) == 2
    assert sum(1 for point in expanded["points"] if point["rejected"]) == 1
