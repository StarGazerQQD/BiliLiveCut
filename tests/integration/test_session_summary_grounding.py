from __future__ import annotations

import json

import httpx
import pytest
from sqlmodel import select

from app.analysis import session_summary
from app.core.config import settings
from app.db.entities import AppSetting, Transcript
from app.db.session import get_session
from tests.integration.test_session_summary_recovery import _run, _seed
from tests.integration.test_structured_llm import response


@pytest.mark.parametrize("supported", [False, True])
def test_summary_is_verified_before_cache_and_ready_without_human_labels(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, supported: bool
) -> None:
    session_id = _seed(count=1, length=20)
    monkeypatch.setattr(settings, "summary_model_max_attempts", 2)
    original = "演员没起床，铃没有响，是哪里出错？"
    candidate = "铃未响导致演员迟起。" if not supported else "发言者说演员未起床并询问铃未响的原因。"
    with get_session() as db:
        db.exec(select(Transcript)).one().final_text = original
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        calls.append(wire)
        if len(calls) == 1:
            result = {"summary": candidate}
        else:
            material = json.loads(wire["messages"][1]["content"])
            assert material["candidate"] == candidate
            assert material["material"][0]["text"] == original
            assert material["material"][0]["time_basis"] == "recording"
            assert material["coverage"] is not None
            result = {
                "status": "supported" if supported else "unsupported",
                "reason": "因果未明" if not supported else "保留不确定性",
                "evidence_ids": [material["material"][0]["evidence_id"]] if supported else [],
            }
        return httpx.Response(200, json=response(json.dumps(result)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id) is supported
    assert len(calls) == 2
    with get_session() as db:
        cached = db.exec(select(AppSetting).where(AppSetting.key.startswith("summary_cache:v1:"))).all()
        assert bool(cached) is supported
        records = [
            json.loads(row.value)
            for row in db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
        ]
        assert len(records) == 2 and len({item["operation_id"] for item in records}) == 1
        assert {item["purpose"] for item in records} == {"text", "grounding"}
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert (view["status"] == "ready") is supported


def test_unverified_summary_cannot_be_cached_when_shared_budget_ends(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=1, length=20)
    monkeypatch.setattr(settings, "summary_model_max_attempts", 1)
    calls: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=response('{"summary":"待核验正文"}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert not _run(session_id)
    assert len(calls) == 0
    with get_session() as db:
        assert not db.exec(select(AppSetting).where(AppSetting.key.startswith("summary_cache:v1:"))).all()
        assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None


@pytest.mark.parametrize("fault", ["unknown_index", "invented_quote", "instruction_quote", "empty_quotes", "oversized"])
def test_summary_rejects_invalid_grounding_and_unverifiable_large_candidate(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    session_id = _seed(count=1, length=20)
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        calls.append(wire)
        if len(calls) == 1:
            result = {"summary": "内容" * 2000 if fault == "oversized" else "候选概括"}
        else:
            assert json.loads(wire["messages"][1]["content"])["material"]
            text = "S_unknown" if fault == "unknown_index" else "虚构正文"
            if fault == "instruction_quote":
                text = "不推断未明说的因果"
            result = {
                "status": "supported",
                "reason": "受控非法响应",
                "evidence_ids": [] if fault == "empty_quotes" else [text],
            }
        return httpx.Response(200, json=response(json.dumps(result)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert not _run(session_id)
    assert len(calls) == 3
    with get_session() as db:
        assert not db.exec(select(AppSetting).where(AppSetting.key.startswith("summary_cache:v1:"))).all()
        assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None


@pytest.mark.parametrize("change", ["source", "shutdown"])
def test_grounding_cannot_commit_after_input_change_or_cancellation(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from app.core.cancellation import shutdown_event
    from tests.integration.test_session_summary_recovery import _grounding_reply

    session_id = _seed(count=1, length=20)
    calls: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if checked := _grounding_reply(request):
            if change == "source":
                with get_session() as db:
                    db.exec(select(Transcript)).one().final_text = "人工更正后的原文"
            else:
                shutdown_event.set()
            return checked
        return httpx.Response(200, json=response('{"summary":"候选概括"}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    try:
        assert not _run(session_id)
    finally:
        shutdown_event.clear()
    assert len(calls) == 2
    with get_session() as db:
        assert not db.exec(select(AppSetting).where(AppSetting.key.startswith("summary_cache:v1:"))).all()
        assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None
