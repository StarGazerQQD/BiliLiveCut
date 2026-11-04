from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from app.analysis import llm
from app.analysis.editorial_contracts import ModelAttempt, TokenUsage
from app.analysis.model_execution import ModelCallBudget
from app.analysis.model_journal import ModelJournalError, ModelRequestJournal, ModelRequestRecord
from app.core.cancellation import OperationCancelledError
from app.db.entities import AppSetting
from app.db.session import get_session
from app.models.configuration import save_model_configuration
from tests.integration.test_structured_llm import catalog, response


def _budget(operation_id: str, *, attempts: int = 8, seconds: float = 60) -> ModelCallBudget:
    return ModelCallBudget(
        max_attempts=attempts,
        max_elapsed_s=seconds,
        operation_id=operation_id,
        journal=ModelRequestJournal(target="session-summary:1"),
    )


def _start(budget: ModelCallBudget) -> None:
    budget.start_request(
        provider_id="route",
        connection_id="connection",
        model="model",
        purpose="text",
        protocol="text",
        output_tokens_limit=128,
        request={"input": "local test"},
    )


def test_restart_preserves_all_one_time_allowances_and_protocol_memory(temp_db: None) -> None:
    identity = uuid4().hex
    budget = _budget(identity)
    assert budget.claim_repair() and budget.claim_context() and budget.claim_empty_retry()
    assert budget.claim_transport_retry("connection", "https://fixture.invalid/v1", "model")
    assert budget.protocol("route", "model") == "json_schema"
    assert budget.downgrade_protocol("route", "model")
    restored = _budget(identity, attempts=32, seconds=14400)
    assert not restored.claim_repair() and not restored.claim_context() and not restored.claim_empty_retry()
    assert not restored.claim_transport_retry("connection", "https://fixture.invalid/v1", "model")
    assert restored.protocol("route", "model") == "json_object"
    assert restored.downgrade_protocol("route", "model")
    again = _budget(identity)
    assert again.protocol("route", "model") == "text" and not again.downgrade_protocol("route", "model")
    assert 0 < again.remaining_s <= 60


def test_unknown_reserved_request_consumes_original_limit_without_fabricated_attempt(temp_db: None) -> None:
    identity = uuid4().hex
    original = _budget(identity, attempts=2)
    _start(original)
    restored = _budget(identity, attempts=32)
    assert restored.operation_id == identity and restored.request_count == 1
    assert restored.attempts == () and restored.next_number == 2
    _start(restored)
    assert restored.interruption(before_request=False) is None
    restored.record(
        ModelAttempt(
            number=2,
            purpose="text",
            provider_id="route",
            model="model",
            protocol="text",
            status="failed",
            error="timeout",
            elapsed_s=0.1,
        )
    )
    assert restored.interruption() == "attempt_budget"
    assert [attempt.number for attempt in restored.attempts] == [2]
    with get_session() as db:
        unknown = ModelRequestRecord.model_validate_json(db.get(AppSetting, f"model_request:v1:{identity}:1").value)
        assert unknown.state == "unconfirmed" and unknown.attempt is None
        assert unknown.usage.estimated_cost_usd is None
    assert _budget(identity).interruption() == "attempt_budget"


def test_revoked_claim_stops_new_reservation_but_keeps_original_receipt(temp_db: None) -> None:
    valid = True

    def guard(_db: Session) -> None:
        if not valid:
            raise OperationCancelledError("领取已失效")

    identity = uuid4().hex
    budget = ModelCallBudget(
        max_attempts=2,
        max_elapsed_s=60,
        operation_id=identity,
        journal=ModelRequestJournal(target="session-summary:1", guard=guard),
    )
    valid = False
    with pytest.raises(OperationCancelledError):
        _start(budget)
    with get_session() as db:
        assert db.get(AppSetting, f"model_request:v1:{identity}:1") is None
    valid = True
    _start(budget)
    valid = False
    usage = TokenUsage(input_tokens=10, output_tokens=20, estimated_cost_usd=0.5)
    budget.request_receipt(usage, "actual-response")
    budget.record(
        ModelAttempt(
            number=1,
            purpose="text",
            provider_id="route",
            model="model",
            protocol="text",
            status="failed",
            error="cancelled",
            elapsed_s=0.1,
            usage=usage,
        )
    )
    with get_session() as db:
        record = ModelRequestRecord.model_validate_json(db.get(AppSetting, f"model_request:v1:{identity}:1").value)
        assert record.state == "finished" and record.received_at is not None
        assert record.usage.estimated_cost_usd == 0.5


def test_expired_operation_constructs_without_renewing_absolute_deadline(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.analysis.model_execution as execution
    import app.analysis.model_journal as journal

    identity = uuid4().hex
    original = _budget(identity, seconds=10)
    future = datetime.now(UTC) + timedelta(seconds=11)

    class FutureClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return future

    monkeypatch.setattr(execution, "datetime", FutureClock)
    monkeypatch.setattr(journal, "datetime", FutureClock)
    restored = _budget(identity, seconds=14400)
    assert restored.operation_id == original.operation_id and restored.attempts == ()
    assert restored.interruption() == "deadline" and restored.elapsed_s >= 11


def test_stale_budget_cannot_reserve_a_duplicate_request(temp_db: None) -> None:
    identity = uuid4().hex
    first, stale = _budget(identity), _budget(identity)
    _start(first)
    with pytest.raises(ModelJournalError):
        _start(stale)
    recovered = _budget(identity)
    assert recovered.next_number == 2 and recovered.request_count == 1


def test_operation_reservation_write_failure_stops_before_http(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_model_configuration(catalog())
    identity = uuid4().hex
    budget = _budget(identity)
    original = Session.commit
    calls: list[httpx.Request] = []

    def fail_commit(db: Session) -> None:
        if any(isinstance(row, AppSetting) and row.key.startswith("model_request:v1:") for row in db.new):
            raise OperationalError("controlled reservation failure", {}, OSError("storage unavailable"))
        original(db)

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=response())

    monkeypatch.setattr(Session, "commit", fail_commit)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    for _ in range(2):
        with pytest.raises(ModelJournalError):
            llm.call_text("本地测试", budget=budget)
    assert calls == []
    with get_session() as db:
        assert db.get(AppSetting, f"model_request:v1:{identity}:1") is None
    assert _budget(identity).request_count == 0
