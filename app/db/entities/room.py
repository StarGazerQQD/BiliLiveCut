"""数据库实体 — Room."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.entities.base import RoomMode, SessionStatus, utcnow


class LiveRoom(SQLModel, table=True):
    """直播间(``live_rooms``):被监控的直播源及其策略配置。"""

    __tablename__ = "live_rooms"

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(default="bilibili", description="平台标识")
    input_url: str = Field(description="用户输入的原始 URL 或短号")
    room_id: int | None = Field(default=None, index=True, description="归一化后的真实房间号")
    uploader_name: str | None = Field(default=None, description="主播名")
    title: str | None = Field(default=None, description="直播间标题")
    mode: str = Field(
        default=RoomMode.MANUAL, description="[已废弃 V0.1.6]审核模式:manual/semi/auto;请改用 auto_* 开关"
    )  # noqa: E501
    highlight_threshold: float = Field(default=0.65, description="进入候选池的综合评分阈值")
    auto_publish_threshold: float = Field(default=0.85, description="自动发布阈值")
    enabled: bool = Field(default=False, description="是否启用监控/录制")
    authorized: bool = Field(default=False, description="是否已确认拥有录制授权(合规闸)")

    # V0.1.6: 独立自动化开关(替代旧 mode)。
    auto_record: bool = Field(default=False, description="是否允许自动开始录制")
    auto_analyze: bool = Field(default=False, description="是否自动执行转写+高光分析")
    auto_render: bool = Field(default=False, description="是否自动生成切片成品")
    auto_approve: bool = Field(default=False, description="是否自动批准高分候选(免人工审核)")
    auto_upload: bool = Field(default=False, description="是否自动提交上传任务")

    # V0.1.6: 审核阈值。
    auto_approve_threshold: float = Field(default=0.82, description="≥此分自动批准")
    review_threshold: float = Field(default=0.50, description="≥此分进入人工审核;低于此分自动淘汰")

    # V0.1.2 新增:房间级功能开关(录制启动后锁定,不可更改启用状态)
    schedule_enabled: bool = Field(default=False, description="是否启用录制预约")
    auto_threshold_enabled: bool = Field(default=False, description="是否启用阈值自学习")
    danmaku_sentiment_enabled: bool = Field(default=False, description="是否启用弹幕情绪分析")

    # V0.1.14.8: ML 高光模型开关(Highlight_Model 分支,开发中)
    ml_highlight_enabled: bool = Field(default=False, description="是否使用 ML 高光模型替代规则+LLM(开发中)")

    # V0.1.6 P2:房间级配置(热词/别名/高光关键词/屏蔽主题,存储为 JSON)。
    room_config_json: str | None = Field(
        default=None, description="房间配置 JSON(hotwords/aliases/highlight_keywords/blocked_topics)"
    )  # noqa: E501
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RecordingSession(SQLModel, table=True):
    """录制会话(``recording_sessions``):一次连续录制的生命周期。"""

    __tablename__ = "recording_sessions"

    id: int | None = Field(default=None, primary_key=True)
    room_id: int = Field(index=True, description="所属 live_rooms.id")
    stream_url: str | None = Field(default=None, description="本次拉流地址(短期,可空)")
    stream_format: str | None = Field(default=None, description="hls / flv")
    quality: int | None = Field(default=None, description="清晰度码 qn")
    status: str = Field(default=SessionStatus.STARTING, description="会话状态")
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = Field(default=None)
    reconnect_count: int = Field(default=0, description="断流重连次数")
    last_reconnected_at: datetime | None = Field(default=None, description="最近一次重连成功时刻(UTC)")
    error_message: str | None = Field(default=None, description="最后一次错误信息")
