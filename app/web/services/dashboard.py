"""Dashboard."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlmodel import select

from app.db.entities import (
    Danmaku,
    FinalClip,
    HighlightCandidate,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentStatus,
    SessionStatus,
)
from app.db.session import get_session
from app.sources.registry import source_registry
from app.sources.rooms import registered_rooms, room_source_view
from app.web.services.rooms import recorder_manager


def dashboard_state() -> dict[str, Any]:
    """汇总仪表盘所需的概览数据。

    :returns: 含房间、运行状态、计数的字典。
    """
    from sqlalchemy import func

    with get_session() as db:
        rooms = [room for room in registered_rooms(db) if room.platform != "local"]
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
                        SessionStatus.STOPPING,
                        SessionStatus.FINALIZING,
                    ]  # noqa: E501
                )
            )
        ).all()

    return {
        "rooms": [_room_dict(r, recorder_manager.status(r.id)) for r in rooms],
        "live_sources": [descriptor.model_dump(mode="json") for descriptor in source_registry.descriptors()],
        "counts": {"candidates": n_candidates, "clips": n_clips, "active_sessions": len(sessions)},
    }


def _room_dict(room: LiveRoom, runtime: dict[str, Any]) -> dict[str, Any]:
    """把房间转为可序列化字典并附带运行状态。"""
    from app.analysis.room_config import load_room_config
    from app.pipeline.live_monitor import live_monitor
    from app.recording.metadata import room_metadata_view

    with get_session() as db:
        metadata = room_metadata_view(db, room)
        source = room_source_view(room, db)

    return {
        "id": room.id,
        "room_id": room.room_id,
        **source,
        "input_url": room.input_url,
        "title": room.title,
        "metadata": metadata,
        "monitor": live_monitor.room_status(room, running=runtime["running"], runtime_state=runtime["state"]),
        "uploader_name": room.uploader_name,
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
        "running": runtime["running"],
        "recording_state": runtime["state"],
        "recording_message": runtime["message"],
        "active_session_id": runtime["session_id"],
        "pipeline_enabled": runtime["pipeline_enabled"],
        "schedule_enabled": room.schedule_enabled,
        "auto_threshold_enabled": room.auto_threshold_enabled,
        "danmaku_sentiment_enabled": room.danmaku_sentiment_enabled,
        # V0.1.6 P2: 房间配置。
        "room_config": load_room_config(room),
    }


def danmaku_overview(
    limit: int = 50, session_id: int | None = None, *, session_limit: int = 50, session_offset: int = 0
) -> dict[str, Any]:
    """返回最近弹幕与按会话聚合的弹幕热度统计。

    :param limit: 返回的最近弹幕条数。
    :param session_id: 仅查询指定会话(可选)。
    :param session_limit: 当前页最多返回的场次数。
    :param session_offset: 场次分页起点，不影响最近弹幕和全局计数。
    :returns: ``{"available", "recent": [...], "sessions": [...]}``。
    """
    with get_session() as db:
        stmt = select(Danmaku).order_by(Danmaku.ts.desc())  # type: ignore[attr-defined]
        if session_id is not None:
            stmt = stmt.where(Danmaku.session_id == session_id)
        recent_rows = db.exec(stmt.limit(limit)).all() if limit >= 0 else db.exec(stmt).all()[:limit]

        from app.web.services.danmaku_statistics import danmaku_page
        from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

        page = danmaku_page(db, session_id=session_id, limit=session_limit, offset=session_offset)
        source_session_ids = set(page.session_ids)
        source_session_ids.update(row.session_id for row in recent_rows)
        sources = source_identities_for_sessions(db, source_session_ids)

    from app.recording.danmaku_archive import archive_view

    sessions = [
        {
            "session_id": sid,
            "count": count if page.evidence[sid]["available"] or count else None,
            "intensity": round(weight, 2) if page.evidence[sid]["available"] or count else None,
            "intensity_unit": "Bilibili 事件权重"
            if sources.get(sid, unknown_source_identity())["platform"] == "bilibili"
            else "普通文本条数",
            "evidence": page.evidence[sid],
            "archive": archive_view(sid),
            **sources.get(sid, unknown_source_identity()),
        }
        for sid in page.session_ids
        for count, weight in [page.counts.get(sid, (0, 0.0))]
    ]
    recent = [
        {
            "session_id": d.session_id,
            "ts": d.ts.isoformat() if d.ts else None,
            "type": d.msg_type,
            "user": d.user,
            "content": d.content,
            **sources.get(d.session_id, unknown_source_identity()),
        }
        for d in recent_rows
    ]
    return {
        "available": page.available,
        "total": page.total_comments,
        "recent": recent,
        "sessions": sessions,
        "session_pagination": {
            "offset": session_offset,
            "limit": session_limit,
            "total": page.total_sessions,
            "has_more": session_offset + len(page.session_ids) < page.total_sessions,
        },
    }


def pipeline_progress(session_id: int | None = None) -> dict[str, Any]:
    """返回录制→转写→评分的流水线进度统计。

    :param session_id: 可选,限定某会话。
    :returns: 含各阶段计数的字典。
    """
    with get_session() as db:
        stmt = select(RawSegment.status, func.count()).group_by(RawSegment.status)
        if session_id is not None:
            stmt = stmt.where(RawSegment.session_id == session_id)
        counts = dict(db.exec(stmt).all())

    recorded = counts.get(SegmentStatus.RECORDED, 0)
    transcribed = counts.get(SegmentStatus.TRANSCRIBED, 0)
    scored = counts.get(SegmentStatus.SCORED, 0)
    total = sum(counts.values())
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
