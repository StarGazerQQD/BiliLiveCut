"""数据库实体 — Clip."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from app.db.entities.base import ClipStatus, ClipVariantType, RenderStatus, utcnow


class FinalClip(SQLModel, table=True):
    """成品切片(``final_clips``):后处理完成、可投稿的 MP4 及其元数据。"""

    __tablename__ = "final_clips"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int = Field(index=True, description="来源 highlight_candidates.id")
    file_path: str = Field(description="成品 MP4 路径")
    cover_path: str | None = Field(default=None, description="封面图路径")
    duration_s: float | None = Field(default=None, description="时长(秒)")
    width: int | None = Field(default=None, description="宽")
    height: int | None = Field(default=None, description="高")
    title: str | None = Field(default=None, description="标题")
    description: str | None = Field(default=None, description="简介")
    tags_json: str | None = Field(default=None, description="标签 JSON 数组")
    publish_suggestion: str | None = Field(default=None, description="发布时间/是否值得发布建议")
    content_hash: str | None = Field(default=None, index=True, description="内容指纹")
    status: str = Field(default=ClipStatus.GENERATED, description="切片状态")
    created_at: datetime = Field(default_factory=utcnow)


class ClipVariant(SQLModel, table=True):
    """成品版本(``clip_variants``):同一事件的不同渲染版本。

    一个 HighlightEvent 可产生多个 ClipVariant:
    - 单段高光版(single)
    - 完整上下文版(full_context)
    - 合集章节(collection_chapter)
    - 带字幕版(subtitled)
    - 无字幕净版(no_subtitles)
    - 投稿压制版(compressed)
    - 高码率归档版(archive)
    """

    __tablename__ = "clip_variants"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(index=True, foreign_key="highlight_events.id", description="关联 highlight_events.id")
    candidate_id: int | None = Field(
        default=None, index=True, description="[已废弃 V0.1.12.2]关联 highlight_candidates.id(仅向后兼容)"
    )  # noqa: E501

    variant_type: str = Field(default=ClipVariantType.SINGLE, description="版本类型")
    render_config_hash: str | None = Field(
        default=None, description="V0.1.12.5:渲染配置哈希,与 event_id+variant_type 组成唯一约束"
    )  # noqa: E501

    # 渲染参数。
    start_ts: datetime | None = Field(default=None, description="实际渲染起点")
    end_ts: datetime | None = Field(default=None, description="实际渲染终点")
    has_subtitles: bool = Field(default=True, description="是否包含字幕")
    resolution: str | None = Field(default=None, description="输出分辨率,如 1920×1080")
    codec_params: str | None = Field(default=None, description="编码参数")

    # 文件。
    file_path: str | None = Field(default=None, description="文件路径")
    file_hash: str | None = Field(default=None, description="文件 SHA256")
    cover_path: str | None = Field(default=None, description="封面图路径")
    duration_s: float | None = Field(default=None, description="时长(秒)")

    render_status: str = Field(default=RenderStatus.QUEUED, description="渲染状态: queued/rendering/done/failed")
    version_number: int = Field(default=1, description="版本号(同 variant_type 同 event 递增)")
    generation: int = Field(default=1, description="文件替换代数 (每次重渲染递增, 崩溃恢复用)")
    backup_path: str | None = Field(default=None, description="替换前的旧正式文件备份路径")

    created_at: datetime = Field(default_factory=utcnow)

    # V0.1.12.7: event_id + variant_type + render_config_hash 三维唯一, 支持同类型多版本
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "variant_type",
            "render_config_hash",
            name="uq_clip_event_variant_config",
        ),
    )
