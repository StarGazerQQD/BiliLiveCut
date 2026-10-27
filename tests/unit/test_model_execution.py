from __future__ import annotations

from threading import Event
from typing import TYPE_CHECKING

import pytest

from app.analysis.editorial_contracts import ModelAttempt
from app.analysis.model_execution import ModelCallBudget, StructuredCallResult

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def attempt(number: int = 1) -> ModelAttempt:
    return ModelAttempt(
        number=number,
        purpose="review",
        provider_id="route",
        model="test",
        protocol="json_schema",
        status="failed",
        error="invalid_output",
        elapsed_s=0.0,
    )


@pytest.mark.parametrize("limit", [0, 33, True, 1.2])
def test_invalid_attempt_limits_fail_before_calls(limit: int) -> None:
    with pytest.raises(ValueError):
        ModelCallBudget(max_attempts=limit, max_elapsed_s=1)


@pytest.mark.parametrize("limit", [0, -1, float("inf"), float("nan"), True])
def test_invalid_deadlines_fail_before_calls(limit: float) -> None:
    with pytest.raises(ValueError):
        ModelCallBudget(max_attempts=1, max_elapsed_s=limit)


def test_deadline_is_shared_and_elapsed_is_actual(monkeypatch: MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("app.analysis.model_execution.time.monotonic", lambda: now[0])
    budget = ModelCallBudget(max_attempts=2, max_elapsed_s=5)
    now[0] = 103.0
    assert budget.elapsed_s == 3 and budget.remaining_s == 2
    now[0] = 105.1
    assert budget.interruption() == "deadline"
    assert not budget.claim_repair() and not budget.claim_context()


def test_cancellation_prevents_requests_and_followup_work() -> None:
    cancelled = Event()
    budget = ModelCallBudget(max_attempts=2, max_elapsed_s=5, cancelled=cancelled.is_set)
    assert budget.interruption() is None
    cancelled.set()
    assert budget.interruption() == "cancelled"
    assert not budget.claim_empty_retry()


def test_attempt_count_and_sequence_cannot_reset() -> None:
    budget = ModelCallBudget(max_attempts=1, max_elapsed_s=5)
    with pytest.raises(ValueError):
        budget.record(attempt(2))
    budget.record(attempt())
    assert budget.interruption() == "attempt_budget"
    assert budget.interruption(before_request=False) is None
    with pytest.raises(ValueError):
        budget.record(attempt(2))
    assert len(budget.attempts) == 1


def test_repairs_context_and_empty_response_have_separate_once_only_allowances() -> None:
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=5)
    for claim in (budget.claim_repair, budget.claim_context, budget.claim_empty_retry):
        assert claim() is True
        assert claim() is False


def test_operation_cannot_overlap_or_recursively_reset_budget() -> None:
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=5)
    with budget.operation():
        with pytest.raises(RuntimeError, match="串行"):
            with budget.operation():
                pytest.fail("并发进入预算")
    with budget.operation():
        assert budget.interruption() is None


def test_protocol_memory_is_scoped_to_route_and_model() -> None:
    budget = ModelCallBudget(max_attempts=8, max_elapsed_s=5)
    assert budget.protocol("a", "x") == "json_schema"
    assert budget.downgrade_protocol("a", "x")
    assert budget.protocol("a", "x") == "json_object"
    assert budget.protocol("a", "y") == "json_schema"
    assert budget.protocol("b", "x") == "json_schema"
    assert budget.downgrade_protocol("a", "x")
    assert not budget.downgrade_protocol("a", "x")


def test_result_never_contains_both_value_and_failure() -> None:
    with pytest.raises(ValueError):
        StructuredCallResult(value=None, error=None)
    with pytest.raises(ValueError):
        StructuredCallResult(value="result", error="timeout")
    assert StructuredCallResult(value={"answer": True}, error=None).value == {"answer": True}
