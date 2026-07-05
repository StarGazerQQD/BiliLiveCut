"""共享工具 (v0.1.10.1-HL-Alpha)。消除 builder.py / self_learn.py 重复代码。"""
from __future__ import annotations


def load_feedback(room_id: int | None = None) -> list[dict]:
    try:
        from app.db.models import ThresholdFeedback
        from app.db.session import get_session
        from sqlmodel import select
        with get_session() as db:
            stmt = select(ThresholdFeedback).where(ThresholdFeedback.action.in_(["approved", "rejected"]))
            if room_id is not None: stmt = stmt.where(ThresholdFeedback.room_id == room_id)
            rows = db.exec(stmt).all()
        return [{"candidate_id": r.candidate_id, "room_id": r.room_id, "action": r.action, "highlight_score": r.highlight_score} for r in rows]
    except Exception:
        return []


def candidate_to_segment(candidate_id: int) -> int | None:
    try:
        from app.db.models import HighlightCandidate, RawSegment
        from app.db.session import get_session
        from sqlmodel import select
        with get_session() as db:
            cand = db.get(HighlightCandidate, candidate_id)
            if cand is None: return None
            seg = db.exec(select(RawSegment).where(RawSegment.session_id == cand.session_id, RawSegment.start_ts <= cand.peak_ts, RawSegment.end_ts >= cand.peak_ts).limit(1)).first()
            return seg.id if seg else None
    except Exception:
        return None
