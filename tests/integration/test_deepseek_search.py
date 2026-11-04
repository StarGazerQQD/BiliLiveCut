from __future__ import annotations

import json
from pathlib import Path
from threading import Event

import httpx
import pytest
from sqlmodel import select

from app.analysis import llm
from app.analysis.editorial_provenance import ReviewProvenance, capture_provenance
from app.analysis.editorial_review import review_fingerprint, review_policy_identity
from app.analysis.editorial_review_sources import compute_event_review, event_review_is_current
from app.analysis.event_enricher import build_event_evidence_bundle
from app.analysis.model_execution import ModelCallBudget
from app.analysis.model_journal import ModelRequestJournal, ModelRequestRecord
from app.core.cancellation import OperationCancelledError
from app.core.config import settings
from app.core.configuration import ConfigurationChange, save_configuration
from app.db.entities import AppSetting
from app.db.session import get_session
from app.models.configuration import save_model_configuration
from app.models.contracts import TextCapability
from app.trends import collector, store
from app.web.services.trends import trends_overview
from tests.integration.test_structured_llm import call, catalog, response
from tests.unit.test_editorial_contracts import _payload
from tests.unit.test_editorial_review import _seed


def _configure(*, enabled: bool = True, capability: TextCapability = "transcript") -> None:
    config = catalog(base_url="https://api.deepseek.com/v1")
    route = config.routes[0].model_copy(
        update={"capability": capability, "model": "deepseek-flash", "deepseek_search_enabled": enabled}
    )
    save_model_configuration(config.model_copy(update={"routes": [route]}))


def _results(*, url: str = "https://example.org/source", date: str | None = None) -> dict[str, object]:
    return {
        "results": [
            {
                "title": "公开资料",
                "url": url,
                "snippet": "公开检索摘要",
                "summary": "与查询有关的公开资料内容",
                "content": "",
                "site_name": "资料站",
                "site_icon": None,
                "date": date,
                "rerank_score": 2.4,
                "authority": 3,
            }
        ]
    }


def _budget(attempts: int = 2) -> ModelCallBudget:
    return ModelCallBudget(
        max_attempts=attempts, max_elapsed_s=30, journal=ModelRequestJournal(target="search-fixture")
    )


def test_deepseek_search_and_generation_share_requests_and_persist_sources(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()
    calls: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer private-test-key"
        if request.url.path == "/v1/tools/search":
            assert json.loads(request.content) == {"query": "需要核查的名称", "count": 10, "need_content": False}
            return httpx.Response(200, json=_results())
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert "deepseek_search_enabled" not in payload
        assert "https://example.org/source" in json.dumps(payload, ensure_ascii=False)
        assert payload["max_tokens"] <= 4096
        return httpx.Response(200, json=response("整理正文"))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = _budget()
    assert (
        llm.call_text("按原文整理", 4096, capability="transcript", budget=budget, search_query="需要核查的名称")
        == "整理正文"
    )
    assert len(calls) == budget.request_count == 2
    assert [item.purpose for item in budget.attempts] == ["search", "text"]
    assert budget.attempts[0].output_tokens_limit is None
    assert budget.attempts[0].usage.estimated_cost_usd is None
    with get_session() as db:
        rows = db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
        records = sorted(
            (ModelRequestRecord.model_validate_json(row.value) for row in rows), key=lambda row: row.number
        )
        assert len(records) == 2
        evidence = records[0].search
        assert evidence is not None and evidence.collected_at.utcoffset().total_seconds() == 0
        assert evidence.sources[0].url == "https://example.org/source"
        assert evidence.sources[0].published_at is None
        assert records[0].output_tokens_limit is None


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_deepseek_search_failure_does_not_redirect_or_generate_without_sources(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _configure()
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, headers={"location": "https://example.org/redirect"}, json={"error": "拒绝"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = _budget()
    assert llm.call_text("原文", capability="transcript", budget=budget) is None
    assert calls == ["https://api.deepseek.com/v1/tools/search"]
    assert budget.request_count == 1
    assert budget.attempts[0].status == "failed"


def test_deepseek_search_consumes_last_request_and_prevents_generation(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_results())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = _budget(1)
    assert llm.call_text("原文", capability="transcript", budget=budget) is None
    assert calls == ["/v1/tools/search"]
    assert budget.request_count == 1 and budget.last_failure == "attempt_budget"


def test_search_respects_daily_limit_after_paid_invalid_output(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    config = catalog(base_url="https://api.deepseek.com/v1", routes=2)
    config.routes[1] = config.routes[1].model_copy(update={"deepseek_search_enabled": True})
    save_model_configuration(config)
    monkeypatch.setattr(settings, "llm_daily_budget", 0.00001)
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/tools/search":
            return httpx.Response(200, json=_results())
        payload = response("未完整输出")
        payload["choices"] = [
            {"index": 0, "message": {"role": "assistant", "content": "未完整输出"}, "finish_reason": "length"}
        ]
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = _budget(3)
    assert llm.call_text("原文", budget=budget) is None
    assert budget.last_failure == "daily_budget"
    assert llm._today_spend() > settings.llm_daily_budget
    assert calls == ["/v1/chat/completions"]


def test_search_budget_callback_cancellation_is_propagated(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure()
    cancelled = Event()
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        cancelled.set()
        return httpx.Response(200, json=_results())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = ModelCallBudget(max_attempts=2, max_elapsed_s=30, cancelled=cancelled.is_set)
    with pytest.raises(OperationCancelledError):
        llm.call_text("原文", capability="transcript", budget=budget)
    assert calls == ["/v1/tools/search"]
    assert budget.attempts[0].error == "cancelled"


def test_search_structured_repair_reuses_sources_in_same_budget(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(capability="analysis")
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/tools/search":
            return httpx.Response(200, json=_results())
        assert "https://example.org/source" in request.content.decode()
        return httpx.Response(200, json=response('{"unknown":true}' if len(calls) == 2 else '{"answer":"有效正文"}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = _budget(3)
    result = call(budget)
    assert result.value is not None and result.value.answer == "有效正文"
    assert calls == ["/v1/tools/search", "/v1/chat/completions", "/v1/chat/completions"]
    assert budget.request_count == 3


def test_disabled_search_sends_only_generation(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(enabled=False)
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=response("正文"))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert llm.call_text("原文", capability="transcript", budget=_budget()) == "正文"
    assert calls == ["/v1/chat/completions"]


@pytest.mark.parametrize("payload", [{"results": []}, _results(date="2026-11-02T12:00:00"), {"results": [{}]}])
def test_unusable_search_sources_prevent_generation(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    _configure()
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert len(json.loads(request.content)["query"]) == 128
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = _budget()
    assert llm.call_text("汉字" * 150, capability="transcript", budget=budget) is None
    assert calls == ["/v1/tools/search"]
    assert not budget.search_results


def test_search_failure_remains_charged_after_operation_recovery(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(500, json={"error": "temporary"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    for _ in range(2):
        budget = ModelCallBudget(
            max_attempts=1,
            max_elapsed_s=30,
            operation_id="a" * 32,
            journal=ModelRequestJournal(target="search-recovery"),
        )
        assert llm.call_text("原文", capability="transcript", budget=budget) is None
        assert budget.request_count == 1
    assert calls == ["/v1/tools/search"]
    assert budget.last_failure == "attempt_budget"


def test_search_authorization_revocation_prevents_generation(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure()
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        config = catalog(revision=1, base_url="https://api.deepseek.com/v1")
        config.connections[0] = config.connections[0].model_copy(update={"enabled": False})
        save_model_configuration(config)
        return httpx.Response(200, json=_results())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budget = _budget()
    assert llm.call_text("原文", capability="transcript", budget=budget) is None
    assert calls == ["/v1/tools/search"]
    assert budget.attempts[0].error == "unauthorized"


def test_trends_search_sources_survive_collection_update_and_read(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(capability="trends")
    save_configuration(ConfigurationChange(values={"trend_enabled": True, "trend_web_search": True}))
    store.save_trends([collector.TrendRecord(source="web", title="历史资料")])
    calls: list[str] = []
    search_number = 0

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal search_number
        calls.append(request.url.path)
        if request.url.path == "/v1/tools/search":
            search_number += 1
            return httpx.Response(
                200,
                json=_results(
                    url=f"https://example.org/source/{search_number}",
                    date=None if search_number == 1 else "2026-11-02T12:00:00Z",
                ),
            )
        output = [
            {
                "source": "web",
                "title": "已检索话题",
                "tags": ["公开话题"],
                "heat": 80,
                "url": f"https://example.org/source/{search_number}",
            },
            {"source": "web", "title": "虚构来源", "url": "https://example.org/invented"},
        ]
        return httpx.Response(200, json=response(json.dumps(output, ensure_ascii=False)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    for number in (1, 2):
        budget = _budget()
        assert collector.collect_and_save("公开话题", budget=budget) == 1
        assert budget.request_count == 2
        items = trends_overview()["items"]
        assert len(items) == 2
        current = next(item for item in items if item["title"] == "已检索话题")
        historical = next(item for item in items if item["title"] == "历史资料")
        assert historical["search"] is None
        assert current["seen_count"] == number
        assert current["url"] == f"https://example.org/source/{number}"
        source = current["search"]["sources"][0]
        assert source["url"] == current["url"] and source["title"] == "公开资料"
        assert source["published_at"] == (None if number == 1 else "2026-11-02T12:00:00Z")
        assert current["search"]["collected_at"]
        assert store.match_text("公开话题")[0] > 0
    assert calls == ["/v1/tools/search", "/v1/chat/completions"] * 2


def test_trend_search_switch_suppresses_search_on_enabled_route(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(capability="trends")
    save_configuration(ConfigurationChange(values={"trend_enabled": True, "trend_web_search": False}))
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=response('[{"title":"未联网生成","source":"web"}]'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert collector.collect_and_save("话题", budget=_budget()) == 1
    overview = trends_overview()
    assert overview["web_search"] is False
    assert overview["items"][0]["search"] is None
    assert calls == ["/v1/chat/completions"]


def test_search_toggle_changes_analysis_identity_and_rejects_previous_draft(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(enabled=False, capability="analysis")
    event_id, _ = _seed(pcm_wav)
    calls: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        ids = [item["evidence_id"] for item in data["evidence"] if item["excerpts"]]
        return httpx.Response(200, json=response(json.dumps(_payload(evidence_ids=ids[:1], suggested_bounds=None))))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    draft = compute_event_review(event_id, _budget())
    assert draft.execution.status == "succeeded"
    policy = review_policy_identity()
    assert "deepseek_search_enabled" not in policy["routes"][0]
    before = review_fingerprint(draft.source, draft.source, draft.revision, policy=policy)
    legacy = capture_provenance(policy, draft.source, operation_id="b" * 32, prior_attempts=[])
    assert ReviewProvenance.model_validate_json(legacy.model_dump_json()).policy == policy
    with get_session() as db:
        original = build_event_evidence_bundle(db, event_id)
        assert event_review_is_current(db, event_id, draft)
    config = catalog(revision=1, base_url="https://api.deepseek.com/v1")
    config.routes[0] = config.routes[0].model_copy(update={"model": "deepseek-flash", "deepseek_search_enabled": True})
    save_model_configuration(config)
    updated = review_policy_identity()
    assert updated["routes"][0]["deepseek_search_enabled"] is True
    assert review_fingerprint(draft.source, draft.source, draft.revision, policy=updated) != before
    saved = capture_provenance(updated, draft.source, operation_id="c" * 32, prior_attempts=[])
    assert ReviewProvenance.model_validate_json(saved.model_dump_json()).policy == updated
    with get_session() as db:
        assert build_event_evidence_bundle(db, event_id).fingerprint != original.fingerprint
        assert not event_review_is_current(db, event_id, draft)
    assert calls == ["/v1/chat/completions"]
