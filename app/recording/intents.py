# ruff: noqa: D100
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.core.paths import to_storage_relative
from app.db.entities import AppSetting
from app.plugins.live_source import SourceRoom
from app.recording.writer_ownership import WriterIdentity

INTENT_PREFIX = "recording_intent:v1:"
SESSION_PREFIX = "recording_execution:v1:"
MAX_CRASH_RECOVERIES = 3


class RecordingIntent(BaseModel):
    """已接受的运行请求；跨服务重启保留人工边界和连续崩溃预算。"""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    room_id: int
    generation: str
    accepted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: SourceRoom
    origin: Literal["manual", "auto", "schedule"]
    pipeline: bool
    produce: bool
    schedule_id: int | None = None
    session_id: int | None = None
    state: Literal["accepted", "running", "cooldown", "stopped", "blocked"] = "accepted"
    reason: str = ""
    ready_at: datetime | None = None
    crashes: int = Field(default=0, ge=0)
    resume_requested: bool = False


class RecordingAttempt(BaseModel):
    """已接纳线路的独立文件范围，不保存临时 URL 或请求凭据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    prefix: str = Field(pattern=r"^part[0-9]+_[a-f0-9]{12}_$")
    directory: str
    segment_list: str


class RecordingExecution(BaseModel):
    """一个实际场次的 writer 证明、已接纳线路及收尾状态。"""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    session_id: int
    room_id: int
    generation: str | None
    writer: WriterIdentity
    attempts: list[RecordingAttempt] = Field(default_factory=list)
    finalized: bool = False
    settled: bool = False
    recovery_errors: list[str] = Field(default_factory=list)


def save(db: Session, value: RecordingIntent | RecordingExecution) -> None:
    """在调用方的业务事务中保存，不触碰配置版本。"""
    key = (
        f"{INTENT_PREFIX}{value.room_id}"
        if isinstance(value, RecordingIntent)
        else f"{SESSION_PREFIX}{value.session_id}"
    )
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value.model_dump_json())
    else:
        row.value = value.model_dump_json()
        row.updated_at = datetime.now(UTC)
    db.add(row)


def load_intent(db: Session, room_id: int) -> RecordingIntent | None:
    """读取严格版本化意图；损坏记录必须暴露错误。"""
    row = db.get(AppSetting, f"{INTENT_PREFIX}{room_id}")
    return RecordingIntent.model_validate_json(row.value) if row is not None else None


def load_execution(db: Session, session_id: int) -> RecordingExecution | None:
    """读取正式 writer 协议，不为旧记录猜测所有权。"""
    row = db.get(AppSetting, f"{SESSION_PREFIX}{session_id}")
    return RecordingExecution.model_validate_json(row.value) if row is not None else None


def all_intents(db: Session) -> list[RecordingIntent]:
    """返回所有已持久接受的请求。"""
    return [
        RecordingIntent.model_validate_json(row.value)
        for row in db.exec(select(AppSetting).where(AppSetting.key.startswith(INTENT_PREFIX))).all()
    ]


def pending_executions(db: Session) -> list[RecordingExecution]:
    """返回尚未完成清算的正式场次。"""
    records = [
        RecordingExecution.model_validate_json(row.value)
        for row in db.exec(select(AppSetting).where(AppSetting.key.startswith(SESSION_PREFIX))).all()
    ]
    return [record for record in records if not record.settled]


def accept(
    db: Session,
    room_id: int,
    source: SourceRoom,
    *,
    pipeline: bool,
    produce: bool,
    origin: Literal["manual", "auto", "schedule"],
    schedule_id: int | None = None,
    previous_generation: str | None = None,
) -> RecordingIntent:
    """持久接受新代次，恢复必须匹配原意图，显式启动才重置预算。"""
    previous = load_intent(db, room_id)
    if previous_generation is not None and (
        previous is None or previous.generation != previous_generation or previous.state != "cooldown"
    ):
        raise ValueError("录制恢复意图已被后续操作撤销")
    intent = RecordingIntent(
        room_id=room_id,
        generation=uuid4().hex,
        source=source,
        origin=origin,
        pipeline=pipeline,
        produce=produce,
        schedule_id=schedule_id,
        crashes=previous.crashes if previous_generation is not None and previous is not None else 0,
    )
    save(db, intent)
    return intent


def stop_intent(db: Session, room_id: int, reason: str) -> None:
    """先持久撤销请求，再由调用方通知实际 writer 停止。"""
    intent = load_intent(db, room_id)
    if intent is not None:
        intent.state, intent.reason = "stopped", reason
        intent.resume_requested = False
        save(db, intent)


def bind_session(db: Session, session_id: int, room_id: int, generation: str | None, writer: WriterIdentity) -> None:
    """创建场次与绑定意图、writer 证明共用同一事务。"""
    if generation is not None:
        intent = load_intent(db, room_id)
        if intent is None or intent.generation != generation or intent.state != "accepted":
            raise ValueError("录制请求已撤销，拒绝创建场次")
        intent.session_id, intent.state = session_id, "running"
        save(db, intent)
    save(db, RecordingExecution(session_id=session_id, room_id=room_id, generation=generation, writer=writer))


def adopt_attempt(db: Session, session_id: int, directory: Path, prefix: str, segment_list: Path) -> None:
    """串行启动前或竞争接纳时持久保存精确文件范围。"""
    record = load_execution(db, session_id)
    if record is None or record.finalized:
        raise ValueError("缺少有效的录制执行，拒绝接纳线路")
    directory = directory.resolve()
    if segment_list.resolve().parent != directory:
        raise ValueError("录制清单超出场次目录")
    attempt = RecordingAttempt(
        prefix=prefix, directory=to_storage_relative(directory), segment_list=to_storage_relative(segment_list)
    )
    if attempt not in record.attempts:
        record.attempts.append(attempt)
        save(db, record)


def finish(
    db: Session,
    session_id: int,
    *,
    reason: str,
    blocked: bool,
    cooldown: bool,
    shutdown: bool,
    cooldown_seconds: float,
) -> None:
    """终态和意图分类同事务完成；旧执行及人工撤销不能恢复新请求。"""
    record = load_execution(db, session_id)
    if record is None:
        return
    record.finalized = True
    save(db, record)
    intent = load_intent(db, record.room_id)
    if (
        intent is None
        or intent.generation != record.generation
        or intent.session_id != session_id
        or intent.state not in {"accepted", "running"}
    ):
        return
    intent.reason = reason
    intent.state = "blocked" if blocked else "cooldown" if shutdown or cooldown else "stopped"
    intent.resume_requested = shutdown and not blocked
    intent.ready_at = datetime.now(UTC) + timedelta(seconds=cooldown_seconds) if intent.state == "cooldown" else None
    save(db, intent)


def prepare_restarts(db: Session) -> list[RecordingIntent]:
    """每个失联执行只消耗一次预算，未获稳定媒体前重启不能清零。"""
    result = []
    for intent in all_intents(db):
        if intent.state in {"accepted", "running"}:
            intent.crashes += 1
            intent.reason = "录制执行异常中断，等待重新核实直播状态"
            intent.state = "cooldown" if intent.crashes <= MAX_CRASH_RECOVERIES else "blocked"
            intent.resume_requested = intent.state == "cooldown"
            intent.ready_at = datetime.now(UTC)
            if intent.state == "blocked":
                intent.reason = "连续异常中断的恢复预算已耗尽，请检查日志后显式启动"
            save(db, intent)
        if intent.state in {"cooldown", "blocked"}:
            result.append(intent)
    return result


def stable(db: Session, session_id: int) -> None:
    """只有当前场次真实产出稳定媒体才清除连续崩溃预算。"""
    record = load_execution(db, session_id)
    if record is None:
        return
    intent = load_intent(db, record.room_id)
    if intent is not None and intent.generation == record.generation and intent.state == "running":
        intent.crashes = 0
        save(db, intent)
