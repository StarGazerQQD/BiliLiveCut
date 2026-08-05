"""按录制场次聚合的高光时间线查询服务。"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from sqlmodel import select

from app.analysis.timeline import source_signals
from app.db.models import (
    AppSetting,
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
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


def list_session_timelines(*, limit: int = 30, room_db_id: int | None = None) -> list[dict[str, Any]]:
    """返回最近录制场次及其时间线概览。"""
    safe_limit = max(1, min(limit, 200))
    with get_session() as db:
        statement = select(RecordingSession).order_by(RecordingSession.started_at.desc()).limit(safe_limit)
        if room_db_id is not None:
            statement = statement.where(RecordingSession.room_id == room_db_id)
        sessions = db.exec(statement).all()
        session_ids = [session.id for session in sessions if session.id is not None]
        if not session_ids:
            return []
        candidates = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id.in_(session_ids))).all()
        candidate_ids = [candidate.id for candidate in candidates if candidate.id is not None]
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

    events_by_candidate = {event.candidate_id: event for event in events}
    candidates_by_session: dict[int, list[HighlightCandidate]] = defaultdict(list)
    tasks_by_session: dict[int, list[SegmentTask]] = defaultdict(list)
    segment_counts: dict[int, int] = defaultdict(int)
    for candidate in candidates:
        candidates_by_session[candidate.session_id].append(candidate)
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
        visible_count = 0
        rejected_count = 0
        pending_review_count = 0
        for candidate in session_candidates:
            event = events_by_candidate.get(candidate.id)
            if _candidate_is_rejected(candidate, event):
                rejected_count += 1
                continue
            visible_count += 1
            if event is None or event.review_status == ReviewStatus.PENDING:
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


def get_session_timeline(session_id: int, *, include_rejected: bool = False) -> dict[str, Any]:
    """返回一场录制的 GMT+8 时间轴与所有高光节点。"""
    with get_session() as db:
        session = db.get(RecordingSession, session_id)
        if session is None:
            raise ValueError(f"录制会话不存在: session_id={session_id}")
        candidates = db.exec(
            select(HighlightCandidate)
            .where(HighlightCandidate.session_id == session_id)
            .order_by(HighlightCandidate.peak_ts.asc())
        ).all()
        candidate_ids = [candidate.id for candidate in candidates if candidate.id is not None]
        events = (
            db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id.in_(candidate_ids))).all()
            if candidate_ids
            else []
        )
        event_by_candidate = {event.candidate_id: event for event in events}
        tasks = db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).all()
        segment_count = len(db.exec(select(RawSegment.id).where(RawSegment.session_id == session_id)).all())
        pending_reanalysis = db.get(AppSetting, f"{_PENDING_REANALYSIS_PREFIX}{session_id}") is not None
        source = source_identities_for_sessions(db, [session_id]).get(session_id, unknown_source_identity())

    points = []
    for candidate in candidates:
        event = event_by_candidate.get(candidate.id)
        rejected = _candidate_is_rejected(candidate, event)
        if rejected and not include_rejected:
            continue
        points.append(_timeline_point(session, candidate, event, rejected=rejected))

    return {
        "session": {
            "session_id": session_id,
            "status": session.status,
            "started_at": _iso_utc(session.started_at),
            "ended_at": _iso_utc(session.ended_at),
            "started_at_gmt8": _iso_gmt8(session.started_at),
            "ended_at_gmt8": _iso_gmt8(session.ended_at),
            "duration_s": _duration_s(session.started_at, session.ended_at),
            "segment_count": segment_count,
            "processing_state": _processing_state(session, tasks, pending_reanalysis=pending_reanalysis),
            **source,
        },
        "timezone": "GMT+8",
        "points": points,
        "counts": {
            "visible": sum(1 for point in points if not point["rejected"]),
            "rejected": sum(
                1 for candidate in candidates if _candidate_is_rejected(candidate, event_by_candidate.get(candidate.id))
            ),
            "total": len(candidates),
        },
    }


def _timeline_point(
    session: RecordingSession,
    candidate: HighlightCandidate,
    event: HighlightEvent | None,
    *,
    rejected: bool,
) -> dict[str, Any]:
    payload = decode_features(event.features_json if event and event.features_json else candidate.features_json)
    timeline = payload.get("timeline") if isinstance(payload.get("timeline"), dict) else {}
    model_features = payload.get("features") if isinstance(payload.get("features"), dict) else payload
    raw_signals = timeline.get("source_signals")
    signals = [str(item) for item in raw_signals if str(item).strip()] if isinstance(raw_signals, list) else []
    if not signals:
        numeric_features = {
            str(key): float(value) for key, value in model_features.items() if isinstance(value, (float, int))
        }
        signals = source_signals(numeric_features)
    raw_danmaku = timeline.get("representative_danmaku")
    danmaku = _representative_danmaku_payload(raw_danmaku)
    start_ts = event.adjusted_start_ts if event and event.adjusted_start_ts else candidate.start_ts
    end_ts = event.adjusted_end_ts if event and event.adjusted_end_ts else candidate.end_ts
    confidence = timeline.get("confidence")
    confidence_value = float(confidence) if isinstance(confidence, (float, int)) else float(candidate.highlight_score)
    review_status = event.review_status if event else ReviewStatus.PENDING
    summary = event.reason if event and event.reason else candidate.reason
    plugin = payload.get("highlight_plugin") if isinstance(payload.get("highlight_plugin"), dict) else None
    analysis_window = payload.get("analysis_window") if isinstance(payload.get("analysis_window"), dict) else None
    return {
        "candidate_id": candidate.id,
        "event_id": event.id if event else None,
        "clock_gmt8": _clock_gmt8(candidate.peak_ts),
        "peak_at_gmt8": _iso_gmt8(candidate.peak_ts),
        "start_at_gmt8": _iso_gmt8(start_ts),
        "end_at_gmt8": _iso_gmt8(end_ts),
        "offset_s": round((_as_utc(candidate.peak_ts) - _as_utc(session.started_at)).total_seconds(), 3),
        "duration_s": round(max(0.0, (_as_utc(end_ts) - _as_utc(start_ts)).total_seconds()), 3),
        "summary": summary or "待生成高光梗概",
        "representative_danmaku": danmaku,
        "confidence": round(max(0.0, min(1.0, confidence_value)), 3),
        "source_signals": signals,
        "review_status": review_status,
        "candidate_status": candidate.status,
        "rejected": rejected,
        "review_url": f"/review/{candidate.id}",
        "preview_url": f"/review/api/{candidate.id}/preview",
        "provenance": {
            "analysis_version": int(timeline.get("analysis_version", 0) or 0),
            "rule_score": round(float(candidate.rule_score), 4),
            "llm_score": round(float(candidate.llm_score), 4),
            "highlight_score": round(float(candidate.highlight_score), 4),
            "dynamic_bounds": bool(timeline.get("dynamic_bounds", False)),
            "cross_segment": bool(timeline.get("cross_segment", False)),
            "danmaku_lag_s": float(timeline.get("danmaku_lag_s", 0.0) or 0.0),
            "analysis_window": analysis_window,
            "highlight_plugin": plugin,
        },
    }


def _candidate_is_rejected(candidate: HighlightCandidate, event: HighlightEvent | None) -> bool:
    return candidate.status == CandidateStatus.REJECTED or bool(
        event is not None and event.review_status in _REJECTED_REVIEWS
    )


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
