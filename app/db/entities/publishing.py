"""数据库实体 — Publishing."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from app.db.entities.base import UploadStatus, utcnow


class UploadTask(SQLModel, table=True):
    """上传任务(``upload_tasks``):成品进入上传队列后的执行记录。"""

    __tablename__ = "upload_tasks"

    id: int | None = Field(default=None, primary_key=True)
    clip_id: int = Field(index=True, description="所属 final_clips.id")
    uploader: str = Field(default="manual", description="使用的上传器")
    status: str = Field(default=UploadStatus.QUEUED, description="任务状态")
    attempts: int = Field(default=0, description="已尝试次数")
    last_error: str | None = Field(default=None, description="最后错误")
    remote_id: str | None = Field(default=None, description="平台返回的稿件号(若有)")
    precheck_json: str | None = Field(default=None, description="预检结果 JSON")
    scheduled_at: datetime | None = Field(default=None, description="计划上传时间")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # V0.1.12.7: 真实复合唯一约束
    __table_args__ = (UniqueConstraint("clip_id", "uploader", name="uq_upload_target"),)


class UploadAttempt(SQLModel, table=True):
    """上传尝试(``upload_attempts``):每次上传执行的详细记录, 用于防重复投稿。

    发布属于不可回滚的远程副作用, 每次尝试必须在发起请求前持久化。
    远程结果不确定时进入 RECONCILIATION_REQUIRED, 禁止自动重试。
    """

    __tablename__ = "upload_attempts"

    id: int | None = Field(default=None, primary_key=True)
    upload_task_id: int = Field(index=True, description="关联 upload_tasks.id")
    attempt_token: str = Field(unique=True, index=True, description="幂等令牌, 防止重复尝试")
    platform: str = Field(default="bilibili", description="投稿平台")
    account_id: str | None = Field(default=None, description="登录账户 ID")
    clip_id: int = Field(index=True, description="关联 final_clips.id")

    status: str = Field(
        default="prepared",
        description=(
            "PREPARED / IN_PROGRESS / SUCCESS / FAILED_RETRYABLE / "
            "FAILED_PERMANENT / REMOTE_RESULT_UNKNOWN / RECONCILIATION_REQUIRED / CANCELLED"
        ),
    )

    started_at: datetime | None = Field(default=None, description="开始执行时间")
    finished_at: datetime | None = Field(default=None, description="完成/失败时间")
    remote_id: str | None = Field(default=None, description="平台稿件 ID")
    remote_url: str | None = Field(default=None, description="平台稿件链接")
    error_type: str | None = Field(default=None, description="错误分类")
    error_message: str | None = Field(default=None, description="错误详情")
    request_fingerprint: str | None = Field(default=None, description="请求指纹, 用于幂等去重")
    created_by_worker: str | None = Field(default=None, description="发起 Worker ID")
    lease_token: str | None = Field(default=None, description="Worker 租约令牌")

    created_at: datetime = Field(default_factory=utcnow)

    # 每个 clip 的同一 attempt_token 唯一 (幂等去重)
    __table_args__ = (UniqueConstraint("clip_id", "attempt_token", name="uq_upload_attempt"),)
