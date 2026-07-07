"""网感资料库."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.web import service

_MAX_QUERY_LIMIT = 500
_MAX_QUERY_DAYS = 365


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


class TrendCollectRequest(BaseModel):
    """网感资料收集请求体。"""

    topic: str = ""


router = APIRouter()


@router.get("/trends")
def get_trends(limit: int = 30, days: int = 7) -> dict[str, Any]:
    """返回网感资料库概览(近期热门条目 + 热词排行)。"""
    limit = _clamp(limit, 1, _MAX_QUERY_LIMIT)
    days = _clamp(days, 1, _MAX_QUERY_DAYS)
    return service.trends_overview(limit=limit, days=days)


@router.post("/trends/collect")
async def collect_trends(req: TrendCollectRequest | None = None) -> dict[str, Any]:
    """立即触发一次联网采集并写入资料库。"""
    topic = req.topic if req else ""
    return await service.collect_trends_now(topic=topic or "")
