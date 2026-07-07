"""数据库实体 — Task."""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel

from app.db.entities.base import TaskStatus, utcnow


class SegmentTask(SQLModel, table=True):
    """分段处理任务(``segment_tasks``):持久化的异步任务队列 (V0.1.12.5 幂等重构)。

    每个 RawSegment 录制完成后创建一条任务,按流水线阶段独立推进:
    recorded → transcribing → analyzing → awaiting_review → approved → rendering → rendered → publishing → completed。

    V0.1.12.5 新增:
    - pipeline_key: 流程级幂等键 (UNIQUE), 创建后永不修改
    - stage_key: 阶段级幂等键, enqueue_next 时更新
    - segment_id UNIQUE 约束: 一个 segment 只能有一个流水线任务

    V0.1.11-alpha 新增:
    - failed_stage / claimed_by / claimed_at / heartbeat_at 字段
    - max_retries 默认值从 3 提升至 5
    - attempts 只在实际开始执行时增加一次
    """

    __tablename__ = "segment_tasks"

    id: int | None = Field(default=None, primary_key=True)
    segment_id: int = Field(index=True, description="关联 raw_segments.id", sa_column_kwargs={"unique": True})
    session_id: int = Field(index=True, description="关联 recording_sessions.id")
    candidate_id: int | None = Field(default=None, index=True, description="关联 highlight_candidates.id(若有)")
    event_id: int | None = Field(default=None, index=True, description="关联 highlight_events.id(若有)")
    clip_id: int | None = Field(default=None, index=True, description="关联 final_clips.id(若有)")

    stage: str = Field(default=TaskStatus.RECORDED, index=True, description="当前处理阶段")
    failed_stage: str | None = Field(default=None, description="失败时的阶段,用于精确恢复")
    priority: int = Field(default=100, description="优先级(数值越小越优先)")

    # V0.1.12.5: 双键幂等 — pipeline_key 创建后永不修改, stage_key 随阶段变化
    pipeline_key: str | None = Field(
        default=None,
        index=True,
        sa_column_kwargs={"unique": True},
        description="流程级幂等键(pipeline:{segment_id}),创建后永不修改",
    )  # noqa: E501
    stage_key: str | None = Field(
        default=None, index=True, description="阶段级幂等键(stage:{segment_id}:{stage}:{config_hash}),防阶段内重复"
    )

    # 后向兼容: 保留旧 idempotency_key 直到迁移完成
    idempotency_key: str | None = Field(
        default=None, index=True, description="[已废弃]旧幂等键,迁移到 pipeline_key + stage_key"
    )  # noqa: E501

    attempts: int = Field(default=0, description="当前阶段已尝试次数")
    max_retries: int = Field(default=5, description="当前阶段最大重试次数(默认5)")
    next_retry_at: datetime | None = Field(default=None, description="下次重试时间(指数退避,含随机抖动)")
    last_error: str | None = Field(default=None, description="最近一次错误信息")
    error_is_permanent: bool = Field(default=False, description="是否为不可恢复的永久错误")

    # V0.1.11-alpha: 并发控制与崩溃恢复
    claimed_by: str | None = Field(default=None, description="领取该任务的 Worker ID(防重复领取)")
    claimed_at: datetime | None = Field(default=None, description="任务被领取的时间")
    heartbeat_at: datetime | None = Field(default=None, description="最后心跳时间(超时判定 stale)")
    lease_token: str | None = Field(default=None, description="V0.1.12.5: 租约令牌(UUID), 条件提交时校验所有权")

    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = Field(default=None, description="当前阶段开始处理时间")
    completed_at: datetime | None = Field(default=None, description="当前阶段完成时间")
    processing_time_ms: int | None = Field(default=None, description="当前阶段处理耗时(毫秒)")
    total_elapsed_ms: int | None = Field(default=None, description="任务总耗时(创建到完成,毫秒)")

    context_json: str | None = Field(default=None, description="任务上下文 JSON(如错误堆栈、配置快照等)")
