"""数据库实体 — HotspotEvent。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.db.entities.base import HotspotStatus, utcnow


class HotspotEvent(SQLModel, table=True):
    """热点事件(``hotspot_events``):独立于视频候选的直播事件事实。

    ``event_key`` 是检测链生成的稳定业务键。热点即使不值得成片也会保留；
    只有后续剪辑评分达到阈值时，``candidate_id`` 才会关联既有候选链。
    """

    __tablename__ = "hotspot_events"

    id: int | None = Field(default=None, primary_key=True)
    event_key: str = Field(description="检测链稳定业务键，用于幂等写入")
    session_id: int = Field(
        index=True,
        foreign_key="recording_sessions.id",
        description="所属 recording_sessions.id",
    )

    start_ts: datetime = Field(description="事件起点")
    peak_ts: datetime = Field(description="事件峰值时刻")
    end_ts: datetime = Field(description="事件终点")
    status: str = Field(default=HotspotStatus.PROVISIONAL, index=True, description="热点生命周期状态")

    heat_score: float = Field(default=0.0, description="热度分")
    clip_score: float = Field(default=0.0, description="成片价值分")
    semantic_confidence: float = Field(default=0.0, description="语义置信度")
    evidence_coverage: float = Field(default=0.0, description="证据覆盖率")

    title: str | None = Field(default=None, description="热点标题")
    summary: str | None = Field(default=None, description="热点摘要")
    category: str | None = Field(default=None, index=True, description="热点类别")
    features_json: str | None = Field(default=None, description="检测特征 JSON")
    evidence_json: str | None = Field(default=None, description="证据明细 JSON")
    representative_danmaku_json: str | None = Field(default=None, description="代表弹幕 JSON")
    transcript_text: str | None = Field(default=None, description="事件窗口转写文本")

    candidate_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="highlight_candidates.id",
        description="达到成片阈值后关联的候选，可空",
    )
    merged_into_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="hotspot_events.id",
        description="合并目标热点，可空",
    )

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    __table_args__ = (
        UniqueConstraint("event_key", name="uq_hotspot_event_key"),
        UniqueConstraint("candidate_id", name="uq_hotspot_candidate"),
        CheckConstraint("start_ts <= peak_ts AND peak_ts <= end_ts", name="ck_hotspot_time_order"),
        Index("ix_hotspot_session_status_peak", "session_id", "status", "peak_ts"),
    )
