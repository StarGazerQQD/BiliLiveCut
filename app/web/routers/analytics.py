"""统计分析."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import ColumnElement, and_, case
from sqlmodel import Session, func, select

from app.db.entities import (
    ClipStatus,
    FinalClip,
    HighlightCandidate,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentTask,
    TaskStatus,
)
from app.db.session import get_session
from app.sources.rooms import room_source_view

router = APIRouter()


def _daily_counts(db: Session, column: ColumnElement[datetime], start: datetime, end: datetime) -> dict[str, int]:
    day = func.date(column)
    rows = db.exec(select(day, func.count()).where(column >= start, column < end).group_by(day)).all()
    return {str(value): count for value, count in rows}


@router.get("/analytics")
def get_analytics(days: Annotated[int, Query(ge=1, le=365)] = 30) -> dict[str, object]:
    """按包含今天的 UTC 日历范围汇总录制、产物、候选和房间排行。"""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    clip_range = (FinalClip.created_at >= start, FinalClip.created_at < end)
    candidate_range = (HighlightCandidate.created_at >= start, HighlightCandidate.created_at < end)
    session_range = (RecordingSession.started_at >= start, RecordingSession.started_at < end)

    with get_session() as db:
        total_clips, published_clips, total_duration = db.exec(
            select(
                func.count(FinalClip.id),
                func.count(case((FinalClip.status == ClipStatus.PUBLISHED, 1))),
                func.coalesce(func.sum(FinalClip.duration_s), 0.0),
            ).where(*clip_range)
        ).one()
        total_candidates, approved_candidates, avg_score = db.exec(
            select(
                func.count(HighlightCandidate.id),
                func.count(case((HighlightCandidate.status == "approved", 1))),
                func.coalesce(func.avg(HighlightCandidate.highlight_score), 0.0),
            ).where(*candidate_range)
        ).one()
        score_buckets = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
        score = func.coalesce(HighlightCandidate.highlight_score, 0)
        bucket = case(
            (score < 0.3, "0.0-0.3"),
            (score < 0.5, "0.3-0.5"),
            (score < 0.7, "0.5-0.7"),
            (score < 0.85, "0.7-0.85"),
            else_="0.85-1.0",
        )
        for name, count in db.exec(select(bucket, func.count()).where(*candidate_range).group_by(bucket)).all():
            score_buckets[name] = count
        total_sessions, finished_sessions, total_reconnects = db.exec(
            select(
                func.count(RecordingSession.id),
                func.count(RecordingSession.ended_at),
                func.coalesce(func.sum(RecordingSession.reconnect_count), 0),
            ).where(*session_range)
        ).one()
        raw_bytes = db.exec(
            select(func.coalesce(func.sum(RawSegment.size_bytes), 0))
            .join(RecordingSession, RecordingSession.id == RawSegment.session_id)
            .where(*session_range)
        ).one()
        task_failed = db.exec(
            select(func.count(SegmentTask.id)).where(
                SegmentTask.stage == TaskStatus.FAILED, SegmentTask.created_at >= start, SegmentTask.created_at < end
            )
        ).one()

        sessions = _daily_counts(db, RecordingSession.started_at, start, end)
        clips = _daily_counts(db, FinalClip.created_at, start, end)
        candidates = _daily_counts(db, HighlightCandidate.created_at, start, end)
        daily_record = []
        for offset in range(days):
            day = (start + timedelta(days=offset)).date().isoformat()
            daily_record.append(
                {
                    "date": day,
                    "sessions": sessions.get(day, 0),
                    "clips": clips.get(day, 0),
                    "candidates": candidates.get(day, 0),
                }
            )

        room_ranks: list[dict[str, object]] = []
        rows = db.exec(
            select(LiveRoom, func.count(FinalClip.id), func.coalesce(func.sum(FinalClip.duration_s), 0.0))
            .join(RecordingSession, RecordingSession.room_id == LiveRoom.id, isouter=True)
            .join(HighlightCandidate, HighlightCandidate.session_id == RecordingSession.id, isouter=True)
            .join(FinalClip, and_(FinalClip.candidate_id == HighlightCandidate.id, *clip_range), isouter=True)
            .group_by(LiveRoom.id)
            .order_by(func.count(FinalClip.id).desc(), LiveRoom.id)
            .limit(10)
        ).all()
        for room, count, duration in rows:
            source = room_source_view(room, db) if room.platform != "local" else None
            name = (room.uploader_name or "").strip() or f"房间{room.room_id or room.id}"
            if source and source["platform"] != "bilibili":
                name = f"{source['platform']} · {name}"
            room_ranks.append(
                {
                    "name": name,
                    "room_db_id": room.id,
                    "platform": room.platform,
                    "source_id": source["source_id"] if source else None,
                    "clips": count,
                    "duration_h": round(duration / 3600, 1),
                }
            )

    return {
        "overview": {
            "total_clips": total_clips,
            "published_clips": published_clips,
            "total_duration_h": round(total_duration / 3600, 1),
            "total_candidates": total_candidates,
            "approved_candidates": approved_candidates,
            "avg_highlight_score": round(avg_score, 3),
            "total_sessions": total_sessions,
            "finished_sessions": finished_sessions,
            "total_reconnects": total_reconnects,
            "total_raw_gb": round(raw_bytes / (1024**3), 1),
            "task_failed": task_failed,
        },
        "score_distribution": score_buckets,
        "daily_trend": daily_record,
        "room_ranking": room_ranks,
    }
