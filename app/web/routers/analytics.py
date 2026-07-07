"""统计分析."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter()


@router.get("/analytics")
def get_analytics() -> dict[str, Any]:
    """返回 Dashboard 统计分析数据。

    包含:
    - 录制趋势(近 30 天每日录制会话数/时长)
    - 切片统计(总数/已发布/总时长)
    - 候选分布(分数区间分布/状态分布)
    - 直播间排行(按切片数)
    """
    from datetime import UTC, datetime, timedelta

    from sqlmodel import func
    from sqlmodel import select as _sel

    from app.db.models import (
        ClipStatus,
        FinalClip,
        HighlightCandidate,
        LiveRoom,
        RawSegment,
        RecordingSession,
        TaskStatus,
    )
    from app.db.session import get_session

    now = datetime.now(UTC)
    days_30 = now - timedelta(days=30)

    with get_session() as db:
        # --- 切片统计 ---
        total_clips = db.exec(_sel(func.count()).select_from(FinalClip)).one()
        published_clips = db.exec(
            _sel(func.count()).select_from(FinalClip).where(FinalClip.status == ClipStatus.PUBLISHED)
        ).one()
        total_duration = (
            db.exec(_sel(func.coalesce(func.sum(FinalClip.duration_s), 0)).select_from(FinalClip)).one() or 0.0
        )

        # --- 候选统计 ---
        total_candidates = db.exec(_sel(func.count()).select_from(HighlightCandidate)).one()
        approved_candidates = db.exec(
            _sel(func.count()).select_from(HighlightCandidate).where(HighlightCandidate.status == "approved")
        ).one()
        avg_score = (
            db.exec(
                _sel(func.coalesce(func.avg(HighlightCandidate.highlight_score), 0.0)).select_from(HighlightCandidate)
            ).one()
            or 0.0
        )

        # 分数区间分布
        score_buckets = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.85": 0, "0.85-1.0": 0}
        all_scores = db.exec(_sel(HighlightCandidate.highlight_score).select_from(HighlightCandidate)).all()
        for s in all_scores:
            s = s or 0
            if s < 0.3:
                score_buckets["0.0-0.3"] += 1
            elif s < 0.5:
                score_buckets["0.3-0.5"] += 1
            elif s < 0.7:
                score_buckets["0.5-0.7"] += 1
            elif s < 0.85:
                score_buckets["0.7-0.85"] += 1
            else:
                score_buckets["0.85-1.0"] += 1

        # --- 录制统计 ---
        total_sessions = db.exec(_sel(func.count()).select_from(RecordingSession)).one()
        finished_sessions = db.exec(
            _sel(func.count()).select_from(RecordingSession).where(RecordingSession.ended_at is not None)
        ).one()
        total_reconnects = (
            db.exec(
                _sel(func.coalesce(func.sum(RecordingSession.reconnect_count), 0)).select_from(RecordingSession)
            ).one()
            or 0
        )

        # 原始数据量
        total_raw_gb = (
            db.exec(_sel(func.coalesce(func.sum(RawSegment.size_bytes), 0.0)).select_from(RawSegment)).one() or 0.0
        )
        total_raw_gb = round(total_raw_gb / (1024**3), 2)  # size_bytes → GB

        # --- 任务统计 ---
        from app.db.models import SegmentTask

        task_failed = db.exec(
            _sel(func.count()).select_from(SegmentTask).where(SegmentTask.stage == TaskStatus.FAILED)
        ).one()

        # --- 每日趋势(近 30 天) ---
        daily_record: list[dict[str, Any]] = []
        for i in range(30):
            day = days_30 + timedelta(days=i)
            day_end = day + timedelta(days=1)
            sessions_count = db.exec(
                _sel(func.count())
                .select_from(RecordingSession)
                .where(
                    RecordingSession.started_at >= day,
                    RecordingSession.started_at < day_end,
                )
            ).one()
            clips_count = db.exec(
                _sel(func.count())
                .select_from(FinalClip)
                .where(
                    FinalClip.created_at >= day,
                    FinalClip.created_at < day_end,
                )
            ).one()
            candidates_count = db.exec(
                _sel(func.count())
                .select_from(HighlightCandidate)
                .where(
                    HighlightCandidate.created_at >= day,
                    HighlightCandidate.created_at < day_end,
                )
            ).one()
            daily_record.append(
                {
                    "date": day.strftime("%m-%d"),
                    "sessions": sessions_count,
                    "clips": clips_count,
                    "candidates": candidates_count,
                }
            )

        # --- 直播间排行(按切片数 TOP 10) ---
        room_ranks: list[dict[str, Any]] = []
        rows = db.exec(
            _sel(
                LiveRoom.room_id,
                LiveRoom.uploader_name,
                func.count(FinalClip.id).label("cnt"),
                func.coalesce(func.sum(FinalClip.duration_s), 0.0).label("dur"),
            )
            .select_from(LiveRoom)
            .join(RecordingSession, RecordingSession.room_id == LiveRoom.id, isouter=True)
            .join(HighlightCandidate, HighlightCandidate.session_id == RecordingSession.id, isouter=True)
            .join(FinalClip, FinalClip.candidate_id == HighlightCandidate.id, isouter=True)
            .where(LiveRoom.uploader_name is not None)
            .group_by(LiveRoom.id)
            .order_by(func.count(FinalClip.id).desc())
            .limit(10)
        ).all()
        for r in rows:
            room_ranks.append(
                {
                    "name": r.uploader_name or f"房间{r.room_id}",
                    "clips": r.cnt,
                    "duration_h": round((r.dur or 0) / 3600, 1),
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
            "total_raw_gb": round(total_raw_gb, 1),
            "task_failed": task_failed,
        },
        "score_distribution": score_buckets,
        "daily_trend": daily_record,
        "room_ranking": room_ranks,
    }
