"""主题管理."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator


class MergeTopicsRequest(BaseModel):
    """合并主题请求体（将 source_id 并入 target_id）。"""

    source_id: int
    target_id: int


class TopicUpdateRequest(BaseModel):
    """主题信息更新请求体。"""

    title: str | None = None
    summary: str | None = None
    keywords_json: str | None = None
    status: str | None = None
    is_collection: bool | None = None


class SplitTopicRequest(BaseModel):
    """拆分主题请求体（提取指定事件组成新主题）。"""

    event_ids: list[int]

    @field_validator("event_ids")
    @classmethod
    def _non_empty_ids(cls, v):
        if not v:
            raise ValueError("event_ids empty")
        return v


class ReorderTopicRequest(BaseModel):
    """主题内视频重排序请求体。"""

    event_ids: list[int]


router = APIRouter()


@router.get("/topics")
def list_topics(session_id: int | None = None) -> dict[str, Any]:
    """获取主题列表。"""
    from app.analysis.topic_cluster import list_topics as _lt

    return {"topics": _lt(session_id=session_id)}


@router.get("/topics/{topic_id}")
def get_topic(topic_id: int) -> dict[str, Any]:
    """获取单个主题详情。"""
    from app.analysis.topic_cluster import get_topic as _gt

    t = _gt(topic_id)
    if t is None:
        raise HTTPException(status_code=404, detail="主题不存在")
    return t


@router.patch("/topics/{topic_id}")
def update_topic(topic_id: int, body: TopicUpdateRequest) -> dict[str, str]:
    """更新主题属性(title/summary/keywords/status/is_collection)。"""
    from app.analysis.topic_cluster import update_topic as _ut

    ok = _ut(topic_id, **body.model_dump(exclude_none=True))
    if not ok:
        raise HTTPException(status_code=404, detail="主题不存在")
    return {"status": "updated"}


@router.post("/topics/{topic_id}/events/{event_id}")
def add_event_to_topic(topic_id: int, event_id: int) -> dict[str, str]:
    """将事件加入主题。"""
    from app.analysis.topic_cluster import add_event_to_topic as _ae

    _ae(event_id, topic_id)
    return {"status": "added"}


@router.delete("/topics/{topic_id}/events/{event_id}")
def remove_event_from_topic(topic_id: int, event_id: int) -> dict[str, str]:
    """从主题移除事件。"""
    from app.analysis.topic_cluster import remove_event_from_topic as _re

    ok = _re(event_id, topic_id)
    if not ok:
        raise HTTPException(status_code=404, detail="关联不存在")
    return {"status": "removed"}


@router.post("/topics/merge")
def merge_topics(req: MergeTopicsRequest) -> dict[str, str]:
    """合并两个主题。"""
    from app.analysis.topic_cluster import merge_topics as _mt

    ok = _mt(req.source_id, req.target_id)
    if not ok:
        raise HTTPException(status_code=400, detail="合并失败")
    return {"status": "merged"}


@router.post("/topics/{topic_id}/split")
def split_topic(topic_id: int, req: SplitTopicRequest) -> dict[str, Any]:
    """拆分主题:将指定事件移出并创建新主题。"""
    from app.analysis.topic_cluster import split_topic as _st

    new_id = _st(topic_id, req.event_ids)
    if new_id is None:
        raise HTTPException(status_code=400, detail="拆分失败")
    return {"status": "split", "new_topic_id": new_id}


@router.post("/topics/{topic_id}/reorder")
def reorder_topic_events(topic_id: int, req: ReorderTopicRequest) -> dict[str, str]:
    """重排主题内事件顺序。"""
    from app.analysis.topic_cluster import reorder_topic_events as _ro

    _ro(topic_id, req.event_ids)
    return {"status": "reordered"}


@router.post("/sessions/{session_id}/cluster")
def cluster_session_candidates(session_id: int) -> dict[str, Any]:
    """对一场直播的候选进行主题聚类。"""
    from app.analysis.topic_cluster import cluster_candidates

    topics = cluster_candidates(session_id)
    return {"status": "clustered", "topics": topics}
