"""Dashboard."""

from __future__ import annotations

import json
from typing import Any

from sqlmodel import select

from app.db.models import (
    Danmaku,
    FinalClip,
    HighlightCandidate,
    LiveRoom,
    RawSegment,
    RecordingSession,
    RoomMode,
    SegmentStatus,
    SessionStatus,
)
from app.db.session import get_session
from app.web.services.rooms import recorder_manager


def dashboard_state() -> dict[str, Any]:
    """汇总仪表盘所需的概览数据。

    :returns: 含房间、运行状态、计数的字典。
    """
    from sqlalchemy import func

    with get_session() as db:
        rooms = db.exec(select(LiveRoom)).all()
        n_candidates = db.scalar(select(func.count()).select_from(HighlightCandidate)) or 0
        n_clips = db.scalar(select(func.count()).select_from(FinalClip)) or 0
        sessions = db.exec(
            select(RecordingSession).where(
                RecordingSession.status.in_(  # type: ignore[attr-defined]
                    [
                        SessionStatus.RECORDING,
                        SessionStatus.RECONNECTING,
                        SessionStatus.STARTING,
                        SessionStatus.RECONNECTED,
                    ]  # noqa: E501
                )
            )
        ).all()

    running = set(recorder_manager.running_ids())
    return {
        "rooms": [_room_dict(r, r.id in running) for r in rooms],
        "counts": {"candidates": n_candidates, "clips": n_clips, "active_sessions": len(sessions)},
        "modes": [RoomMode.MANUAL, RoomMode.SEMI, RoomMode.AUTO],
    }


def _room_dict(room: LiveRoom, running: bool) -> dict[str, Any]:
    """把房间转为可序列化字典并附带运行状态。"""
    return {
        "id": room.id,
        "room_id": room.room_id,
        "input_url": room.input_url,
        "title": room.title,
        "uploader_name": room.uploader_name,
        "mode": room.mode,
        "highlight_threshold": room.highlight_threshold,
        "auto_publish_threshold": room.auto_publish_threshold,
        # V0.1.6: 独立自动化开关。
        "auto_record": room.auto_record,
        "auto_analyze": room.auto_analyze,
        "auto_render": room.auto_render,
        "auto_approve": room.auto_approve,
        "auto_upload": room.auto_upload,
        "auto_approve_threshold": room.auto_approve_threshold,
        "review_threshold": room.review_threshold,
        "authorized": room.authorized,
        "enabled": room.enabled,
        "running": running,
        "schedule_enabled": room.schedule_enabled,
        "auto_threshold_enabled": room.auto_threshold_enabled,
        "danmaku_sentiment_enabled": room.danmaku_sentiment_enabled,
        # V0.1.6 P2: 房间配置。
        "room_config": json.loads(room.room_config_json) if room.room_config_json else {},
    }


def danmaku_overview(limit: int = 50, session_id: int | None = None) -> dict[str, Any]:
    """返回最近弹幕与按会话聚合的弹幕热度统计。

    :param limit: 返回的最近弹幕条数。
    :param session_id: 仅查询指定会话(可选)。
    :returns: ``{"available", "recent": [...], "sessions": [...]}``。
    """
    with get_session() as db:
        stmt = select(Danmaku).order_by(Danmaku.ts.desc())  # type: ignore[attr-defined]
        if session_id is not None:
            stmt = stmt.where(Danmaku.session_id == session_id)
        recent_rows = db.exec(stmt).all()[:limit]

        # 按会话聚合数量(简单热度指标)。
        agg_stmt = select(Danmaku.session_id, Danmaku.value)
        if session_id is not None:
            agg_stmt = agg_stmt.where(Danmaku.session_id == session_id)
        all_rows = db.exec(agg_stmt).all()

    counts: dict[int, dict[str, float]] = {}
    for sid, value in all_rows:
        bucket = counts.setdefault(sid, {"count": 0.0, "intensity": 0.0})
        bucket["count"] += 1
        bucket["intensity"] += float(value)

    sessions = [
        {"session_id": sid, "count": int(v["count"]), "intensity": round(v["intensity"], 2)}
        for sid, v in sorted(counts.items(), reverse=True)
    ]
    recent = [
        {
            "session_id": d.session_id,
            "ts": d.ts.isoformat() if d.ts else None,
            "type": d.msg_type,
            "user": d.user,
            "content": d.content,
        }
        for d in recent_rows
    ]
    return {"available": True, "total": len(all_rows), "recent": recent, "sessions": sessions}


def pipeline_progress(session_id: int | None = None) -> dict[str, Any]:
    """返回录制→转写→评分的流水线进度统计。

    :param session_id: 可选,限定某会话。
    :returns: 含各阶段计数的字典。
    """
    with get_session() as db:
        stmt = select(RawSegment)
        if session_id is not None:
            stmt = stmt.where(RawSegment.session_id == session_id)
        segments = db.exec(stmt).all()

    recorded = sum(1 for s in segments if s.status == SegmentStatus.RECORDED)
    transcribed = sum(1 for s in segments if s.status == SegmentStatus.TRANSCRIBED)
    scored = sum(1 for s in segments if s.status == SegmentStatus.SCORED)

    total = len(segments)
    return {
        "total_segments": total,
        "recorded": recorded,
        "transcribed": transcribed,
        "scored": scored,
        "progress_pct": round(scored / total * 100, 1) if total > 0 else 0,
        "active_session_id": session_id,
    }


# --------------------------------------------------------------------------- #
# 阈值自学习查询(V0.1.2)
# --------------------------------------------------------------------------- #
