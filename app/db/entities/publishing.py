"""数据库实体 — Publishing (V0.1.14.3)."""

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
