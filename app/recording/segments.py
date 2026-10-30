# ruff: noqa: D100
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.db.entities import RawSegment, SegmentStatus


def register_segment(
    db: Session,
    session_id: int,
    path: Path,
    *,
    duration: float,
    ended_at: datetime,
    size: int,
) -> tuple[RawSegment, bool]:
    """在短写事务内按场次与规范路径幂等登记，序号继续当前场次。"""
    canonical = str(path.resolve())
    existing = db.exec(
        select(RawSegment).where(
            RawSegment.session_id == session_id,
            RawSegment.file_path == canonical,
        )
    ).first()
    if existing is not None:
        return existing, False
    sequences = db.exec(select(RawSegment.seq).where(RawSegment.session_id == session_id)).all()
    segment = RawSegment(
        session_id=session_id,
        seq=max(sequences, default=-1) + 1,
        file_path=canonical,
        start_ts=ended_at - timedelta(seconds=duration),
        end_ts=ended_at,
        duration_s=duration,
        size_bytes=size,
        status=SegmentStatus.RECORDED,
    )
    db.add(segment)
    db.flush()
    db.refresh(segment)
    return segment, True
