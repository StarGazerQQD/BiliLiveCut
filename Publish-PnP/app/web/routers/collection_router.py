"""P2 合集编辑器路由(V0.1.6)。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

collection_router = APIRouter(prefix="/collection", tags=["collection"])


class ReorderEventsRequest(BaseModel):
    """重排事件请求。"""

    event_ids: list[int]
    chapter_titles: dict[str, str] | None = None


class RenderCollectionRequest(BaseModel):
    """渲染合集请求。"""

    event_ids: list[int]
    chapter_titles: list[str] | None = None
    include_chapter_cards: bool = True


_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "web" / "templates")
)


@collection_router.get("/{topic_id}", response_class=HTMLResponse)
async def collection_page(request: Request, topic_id: int) -> HTMLResponse:
    """合集编辑器页面。"""
    return _TEMPLATES.TemplateResponse(
        "collection.html",
        {"request": request, "topic_id": topic_id},
    )


@collection_router.get("/api/{topic_id}")
def get_collection_data(topic_id: int) -> dict:
    """获取合集编辑所需数据:主题+事件列表+重叠检测。"""
    from app.pipeline.collection import detect_overlap, get_collection_events
    from app.analysis.topic_cluster import get_topic as _gt

    topic = _gt(topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="主题不存在")
    events = get_collection_events(topic_id)
    overlaps = detect_overlap(events)
    return {
        "topic": topic,
        "events": events,
        "overlaps": overlaps,
        "total_duration_s": sum(e.get("duration_s", 0) for e in events),
    }


@collection_router.post("/api/{topic_id}/reorder")
def reorder_events(topic_id: int, req: ReorderEventsRequest) -> dict:
    """保存事件顺序及章节标题。"""
    from app.analysis.topic_cluster import reorder_topic_events

    ok = reorder_topic_events(topic_id, req.event_ids, req.chapter_titles)
    if not ok:
        raise HTTPException(status_code=400, detail="排序保存失败")
    return {"status": "reordered"}


@collection_router.post("/api/{topic_id}/render")
async def render_collection(
    topic_id: int,
    req: RenderCollectionRequest,
) -> dict:
    """渲染合集 MP4(异步)。"""
    import asyncio
    from app.pipeline.collection import render_collection as _rc

    result = await asyncio.to_thread(
        _rc, topic_id, req.event_ids, req.chapter_titles, req.include_chapter_cards,
    )
    if result is None:
        raise HTTPException(status_code=500, detail="合集渲染失败(需至少2个可用成品)")
    return {
        "status": "rendered",
        "file_path": result.file_path,
        "duration_s": result.duration_s,
    }


@collection_router.post("/api/{topic_id}/copywriter")
def generate_copywriter(topic_id: int) -> dict:
    """为主题合集生成标题/简介/章节/标签等文案。"""
    from app.publishing.collection_copywriter import generate_copywriter_for_topic

    result = generate_copywriter_for_topic(topic_id)
    if result is None:
        raise HTTPException(status_code=500, detail="文案生成失败")
    return result
