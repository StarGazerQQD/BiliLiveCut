# ruff: noqa: D100
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SearchSource(BaseModel):
    """搜索服务实际返回的来源；发布时间未知时保留为空。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str
    url: str
    title: str
    site_name: str | None
    published_at: datetime | None


class SearchEvidence(BaseModel):
    """一次真实检索的来源与采集时刻，不包含素材正文或凭据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    collected_at: datetime
    sources: list[SearchSource] = Field(max_length=50)


class SearchResult(BaseModel):
    """DeepSeek 官方 Search 响应的一条结果。"""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    title: str
    url: HttpUrl
    snippet: str
    summary: str
    content: str
    site_name: str | None
    site_icon: str | None
    date: datetime | None
    rerank_score: float = Field(ge=0, le=3)
    authority: int = Field(ge=-1, le=4)


class SearchResponse(BaseModel):
    """有界检索结果集；空结果不等于已取得可引用来源。"""

    model_config = ConfigDict(extra="ignore")
    results: list[SearchResult] = Field(max_length=50)
