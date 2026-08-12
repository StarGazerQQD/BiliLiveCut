"""按录制场次聚合高光时间线的回归测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.analysis.session_summary import (
    build_session_timeline_summary,
    claim_pending_session_summary,
    execute_session_summary_claim,
    recover_running_session_summary_requests,
    request_session_timeline_summary,
)
from app.db.models import (
    AppSetting,
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    SegmentTask,
    TaskStatus,
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


def test_whole_session_summary_uses_visible_timeline_in_gmt8_order(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整场总结只能消费未拒绝时间点，并用 GMT+8 时钟按先后整理。"""
    from app.analysis import llm

    session_id = _seed_timeline()
    with get_session() as db:
        pending = db.get(AppSetting, f"session_reanalysis:{session_id}")
        assert pending is not None
        db.delete(pending)
        earlier = HighlightCandidate(
            session_id=session_id,
            peak_ts=datetime(2026, 8, 5, 11, 15, tzinfo=UTC),
            start_ts=datetime(2026, 8, 5, 11, 14, tzinfo=UTC),
            end_ts=datetime(2026, 8, 5, 11, 16, tzinfo=UTC),
            highlight_score=0.76,
            reason="开场铺垫",
            dedup_hash="summary-earlier",
        )
        db.add(earlier)
        db.flush()
        db.add(
            HighlightEvent(
                candidate_id=earlier.id,
                session_id=session_id,
                raw_start_ts=earlier.start_ts,
                raw_end_ts=earlier.end_ts,
                reason="主播先介绍挑战规则",
                review_status=ReviewStatus.PENDING,
            )
        )

    prompts: list[str] = []

    def fake_call_text(prompt: str, max_tokens: int) -> str:
        prompts.append(prompt)
        assert max_tokens == 65536
        return '{"summary":"- 19:15:00，主播介绍挑战规则。\\n- 19:45:00，主播完成关键反转。"}'

    monkeypatch.setattr(llm, "call_text", fake_call_text)
    result = build_session_timeline_summary(session_id)

    assert result["source"] == "llm"
    assert result["point_count"] == 2
    assert result["summary"] == "19:15:00，主播介绍挑战规则。 19:45:00，主播完成关键反转。"
    assert len(prompts) == 1
    assert prompts[0].index("19:15:00") < prompts[0].index("19:45:00")
    assert "19:45:00" in prompts[0]
    assert "候选梗概" not in prompts[0]
    assert "严格按 GMT+8 时间从早到晚" in prompts[0]


def test_whole_session_summary_waits_for_final_analysis_and_persists_rule_fallback(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """请求应等待最终重分析稳定，LLM 不可用时仍持久化规则版总结。"""
    from app.analysis import llm

    session_id = _seed_timeline()
    with get_session() as db:
        pending = db.get(AppSetting, f"session_reanalysis:{session_id}")
        assert pending is not None
        db.delete(pending)
        task = SegmentTask(
            segment_id=db.exec(select(RawSegment.id).where(RawSegment.session_id == session_id)).first(),
            session_id=session_id,
            stage=TaskStatus.ANALYZING,
            idempotency_key="summary-wait",
            pipeline_key="summary-wait-pipeline",
        )
        db.add(task)

    assert request_session_timeline_summary(session_id, reason="test", force=True) is True
    assert claim_pending_session_summary() is None

    with get_session() as db:
        task = db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).first()
        assert task is not None
        task.stage = TaskStatus.COMPLETED
        db.add(task)

    monkeypatch.setattr(llm, "call_text", lambda *_args, **_kwargs: None)
    claim = claim_pending_session_summary()
    assert claim is not None
    assert execute_session_summary_claim(claim) is True

    payload = get_session_timeline(session_id)
    summary = payload["whole_session_summary"]
    assert summary["status"] == "ready"
    assert summary["source"] == "rules"
    assert "19:45:00" in summary["summary"]
    assert "本场共记录 1 个高光" in summary["summary"]
    assert summary["generated_at"].endswith("+08:00")


def test_timeline_view_requests_missing_summary_for_finished_session(temp_db: None) -> None:
    """历史结束场次没有总结时，首次查看时间线应自动补登记持久请求。"""
    session_id = _seed_timeline()
    with get_session() as db:
        pending = db.get(AppSetting, f"session_reanalysis:{session_id}")
        assert pending is not None
        db.delete(pending)

    payload = get_session_timeline(session_id)

    assert payload["whole_session_summary"]["status"] == "pending"
    with get_session() as db:
        row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
        assert row is not None


def test_whole_session_summary_recovers_running_request_and_rejects_stale_claim(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重启应恢复运行请求，强制重生成后旧线程不得覆盖新请求。"""
    from app.analysis import session_summary

    session_id = _seed_timeline()
    with get_session() as db:
        pending = db.get(AppSetting, f"session_reanalysis:{session_id}")
        assert pending is not None
        db.delete(pending)

    assert request_session_timeline_summary(session_id, reason="first", force=True) is True
    stale_claim = claim_pending_session_summary()
    assert stale_claim is not None
    assert recover_running_session_summary_requests() == 1
    recovered_claim = claim_pending_session_summary()
    assert recovered_claim == stale_claim

    assert request_session_timeline_summary(session_id, reason="new-version", force=True) is True
    monkeypatch.setattr(
        session_summary,
        "build_session_timeline_summary",
        lambda _session_id: {
            "version": 1,
            "session_id": session_id,
            "timeline_signature": "stale",
            "point_count": 1,
            "source": "rules",
            "summary": "旧线程结果",
            "generated_at": datetime.now(UTC).isoformat(),
        },
    )

    assert execute_session_summary_claim(recovered_claim) is False
    with get_session() as db:
        request = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
        result = db.get(AppSetting, f"session_timeline_summary:{session_id}")
        assert request is not None
        assert json.loads(request.value)["reason"] == "new-version"
        assert result is None


def test_whole_session_summary_stops_retrying_after_three_failures(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整场总结执行异常最多重试三次，之后保留可见失败状态。"""
    from app.analysis import session_summary

    session_id = _seed_timeline()
    with get_session() as db:
        pending = db.get(AppSetting, f"session_reanalysis:{session_id}")
        assert pending is not None
        db.delete(pending)

    assert request_session_timeline_summary(session_id, reason="retry-test", force=True) is True

    def fail_summary(_session_id: int) -> dict[str, object]:
        raise RuntimeError("模拟总结故障")

    monkeypatch.setattr(session_summary, "build_session_timeline_summary", fail_summary)
    for attempt in range(1, 4):
        if attempt > 1:
            with get_session() as db:
                row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
                assert row is not None
                payload = json.loads(row.value)
                payload["next_retry_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
                row.value = json.dumps(payload, ensure_ascii=False)
                db.add(row)
        claim = claim_pending_session_summary()
        assert claim is not None
        assert execute_session_summary_claim(claim) is False

    with get_session() as db:
        row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
        assert row is not None
        payload = json.loads(row.value)
    assert payload["status"] == "failed"
    assert payload["attempts"] == 3
    assert "模拟总结故障" in payload["last_error"]
    assert claim_pending_session_summary() is None
