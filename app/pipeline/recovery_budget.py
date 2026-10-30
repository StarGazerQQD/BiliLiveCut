# ruff: noqa: D100
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from sqlmodel import select

from app.core.sanitize import sanitize_diagnostic
from app.db.entities import AppSetting, SegmentTask, TaskStatus

_STAGES = (TaskStatus.TRANSCRIBING, TaskStatus.ANALYZING, TaskStatus.RENDERING, TaskStatus.PUBLISHING)


class RecoveryBudget(BaseModel):
    """保存跨进程重启的连续失联次数，不与普通业务重试混用。"""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    consumed: int = Field(default=0, ge=0)
    last_execution: str = ""
    last_reason: str = ""


def consume_recovery(db: Session, task: SegmentTask, reason: str) -> bool:
    """在已锁定任务的事务中计入一次崩溃，返回是否耗尽恢复预算。"""
    if task.stage not in _STAGES:
        raise ValueError(f"不能在非活动阶段消耗恢复预算: {task.stage}")
    key = f"task_recovery:v1:{task.id}:{task.stage}"
    row = db.get(AppSetting, key)
    budget = RecoveryBudget.model_validate_json(row.value) if row is not None else RecoveryBudget()
    execution = f"{task.claimed_by}:{task.lease_token}:{task.claimed_at or task.started_at or task.created_at}"
    if budget.last_execution != execution:
        budget.consumed += 1
        budget.last_execution = execution
        budget.last_reason = sanitize_diagnostic(reason, limit=1000)
        if row is None:
            row = AppSetting(key=key)
        row.value = budget.model_dump_json()
        row.updated_at = datetime.now(UTC)
        db.add(row)
    return budget.consumed >= task.max_retries


def reset_recovery(db: Session, task: SegmentTask, *, all_stages: bool = False) -> None:
    """成功提交或显式重试时，在业务事务内清除对应连续崩溃预算。"""
    stages = _STAGES if all_stages else (task.stage,)
    for stage in stages:
        row = db.get(AppSetting, f"task_recovery:v1:{task.id}:{stage}")
        if row is not None:
            db.delete(row)


def has_saved_asr(db: Session, task: SegmentTask) -> bool:
    """已落盘原始结果必须保留供显式重试，不能因后处理崩溃宣称 ASR 不可用。"""
    return (
        db.execute(
            select(AppSetting.key).where(
                AppSetting.key.startswith(f"asr_checkpoint:v1:{task.id}:"),
                AppSetting.key.endswith(":raw"),
            )
        ).first()
        is not None
    )
