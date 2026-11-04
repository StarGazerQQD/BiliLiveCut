from __future__ import annotations

import json

import httpx
import pytest
from sqlmodel import select

from app.analysis import session_summary
from app.db.entities import AppSetting
from app.db.session import get_session
from app.models.configuration import model_configuration_view, save_model_configuration
from app.models.contracts import ModelConfigurationChange
from tests.integration.test_session_summary_recovery import _grounding_reply, _seed
from tests.integration.test_structured_llm import catalog, response


def _configure(mode: str) -> None:
    config = catalog(revision=model_configuration_view().revision)
    routes = [route.model_copy(update={"capability": "summary"}) for route in config.routes]
    if mode == "empty":
        config = ModelConfigurationChange(revision=config.revision, connections=[], routes=[])
    else:
        if mode == "other":
            routes = config.routes
        elif mode == "route_disabled":
            routes = [route.model_copy(update={"enabled": False}) for route in routes]
        elif mode == "connection_disabled":
            config = config.model_copy(
                update={"connections": [item.model_copy(update={"enabled": False}) for item in config.connections]}
            )
        config = config.model_copy(update={"routes": routes})
    save_model_configuration(config)


def _request(session_id: int) -> dict[str, object]:
    with get_session() as db:
        row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
        assert row is not None
        return json.loads(row.value)


def _view(session_id: int) -> dict[str, object]:
    return session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)


@pytest.mark.parametrize("mode", ["empty", "other", "route_disabled", "connection_disabled"])
def test_unconfigured_summary_waits_without_spending_attempts_and_resumes_same_operation(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    session_id = _seed(count=1, length=40)
    _configure(mode)
    calls: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if checked := _grounding_reply(request):
            return checked
        return httpx.Response(200, json=response('{"summary":"已有事实的完整总结"}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert session_summary.request_session_timeline_summary(session_id, reason="recording_finished")
    original = _request(session_id)
    for _ in range(4):
        assert session_summary.claim_pending_session_summary() is None
        assert _view(session_id)["status"] == "unconfigured"
        assert _request(session_id) == original
    assert calls == []
    assert original["attempts"] == 0
    with get_session() as db:
        assert not db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
    _configure("enabled")
    claim = session_summary.claim_pending_session_summary()
    assert claim is not None
    assert session_summary.execute_session_summary_claim(claim)
    assert len(calls) == 2
    assert _view(session_id)["status"] == "ready"
    completed = _request(session_id)
    assert completed["operation_id"] == original["operation_id"]
    assert completed["attempts"] == 0


def test_summary_route_removed_after_claim_does_not_consume_failure_attempt(temp_db: None) -> None:
    session_id = _seed(count=1, length=40)
    assert session_summary.request_session_timeline_summary(session_id, reason="recording_finished")
    original = _request(session_id)
    claim = session_summary.claim_pending_session_summary()
    assert claim is not None
    _configure("empty")
    with pytest.raises(session_summary.SessionSummaryUnconfigured, match="未配置"):
        session_summary.build_session_timeline_summary(session_id)
    assert not session_summary.execute_session_summary_claim(claim)
    current = _request(session_id)
    assert current["status"] == "pending"
    assert current["attempts"] == 0
    assert current["operation_id"] == original["operation_id"]
    assert _view(session_id)["status"] == "unconfigured"
    with get_session() as db:
        assert not db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()


def test_unconfigured_view_preserves_actual_failed_summary_history(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=1, length=40)
    assert session_summary.request_session_timeline_summary(session_id, reason="recording_finished")

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response('{"summary":""}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    for _ in range(3):
        with get_session() as db:
            row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
            assert row is not None
            payload = json.loads(row.value)
            payload["next_retry_at"] = None
            row.value = json.dumps(payload)
        claim = session_summary.claim_pending_session_summary()
        assert claim is not None
        assert not session_summary.execute_session_summary_claim(claim)
    failed = _request(session_id)
    assert failed["status"] == "failed" and failed["attempts"] == 3
    for mode in ("empty", "enabled"):
        _configure(mode)
        assert _view(session_id)["status"] == "failed"
        assert not session_summary.ensure_session_timeline_summary_requested(session_id)
        assert session_summary.recover_running_session_summary_requests() == 0
        assert session_summary.claim_pending_session_summary() is None
        assert _request(session_id) == failed


def test_empty_summary_can_finish_without_model_configuration(temp_db: None) -> None:
    session_id = _seed(count=0)
    _configure("empty")
    assert session_summary.request_session_timeline_summary(session_id, reason="recording_finished")
    claim = session_summary.claim_pending_session_summary()
    assert claim is not None
    assert session_summary.execute_session_summary_claim(claim)
    view = _view(session_id)
    assert view["status"] == "ready" and view["source"] == "empty"
