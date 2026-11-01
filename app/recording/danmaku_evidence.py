# ruff: noqa: D100
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.db.entities import AppSetting
from app.db.session import get_session
from app.plugins.live_source import DanmakuStatus
from app.recording.metadata import read_metadata


class CaptureInterval(BaseModel):
    """已确认连通的 UTC 区间，断连和正常停止均关闭区间。"""

    model_config = ConfigDict(extra="forbid")
    start: AwareDatetime
    end: AwareDatetime | None = None


class DanmakuEvidence(BaseModel):
    """状态与历史覆盖分开，避免断连抹掉历史或缺失被当作零事件。"""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    status: DanmakuStatus
    lag_s: float = 0.0
    intervals: list[CaptureInterval] = Field(default_factory=list)
    ended_at: AwareDatetime | None = None
    confirmed_until: AwareDatetime | None = None
    interrupted: bool = False
    archive_error: str | None = None


def read_evidence(db: Session, session_id: int) -> DanmakuEvidence | None:
    """读取场次证据；缺失记录不推断为成功采集。"""
    return read_metadata(db, f"session_danmaku:{session_id}", DanmakuEvidence)


def save_evidence(db: Session, session_id: int, evidence: DanmakuEvidence) -> None:
    """与消息及消费位置共用业务事务保存覆盖事实。"""
    key = f"session_danmaku:{session_id}"
    row = db.get(AppSetting, key) or AppSetting(key=key)
    row.value = evidence.model_dump_json()
    row.updated_at = datetime.now(UTC)
    db.add(row)


def freeze_evidence(db: Session, session_id: int) -> None:
    """没有正常终态时仅冻结最后一次持久观测，不用消息推断完整覆盖。"""
    evidence = read_evidence(db, session_id)
    if evidence is None or evidence.ended_at is not None:
        return
    for interval in evidence.intervals:
        if interval.end is None:
            interval.end = max(interval.start, evidence.confirmed_until or interval.start)
    evidence.ended_at = evidence.confirmed_until or datetime.now(UTC)
    evidence.interrupted = True
    if evidence.status in {DanmakuStatus.AVAILABLE, DanmakuStatus.CONNECTING}:
        evidence.status = DanmakuStatus.FAILED
    save_evidence(db, session_id, evidence)


def freeze_interrupted_captures() -> None:
    """旧 writer 清算及 journal 重放后，冻结没有正式执行记录的遗留连接。"""
    with get_session() as db:
        rows = db.exec(select(AppSetting.key).where(AppSetting.key.startswith("session_danmaku:"))).all()
        for key in rows:
            freeze_evidence(db, int(key.split(":", 1)[1]))
