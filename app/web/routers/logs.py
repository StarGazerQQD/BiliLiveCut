"""系统日志 (V0.1.14.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.web import service

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


router = APIRouter()


@router.get("/logs")
def get_logs(limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
    """返回系统日志(WARNING 及以上)。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    return service.list_logs(limit=limit, level=level)
