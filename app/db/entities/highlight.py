"""数据库实体 — Highlight (V0.1.14.3)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from app.db.entities.base import CandidateStatus, ReviewStatus, utcnow


class HighlightCandidate(SQLModel, table=True):
    """高光候选(``highlight_candidates``):达阈值待切片的爆点。"""

    __tablename__ = "highlight_candidates"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    peak_ts: datetime = Field(description="爆点时刻")
    start_ts: datetime = Field(description="建议切片起点(含前置留白)")
    end_ts: datetime = Field(description="建议切片终点(含后置留白)")
    rule_score: float = Field(default=0.0, description="规则打分")
    llm_score: float = Field(default=0.0, description="LLM 复核打分")
    highlight_score: float = Field(default=0.0, description="综合高光评分")
    features_json: str | None = Field(default=None, description="各维度特征 JSON")
    reason: str | None = Field(default=None, description="LLM 给出的高光理由")
    status: str = Field(default=CandidateStatus.PENDING, description="候选状态")
    dedup_hash: str | None = Field(default=None, index=True, description="内容指纹,用于查重")
    created_at: datetime = Field(default_factory=utcnow)


class HighlightEvent(SQLModel, table=True):
    """高光事件(``highlight_events``):V0.1.6 P1 拆分为独立事件模型。

    代表"直播中发生了一件值得剪辑的事情",与 highlight_candidates 共存。
    新增:人工调整边界、细粒度审核决断、主题归属、审核原因、ASR 文本留存。
    """

    __tablename__ = "highlight_events"

    id: int | None = Field(default=None, primary_key=True)
    candidate_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="highlight_candidates.id",
        description="关联 highlight_candidates.id(可空)",
        sa_column_kwargs={"unique": True},
    )  # noqa: E501
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    segment_id: int | None = Field(default=None, description="来源 raw_segments.id")

    # 时间边界(原始 + 人工调整)。
    raw_start_ts: datetime | None = Field(default=None, description="原始评分起点")
    raw_end_ts: datetime | None = Field(default=None, description="原始评分终点")
    adjusted_start_ts: datetime | None = Field(default=None, description="人工调整后起点")
    adjusted_end_ts: datetime | None = Field(default=None, description="人工调整后终点")

    # 评分。
    rule_score: float = Field(default=0.0)
    llm_score: float = Field(default=0.0)
    highlight_score: float = Field(default=0.0, description="综合高光评分")
    features_json: str | None = Field(default=None, description="各维度特征 JSON(含 danmaku_explain)")
    reason: str | None = Field(default=None, description="LLM 高光理由")
    asr_text: str | None = Field(default=None, description="ASR 转写文本(留存)")
    danmaku_explain_json: str | None = Field(default=None, description="弹幕评分解释 JSON")

    # 审核。
    review_status: str = Field(default=ReviewStatus.PENDING, description="审核决断")
    review_reason: str | None = Field(default=None, description="审核原因/备注")
    review_by: str = Field(default="auto", description="审核者:auto/manual")

    # 主题。
    topic_id: int | None = Field(default=None, index=True, description="所属 highlight_topics.id")

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # V0.1.12.7: 真实唯一约束
    __table_args__ = (UniqueConstraint("candidate_id", name="uq_highlight_event_candidate"),)


class HighlightTopic(SQLModel, table=True):
    """事件-主题关联(``highlight_topics``):多对多映射。

    V0.1.11-alpha: event_id 永远指向 HighlightEvent.id; confirmed_by_user 标记人工确认。
    """

    __tablename__ = "highlight_topics"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(index=True, foreign_key="highlight_events.id", description="关联 highlight_events.id")
    topic_id: int = Field(index=True, foreign_key="topics.id", description="关联 topics.id")
    confidence: float = Field(default=0.0, description="该事件属于本主题的相似度")
    is_manual: bool = Field(default=False, description="是否人工手动归类")
    sort_order: int = Field(default=0, description="在合集中的顺序")
    chapter_title: str | None = Field(default=None, description="合集内章节标题")
    confirmed_by_user: bool = Field(default=False, description="V0.1.11-alpha:已人工确认,后续自动聚类不覆盖")
    created_at: datetime = Field(default_factory=utcnow)

    # V0.1.12.7: 真实复合唯一约束
    __table_args__ = (UniqueConstraint("event_id", "topic_id", name="uq_topic_event_membership"),)
