"""数据库实体 — Recording."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.entities.base import DanmakuType, SegmentStatus, utcnow


class RawSegment(SQLModel, table=True):
    """原始片段(``raw_segments``):FFmpeg 按固定时长切出的录制文件。"""

    __tablename__ = "raw_segments"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    seq: int = Field(description="片段序号(从 0 递增)")
    file_path: str = Field(description="本地文件路径")
    start_ts: datetime | None = Field(default=None, description="片段对应直播起始时间")
    end_ts: datetime | None = Field(default=None, description="片段对应直播结束时间")
    duration_s: float | None = Field(default=None, description="片段时长(秒)")
    size_bytes: int | None = Field(default=None, description="文件大小(字节)")
    status: str = Field(default=SegmentStatus.RECORDED, description="处理状态")


class Danmaku(SQLModel, table=True):
    """弹幕/互动事件(``danmaku``)。

    用于"弹幕热度"视图与高光评分中的弹幕维度。``ts`` 使用接收到的墙钟时间(UTC),
    与原始片段的 ``start_ts``/``end_ts`` 对齐以便按窗口统计速率。
    """

    __tablename__ = "danmaku"

    id: int | None = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, description="所属 recording_sessions.id")
    room_id: int = Field(index=True, description="真实房间号")
    ts: datetime = Field(default_factory=utcnow, index=True, description="接收时间(UTC)")
    msg_type: str = Field(default=DanmakuType.DANMAKU, description="消息类型")
    user: str | None = Field(default=None, description="发送者昵称")
    content: str | None = Field(default=None, description="弹幕文本/礼物名等")
    value: float = Field(default=1.0, description="价值权重(礼物/SC 价格,普通弹幕为 1)")


class RecordingSchedule(SQLModel, table=True):
    """录制预约(``recording_schedules``):预定时间自动启动录制。"""

    __tablename__ = "recording_schedules"

    id: int | None = Field(default=None, primary_key=True)
    room_id: int = Field(index=True, description="所属 live_rooms.id")
    scheduled_at: datetime = Field(description="计划启动时间(UTC)")
    enabled: bool = Field(default=True, description="是否启用")
    recurrent: str = Field(default="", description="重复规则:空=一次性,daily=每日,weekly=每周")
    triggered: bool = Field(default=False, description="是否已触发")
    created_at: datetime = Field(default_factory=utcnow)
