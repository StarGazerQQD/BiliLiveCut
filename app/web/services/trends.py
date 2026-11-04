"""Trends."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.analysis.search_contracts import SearchEvidence
from app.core.config import settings
from app.web.services.notifications import push_notification


def _search_evidence(raw_json: str | None) -> dict[str, object] | None:
    """读取本次来源元数据；旧资料没有搜索证据时保留未知状态。"""
    raw = json.loads(raw_json or "{}")
    evidence = raw.get("search") if isinstance(raw, dict) else None
    return SearchEvidence.model_validate(evidence).model_dump(mode="json") if evidence is not None else None


def trends_overview(limit: int = 30, days: int = 7) -> dict[str, Any]:
    """返回网感资料库概览:近期热门条目 + 热词排行 + 是否启用。

    :param limit: 条目数量上限。
    :param days: 近期窗口(天)。
    :returns: ``{"enabled", "days", "items": [...], "keywords": [...]}``。
    """
    from app.trends import store as trend_store
    from app.trends.scheduler import trend_scheduler

    items = trend_store.recent_trends(limit=limit, days=days)
    keywords = trend_store.keyword_heat(days=days, top=24)
    return {
        "enabled": settings.trend_enabled,
        "web_search": settings.trend_web_search,
        "days": days,
        "scheduler": trend_scheduler.status(),
        "items": [
            {
                "id": it.id,
                "source": it.source,
                "category": it.category,
                "title": it.title,
                "summary": it.summary,
                "url": it.url,
                "tags": json.loads(it.tags_json or "[]"),
                "heat": round(it.heat, 1),
                "seen_count": it.seen_count,
                "collected_at": it.collected_at.isoformat() if it.collected_at else None,
                "search": _search_evidence(it.raw_json),
            }
            for it in items
        ],
        "keywords": keywords,
    }


async def collect_trends_now(topic: str = "") -> dict[str, Any]:
    """立即触发一次网感采集(在线程池中执行,避免阻塞事件循环)。

    :param topic: 采集主题提示。
    :returns: ``{"enabled", "saved"}``。
    """
    if not settings.trend_enabled:
        return {"enabled": False, "saved": 0, "note": "网感资料库未启用(TREND_ENABLED=false)。"}
    from app.trends.collector import collect_and_save

    saved = await asyncio.to_thread(collect_and_save, topic)
    push_notification(f"网感采集完成,新增/更新 {saved} 条。")
    return {"enabled": True, "saved": saved}
