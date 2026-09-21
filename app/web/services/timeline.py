"""按录制场次聚合的高光时间线查询服务。"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from sqlmodel import select

from app.db.entities import (
    AppSetting,
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    HotspotStatus,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    SegmentTask,
    SessionStatus,
    TaskStatus,
)
from app.db.session import get_session
from app.web.services.review_workflow import decode_features
from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

_GMT8 = timezone(timedelta(hours=8), name="GMT+8")
_PENDING_REANALYSIS_PREFIX = "session_reanalysis:"
_REJECTED_REVIEWS = {ReviewStatus.REJECTED, ReviewStatus.NOT_EXCITING}
_VISIBLE_HOTSPOT_STATUSES = {
    HotspotStatus.PROVISIONAL,
    HotspotStatus.ENRICHING,
    HotspotStatus.CONFIRMED,
}
_PROCESSING_STAGES = {
    TaskStatus.RECORDED,
    TaskStatus.QUEUED_FOR_TRANS,
    TaskStatus.TRANSCRIBING,
    TaskStatus.TRANSCRIBED,
    TaskStatus.QUEUED_FOR_ANALYSIS,
    TaskStatus.ANALYZING,
    TaskStatus.STALE,
    TaskStatus.TRANSIENT_FAILED,
}


def list_session_timelines(
    *, limit: int = 30, room_db_id: int | None = None, session_id: int | None = None
) -> list[dict[str, Any]]:
    """返回最近录制场次及其时间线概览。"""
    safe_limit = max(1, min(limit, 200))
    with get_session() as db:
        statement = select(RecordingSession).order_by(RecordingSession.started_at.desc()).limit(safe_limit)
        if room_db_id is not None:
            statement = statement.where(RecordingSession.room_id == room_db_id)
        if session_id is not None:
            statement = statement.where(RecordingSession.id == session_id)
        sessions = db.exec(statement).all()
        session_ids = [session.id for session in sessions if session.id is not None]
        if not session_ids:
            return []
        hotspots = db.exec(
            select(HotspotEvent).where(
                HotspotEvent.session_id.in_(session_ids),
                HotspotEvent.status.in_(_VISIBLE_HOTSPOT_STATUSES),
            )
        ).all()
        candidate_ids = [hotspot.candidate_id for hotspot in hotspots if hotspot.candidate_id is not None]
        candidates = (
            db.exec(select(HighlightCandidate).where(HighlightCandidate.id.in_(candidate_ids))).all()
            if candidate_ids
            else []
        )
        events = (
            db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id.in_(candidate_ids))).all()
            if candidate_ids
            else []
        )
        tasks = db.exec(select(SegmentTask).where(SegmentTask.session_id.in_(session_ids))).all()
        segments = db.exec(select(RawSegment).where(RawSegment.session_id.in_(session_ids))).all()
        pending_keys = [f"{_PENDING_REANALYSIS_PREFIX}{session_id}" for session_id in session_ids]
        pending_rows = db.exec(select(AppSetting).where(AppSetting.key.in_(pending_keys))).all()
        sources = source_identities_for_sessions(db, session_ids)

    events_by_candidate = _event_map(candidate_ids, events)
    candidates_by_session: dict[int, list[HighlightCandidate]] = defaultdict(list)
    hotspots_by_session: dict[int, list[HotspotEvent]] = defaultdict(list)
    tasks_by_session: dict[int, list[SegmentTask]] = defaultdict(list)
    segment_counts: dict[int, int] = defaultdict(int)
    for candidate in candidates:
        candidates_by_session[candidate.session_id].append(candidate)
    for hotspot in hotspots:
        hotspots_by_session[hotspot.session_id].append(hotspot)
    for task in tasks:
        tasks_by_session[task.session_id].append(task)
    for segment in segments:
        segment_counts[segment.session_id] += 1
    pending_sessions = {
        int(row.key.removeprefix(_PENDING_REANALYSIS_PREFIX))
        for row in pending_rows
        if row.key.removeprefix(_PENDING_REANALYSIS_PREFIX).isdigit()
    }

    result: list[dict[str, Any]] = []
    for session in sessions:
        if session.id is None:
            continue
        session_candidates = candidates_by_session.get(session.id, [])
        session_hotspots = hotspots_by_session.get(session.id, [])
        visible_count = 0
        rejected_count = 0
        pending_review_count = 0
        for candidate in session_candidates:
            event = events_by_candidate[candidate.id]
            if _candidate_is_rejected(candidate, event):
                rejected_count += 1
                continue
            visible_count += 1
            if event.review_status == ReviewStatus.PENDING:
                pending_review_count += 1
        result.append(
            {
                "session_id": session.id,
                "status": session.status,
                "started_at": _iso_utc(session.started_at),
                "ended_at": _iso_utc(session.ended_at),
                "started_at_gmt8": _iso_gmt8(session.started_at),
                "ended_at_gmt8": _iso_gmt8(session.ended_at),
                "duration_s": _duration_s(session.started_at, session.ended_at),
                "segment_count": segment_counts.get(session.id, 0),
                "highlight_count": visible_count,
                "hotspot_count": len(session_hotspots),
                "hotspot_only_count": sum(hotspot.candidate_id is None for hotspot in session_hotspots),
                "timeline_count": len(session_hotspots),
                "pending_review_count": pending_review_count,
                "rejected_count": rejected_count,
                "processing_state": _processing_state(
                    session,
                    tasks_by_session.get(session.id, []),
                    pending_reanalysis=session.id in pending_sessions,
                ),
                "timeline_url": f"/api/sessions/{session.id}/timeline",
                **sources.get(session.id, unknown_source_identity()),
            }
        )
    return result


def get_session_timeline(
    session_id: int,
    *,
    include_rejected: bool = False,
    include_summary: bool = True,
) -> dict[str, Any]:
    """返回一场录制的 GMT+8 时间轴与所有高光节点。"""
    with get_session() as db:
        session = db.get(RecordingSession, session_id)
        if session is None:
            raise ValueError(f"录制会话不存在: session_id={session_id}")
        hotspots = db.exec(
            select(HotspotEvent)
            .where(
                HotspotEvent.session_id == session_id,
                HotspotEvent.status.in_(_VISIBLE_HOTSPOT_STATUSES),
            )
            .order_by(HotspotEvent.peak_ts.asc(), HotspotEvent.id.asc())
        ).all()
        candidate_ids = [hotspot.candidate_id for hotspot in hotspots if hotspot.candidate_id is not None]
        candidates = (
            db.exec(select(HighlightCandidate).where(HighlightCandidate.id.in_(candidate_ids))).all()
            if candidate_ids
            else []
        )
        events = (
            db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id.in_(candidate_ids))).all()
            if candidate_ids
            else []
        )
        event_by_candidate = _event_map(candidate_ids, events)
        tasks = db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).all()
        segment_count = len(db.exec(select(RawSegment.id).where(RawSegment.session_id == session_id)).all())
        pending_reanalysis = db.get(AppSetting, f"{_PENDING_REANALYSIS_PREFIX}{session_id}") is not None
        source = source_identities_for_sessions(db, [session_id]).get(session_id, unknown_source_identity())

    candidate_by_id = {candidate.id: candidate for candidate in candidates if candidate.id is not None}
    all_points: list[dict[str, Any]] = []
    for hotspot in hotspots:
        candidate = candidate_by_id.get(hotspot.candidate_id)
        event = event_by_candidate.get(hotspot.candidate_id) if hotspot.candidate_id is not None else None
        rejected = candidate is not None and event is not None and _candidate_is_rejected(candidate, event)
        all_points.append(
            _hotspot_timeline_point(
                session,
                hotspot,
                candidate,
                event,
                rejected=rejected,
            )
        )
    all_points.sort(key=lambda point: (float(point["offset_s"]), int(point.get("hotspot_event_id") or 0)))
    points = [point for point in all_points if include_rejected or not point["rejected"]]

    processing_state = _processing_state(session, tasks, pending_reanalysis=pending_reanalysis)
    from app.analysis.session_summary import (
        ensure_session_timeline_summary_requested,
        session_timeline_summary_view,
    )

    if include_summary and session.ended_at is not None:
        ensure_session_timeline_summary_requested(session_id)

    result = {
        "session": {
            "session_id": session_id,
            "status": session.status,
            "started_at": _iso_utc(session.started_at),
            "ended_at": _iso_utc(session.ended_at),
            "started_at_gmt8": _iso_gmt8(session.started_at),
            "ended_at_gmt8": _iso_gmt8(session.ended_at),
            "duration_s": _duration_s(session.started_at, session.ended_at),
            "segment_count": segment_count,
            "processing_state": processing_state,
            **source,
        },
        "timezone": "GMT+8",
        "points": points,
        "counts": {
            "visible": sum(1 for point in points if not point["rejected"]),
            "rejected": sum(1 for point in all_points if point["rejected"]),
            "total": len(all_points),
            "hotspots": len(hotspots),
            "hotspot_only": sum(point["candidate_id"] is None for point in all_points),
            "candidates": sum(point["candidate_id"] is not None for point in all_points),
        },
    }
    if include_summary:
        result["whole_session_summary"] = session_timeline_summary_view(
            session_id,
            processing_state=processing_state,
            ended=session.ended_at is not None,
        )
    return result


def _hotspot_timeline_point(
    session: RecordingSession,
    hotspot: HotspotEvent,
    candidate: HighlightCandidate | None,
    event: HighlightEvent | None,
    *,
    rejected: bool,
) -> dict[str, Any]:
    """把一等热点事件转换为时间线节点，并可选附加既有候选审核入口。"""
    event_features = decode_features(hotspot.features_json)
    candidate_features = decode_features(candidate.features_json) if candidate is not None else {}
    candidate_timeline = (
        candidate_features.get("timeline") if isinstance(candidate_features.get("timeline"), dict) else {}
    )
    clip_metadata = (
        event_features.get("event_clip_score") if isinstance(event_features.get("event_clip_score"), dict) else {}
    )
    lifecycle = event_features.get("event_lifecycle") if isinstance(event_features.get("event_lifecycle"), dict) else {}
    candidate_start = event.adjusted_start_ts if event is not None else None
    candidate_end = event.adjusted_end_ts if event is not None else None
    if candidate is not None:
        candidate_start = candidate_start or candidate.start_ts
        candidate_end = candidate_end or candidate.end_ts
    return {
        "point_type": "hotspot",
        "hotspot_event_id": hotspot.id,
        "candidate_id": candidate.id if candidate is not None else None,
        "event_id": event.id if event is not None else None,
        "event_status": hotspot.status,
        "clock_gmt8": _clock_gmt8(hotspot.peak_ts),
        "peak_at_gmt8": _iso_gmt8(hotspot.peak_ts),
        "start_at_gmt8": _iso_gmt8(hotspot.start_ts),
        "end_at_gmt8": _iso_gmt8(hotspot.end_ts),
        "offset_s": round((_as_utc(hotspot.peak_ts) - _as_utc(session.started_at)).total_seconds(), 3),
        "duration_s": round(max(0.0, (_as_utc(hotspot.end_ts) - _as_utc(hotspot.start_ts)).total_seconds()), 3),
        "title": hotspot.title or hotspot.summary or "待补全热点事件",
        "summary": hotspot.summary or hotspot.title or "事件语义证据不足，请结合代表弹幕和来源信号确认。",
        "representative_danmaku": _representative_danmaku_json(hotspot.representative_danmaku_json),
        "confidence": round(max(0.0, min(1.0, float(hotspot.semantic_confidence))), 3),
        "heat_score": round(max(0.0, min(1.0, float(hotspot.heat_score))), 4),
        "clip_score": round(max(0.0, min(1.0, float(hotspot.clip_score))), 4),
        "semantic_confidence": round(max(0.0, min(1.0, float(hotspot.semantic_confidence))), 4),
        "evidence_coverage": round(max(0.0, min(1.0, float(hotspot.evidence_coverage))), 4),
        "source_signals": _hotspot_source_signals(event_features, candidate_features),
        "review_status": event.review_status if event is not None else None,
        "candidate_status": candidate.status if candidate is not None else None,
        "rejected": rejected,
        "review_url": f"/review/{candidate.id}" if candidate is not None else None,
        "preview_url": f"/review/api/{candidate.id}/preview" if candidate is not None else None,
        "provenance": {
            "event_key": hotspot.event_key,
            "detector_version": event_features.get("detector_version"),
            "event_version": lifecycle.get("version"),
            "clip_scorer_version": candidate_features.get("clip_scorer_version"),
            "rule_score": round(float(candidate.rule_score), 4) if candidate is not None else 0.0,
            "llm_score": round(float(candidate.llm_score), 4) if candidate is not None else 0.0,
            "highlight_score": round(float(candidate.highlight_score), 4) if candidate is not None else 0.0,
            "heat_score": round(float(hotspot.heat_score), 4),
            "clip_score": round(float(hotspot.clip_score), 4),
            "semantic_confidence": round(float(hotspot.semantic_confidence), 4),
            "evidence_coverage": round(float(hotspot.evidence_coverage), 4),
            "scoring_components": clip_metadata.get("components", {}),
            "dynamic_bounds": bool(candidate_timeline.get("dynamic_bounds", False)),
            "cross_segment": bool(candidate_timeline.get("cross_segment", False)),
            "candidate_start_at_gmt8": _iso_gmt8(candidate_start),
            "candidate_end_at_gmt8": _iso_gmt8(candidate_end),
        },
    }


def _candidate_is_rejected(candidate: HighlightCandidate, event: HighlightEvent) -> bool:
    return candidate.status == CandidateStatus.REJECTED or event.review_status in _REJECTED_REVIEWS


def _event_map(candidate_ids: list[int], events: list[HighlightEvent]) -> dict[int, HighlightEvent]:
    """验证并返回当前候选与审核事件的一对一映射。"""
    event_by_candidate = {event.candidate_id: event for event in events}
    missing = [candidate_id for candidate_id in candidate_ids if candidate_id not in event_by_candidate]
    if missing:
        raise RuntimeError(f"候选数据不完整：缺少审核事件 candidate_ids={missing}")
    return event_by_candidate


def _processing_state(
    session: RecordingSession,
    tasks: list[SegmentTask],
    *,
    pending_reanalysis: bool,
) -> str:
    if session.status in {
        SessionStatus.STARTING,
        SessionStatus.RECORDING,
        SessionStatus.RECONNECTING,
        SessionStatus.RECONNECTED,
        SessionStatus.STOPPING,
        SessionStatus.FINALIZING,
    }:
        return "recording"
    if pending_reanalysis:
        return "finalizing"
    if any(task.stage in _PROCESSING_STAGES or task.claimed_by or task.lease_token for task in tasks):
        return "processing"
    if any(task.stage == TaskStatus.FAILED for task in tasks):
        return "partial_failure"
    return "ready"


def _representative_danmaku_payload(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in value[:2]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        count = item.get("count", 1)
        result.append({"text": text, "count": max(1, int(count)) if isinstance(count, (int, float)) else 1})
    return result


def _representative_danmaku_json(raw: str | None) -> list[dict[str, object]]:
    """解析热点事件持久化的代表弹幕。"""
    try:
        value = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    return _representative_danmaku_payload(value)


def _hotspot_source_signals(
    event_features: dict[str, Any],
    candidate_features: dict[str, Any],
) -> list[str]:
    """从事件 tick 与候选评分特征中提取稳定、可读的证据信号标签。"""
    values: dict[str, list[float]] = defaultdict(list)
    candidate_scores = candidate_features.get("signal_scores")
    if isinstance(candidate_scores, dict):
        for name, value in candidate_scores.items():
            if isinstance(value, (int, float)):
                values[str(name)].append(float(value))
    ticks = event_features.get("ticks")
    if isinstance(ticks, list):
        for tick in ticks:
            if not isinstance(tick, dict):
                continue
            scores = tick.get("modality_scores")
            if not isinstance(scores, dict):
                continue
            for name, value in scores.items():
                if isinstance(value, (int, float)):
                    values[str(name)].append(float(value))
    labels = {
        "danmaku": "弹幕高峰",
        "audio": "音频峰值",
        "sensevoice": "SenseVoice",
        "asr": "ASR 语义",
        "trend": "趋势信号",
    }
    result = [labels[name] for name in labels if values.get(name) and max(values[name]) >= 0.20]
    return result or ["热点检测"]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso_utc(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _iso_gmt8(value: datetime | None) -> str | None:
    return _as_utc(value).astimezone(_GMT8).isoformat() if value is not None else None


def _clock_gmt8(value: datetime) -> str:
    return _as_utc(value).astimezone(_GMT8).strftime("%H:%M:%S")


def _duration_s(start: datetime, end: datetime | None) -> float | None:
    if end is None:
        return None
    return round(max(0.0, (_as_utc(end) - _as_utc(start)).total_seconds()), 3)
