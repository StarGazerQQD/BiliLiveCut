"""版本管理 (V0.1.14.2)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter()


@router.get("/events/{event_id}/variants")
def list_variants(event_id: int) -> list[dict[str, Any]]:
    """列出某事件的所有成品版本。"""
    from app.db.models import ClipVariant
    from app.db.session import get_session

    with get_session() as db:
        from sqlmodel import select as _sel

        variants = db.exec(
            _sel(ClipVariant).where(ClipVariant.event_id == event_id).order_by(ClipVariant.created_at.desc())
        ).all()
    return [
        {
            "id": v.id,
            "variant_type": v.variant_type,
            "has_subtitles": v.has_subtitles,
            "resolution": v.resolution,
            "file_path": v.file_path,
            "file_hash": v.file_hash,
            "render_status": v.render_status,
            "version_number": v.version_number,
            "duration_s": v.duration_s,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in variants
    ]
