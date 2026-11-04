# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.analysis.editorial_contracts import ModelAttempt, ModelProtocol, ModelPurpose, TokenUsage
from app.analysis.search_contracts import SearchEvidence
from app.db.entities import AppSetting, SystemLog
from app.db.session import get_session

_PREFIX = "model_request:v1:"
_OPERATION_PREFIX = "model_operation:v1:"


class ModelJournalError(RuntimeError):
    """请求事实无法可靠保存时停止发送，避免无记录的收费请求。"""


class ModelOperationState(BaseModel):
    """跨领取与重启保留授权额度，不把未知请求伪造成已结束响应。"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal[1] = 1
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    target: str = Field(min_length=1, max_length=256)
    max_attempts: int = Field(ge=1, le=32)
    started_at: datetime
    deadline: datetime
    request_count: int = Field(default=0, ge=0, le=32)
    repair_used: bool = False
    context_used: bool = False
    empty_used: bool = False
    protocols: list[tuple[str, str, ModelProtocol]] = Field(default_factory=list)
    transport_retries: list[tuple[str, str, str]] = Field(default_factory=list)


class ModelRequestRecord(BaseModel):
    """发送前和响应后的事实记录；未知费用不能按零费用处理。"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal[1] = 1
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    number: int = Field(ge=1, le=32)
    local_request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    target: str | None = Field(default=None, max_length=256)
    provider_id: str = Field(min_length=1, max_length=256)
    connection_id: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    purpose: ModelPurpose
    protocol: ModelProtocol
    output_tokens_limit: int | None = Field(ge=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    state: Literal["unconfirmed", "response_received", "finished"] = "unconfirmed"
    received_at: datetime | None = None
    finished_at: datetime | None = None
    request_id: str | None = Field(default=None, max_length=128)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    attempt: ModelAttempt | None = None
    search: SearchEvidence | None = None

    @model_validator(mode="after")
    def validate_output_limit(self) -> ModelRequestRecord:
        """搜索无 token 输出；生成请求继续要求明确正数上限。"""
        if self.protocol == "search":
            if self.purpose != "search" or self.output_tokens_limit is not None:
                raise ValueError("搜索请求不得声明 token 输出额度")
        elif self.purpose == "search" or self.output_tokens_limit is None:
            raise ValueError("生成请求必须声明 token 输出额度")
        return self


class ModelRequestJournal:
    """只保存请求事实，不回放模型请求或重复结算原有费用账本。"""

    def __init__(self, *, target: str | None = None, guard: Callable[[Session], None] | None = None) -> None:
        self.target = target
        self._failed = False
        self._guard = guard
        self._state: ModelOperationState | None = None

    def open_operation(self, operation_id: str, max_attempts: int, max_elapsed_s: float) -> ModelOperationState:
        """首次可执行时固定期限；恢复仅核对该操作的有界请求记录。"""
        if self.target is None:
            raise ModelJournalError("持久模型操作必须指定业务目标")
        with self._write() as db:
            if self._guard is not None:
                self._guard(db)
            key = f"{_OPERATION_PREFIX}{operation_id}"
            row = db.get(AppSetting, key)
            if row is None:
                existing = db.exec(
                    select(AppSetting.key).where(AppSetting.key.startswith(f"{_PREFIX}{operation_id}:")).limit(1)
                ).first()
                if existing is not None:
                    raise ModelJournalError("模型操作预算缺失，禁止重新授予已发送操作的额度")
                now = datetime.now(UTC)
                state = ModelOperationState(
                    operation_id=operation_id,
                    target=self.target,
                    max_attempts=max_attempts,
                    started_at=now,
                    deadline=now + timedelta(seconds=max_elapsed_s),
                )
                db.add(AppSetting(key=key, value=state.model_dump_json()))
            else:
                state = ModelOperationState.model_validate_json(row.value)
                if state.operation_id != operation_id or state.target != self.target:
                    raise ModelJournalError("模型操作预算身份不匹配")
                if state.request_count > state.max_attempts:
                    raise ModelJournalError("模型操作记录超过授权额度")
                for number in range(1, state.request_count + 1):
                    record_row = db.get(AppSetting, f"{_PREFIX}{operation_id}:{number}")
                    if record_row is None:
                        raise ModelJournalError("模型操作缺少已保留额度的请求记录")
                    record = ModelRequestRecord.model_validate_json(record_row.value)
                    if (record.operation_id, record.number, record.target) != (operation_id, number, self.target):
                        raise ModelJournalError("模型操作请求记录身份不匹配")
        self._state = state
        return state

    def _save_operation(self, db: Session, state: ModelOperationState) -> None:
        if self._guard is not None:
            self._guard(db)
        row = db.get(AppSetting, f"{_OPERATION_PREFIX}{state.operation_id}")
        if row is None or self._state is None or ModelOperationState.model_validate_json(row.value) != self._state:
            raise ModelJournalError("模型操作预算已变更，禁止旧执行继续发送")
        row.value = state.model_dump_json()
        db.add(row)

    def save_allowances(
        self,
        *,
        repair: bool,
        context: bool,
        empty: bool,
        protocols: dict[tuple[str, str], ModelProtocol],
        transport_retries: set[tuple[str, str, str]],
    ) -> None:
        """原子保存一次性许可；回执和结束记录不经过当前领取身份检查。"""
        if self._state is None:
            return
        state = self._state.model_copy(
            update={
                "repair_used": repair,
                "context_used": context,
                "empty_used": empty,
                "protocols": [(provider, model, protocol) for (provider, model), protocol in sorted(protocols.items())],
                "transport_retries": sorted(transport_retries),
            }
        )
        with self._write() as db:
            self._save_operation(db, state)
        self._state = state

    @contextmanager
    def _write(self) -> Iterator[Session]:
        if self._failed:
            raise ModelJournalError("模型请求记录此前写入失败，已停止后续请求")
        try:
            with get_session(operation="model_request_journal") as db:
                if db.get_bind().dialect.name == "sqlite":
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield db
        except (SQLAlchemyError, OSError, ValidationError, ModelJournalError) as exc:
            self._failed = True
            raise ModelJournalError("模型请求记录无法保存，已停止后续请求；已发送请求的费用需核对") from exc

    def start(
        self,
        *,
        operation_id: str,
        number: int,
        provider_id: str,
        connection_id: str,
        model: str,
        purpose: ModelPurpose,
        protocol: ModelProtocol,
        output_tokens_limit: int | None,
        request: object,
    ) -> None:
        """发送前持久登记；该记录不证明请求已到达服务，也不提供服务端幂等保证。"""
        record = ModelRequestRecord(
            operation_id=operation_id,
            number=number,
            local_request_id=uuid4().hex,
            target=self.target,
            provider_id=provider_id,
            connection_id=connection_id,
            model=model,
            purpose=purpose,
            protocol=protocol,
            output_tokens_limit=output_tokens_limit,
            input_sha256=hashlib.sha256(
                json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest(),
            started_at=datetime.now(UTC),
        )
        key = f"{_PREFIX}{operation_id}:{number}"
        state = self._state
        with self._write() as db:
            if state is not None:
                if (
                    number != state.request_count + 1
                    or number > state.max_attempts
                    or datetime.now(UTC) >= state.deadline
                ):
                    raise ModelJournalError("模型操作次数或绝对期限已耗尽")
                state = state.model_copy(update={"request_count": number})
                self._save_operation(db, state)
            if db.get(AppSetting, key) is not None:
                raise ModelJournalError("模型请求身份已登记，禁止自动重复发送")
            db.add(AppSetting(key=key, value=record.model_dump_json()))
            self._log(db, record, "model_request_started", "模型请求已登记；收到用量前费用未知，中断后需核对服务端记录")
        self._state = state

    def receipt(self, operation_id: str, number: int, usage: TokenUsage, request_id: str | None) -> None:
        """费用结算和业务验证之前保存实际回执，不包含响应正文。"""
        with self._write() as db:
            row, record = self._read(db, operation_id, number)
            updated = record.model_copy(
                update={
                    "state": "response_received",
                    "received_at": datetime.now(UTC),
                    "usage": usage,
                    "request_id": request_id,
                }
            )
            row.value = updated.model_dump_json()
            db.add(row)

    def save_search(self, operation_id: str, number: int, evidence: SearchEvidence) -> None:
        """在后续生成前持久保存实际搜索来源，失败时停止业务调用。"""
        with self._write() as db:
            row, record = self._read(db, operation_id, number)
            if record.protocol != "search" or record.state != "response_received":
                raise ModelJournalError("搜索来源缺少对应服务回执")
            row.value = record.model_copy(update={"search": evidence}).model_dump_json()
            db.add(row)

    def finish(self, operation_id: str, attempt: ModelAttempt) -> None:
        """保存本次结束状态；失败或无用量响应仍可能产生费用。"""
        with self._write() as db:
            row, record = self._read(db, operation_id, attempt.number)
            updated = record.model_copy(
                update={
                    "state": "finished",
                    "finished_at": datetime.now(UTC),
                    "usage": attempt.usage,
                    "request_id": attempt.request_id,
                    "attempt": attempt,
                }
            )
            row.value = updated.model_dump_json()
            db.add(row)
            message = (
                "模型请求已结束，费用为服务返回用量的配置价格估算"
                if attempt.usage.estimated_cost_usd is not None
                else "模型请求已结束，费用未知；失败状态不代表未计费，请核对服务端记录"
            )
            self._log(db, updated, "model_request_finished", message)

    @staticmethod
    def _read(db: Session, operation_id: str, number: int) -> tuple[AppSetting, ModelRequestRecord]:
        row = db.get(AppSetting, f"{_PREFIX}{operation_id}:{number}")
        if row is None:
            raise ModelJournalError("模型请求缺少发送前记录")
        record = ModelRequestRecord.model_validate_json(row.value)
        if record.operation_id != operation_id or record.number != number or record.state == "finished":
            raise ModelJournalError("模型请求记录身份或状态不匹配")
        return row, record

    @staticmethod
    def _log(db: Session, record: ModelRequestRecord, event: str, message: str) -> None:
        db.add(
            SystemLog(
                level="INFO",
                module="llm",
                event=event,
                message=message,
                context_json=json.dumps(
                    {
                        "operation_id": record.operation_id,
                        "attempt": record.number,
                        "target": record.target,
                        "local_request_id": record.local_request_id,
                        "request_id": record.request_id,
                        "provider_id": record.provider_id,
                        "model": record.model,
                        "state": record.state,
                        "estimated_cost_usd": record.usage.estimated_cost_usd,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                ),
            )
        )
