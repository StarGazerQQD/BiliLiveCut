"""数据库实体 — Topic (V0.1.14.3)."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.entities.base import TopicStatus, utcnow


class Topic(SQLModel, table=True):
    """主题/事件簇(``topics``):同一直播中语义相关的多个高光。

    主题判定分为三级:同一主题 > 可能相关 > 不同主题。
    """

    __tablename__ = "topics"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    title: str | None = Field(default=None, description="主题标题")
    summary: str | None = Field(default=None, description="主题摘要")
    keywords_json: str | None = Field(default=None, description="关键词 JSON 数组")
    entities_json: str | None = Field(default=None, description="实体 JSON(人物/游戏/歌曲等)")
    confidence: float = Field(default=0.0, description="主题置信度")
    status: str = Field(default=TopicStatus.AUTO, description="auto/confirmed/split/blocked")
    is_collection: bool = Field(default=False, description="是否适合生成合集")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
