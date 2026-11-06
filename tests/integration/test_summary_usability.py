from __future__ import annotations

import json
import re
from uuid import uuid4

import httpx
import pytest
from sqlmodel import select

from app.analysis import llm, session_summary
from app.analysis.llm_providers import LLMProvider
from app.analysis.model_execution import ModelCallBudget
from app.analysis.model_journal import ModelRequestJournal
from app.analysis.summary_verification import VERIFICATION_PROMPT, verification_fits
from app.core.config import settings
from app.core.configuration import configuration_view
from app.db.entities import AppSetting
from app.db.session import get_session
from app.models.capacity import output_budget
from app.models.configuration import configured_models, save_model_configuration
from app.models.contracts import ConnectionChange, ModelConfigurationChange
from tests.integration.test_session_summary_recovery import _continue, _payload, _run, _seed
from tests.integration.test_structured_llm import call, response


def _support(payload: dict[str, object]) -> dict[str, object]:
    material = payload["material"]
    assert isinstance(material, list)
    return {"status": "supported", "reason": "原文支持", "evidence_ids": [material[0]["evidence_id"]]}


def _wide_route() -> None:
    config = configured_models()
    assert config is not None
    save_model_configuration(
        ModelConfigurationChange(
            revision=configuration_view()["revision"],
            connections=[ConnectionChange(**connection.model_dump()) for connection in config.connections],
            routes=[
                route.model_copy(update={"context_window_tokens": 1_000_000, "max_output_tokens": 131_072})
                for route in config.routes
            ],
        )
    )


@pytest.mark.parametrize("count", [1, 6])
def test_failed_verification_preserves_full_draft_without_repeating_generation(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    session_id = _seed(count=count, length=40 if count == 1 else 1500)
    generated = 0
    recover = False

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal generated
        wire = json.loads(request.content)
        if len(wire["messages"]) == 1:
            generated += 1
            result = json.dumps({"summary": "完整总结正文"})
        elif recover:
            result = json.dumps(_support(json.loads(wire["messages"][1]["content"])))
        else:
            result = "invalid-json"
        return httpx.Response(200, json=response(result))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert not _run(session_id)
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["summary"] is None
    assert view["draft_summary"] == "完整总结正文"
    assert view["verification"]["status"] == "failed"
    with get_session() as db:
        row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
        pending = json.loads(row.value)
        pending["next_retry_at"] = None
        row.value = json.dumps(pending)
    recover = True
    assert _continue(session_id)
    assert generated == 1
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["summary"] == "完整总结正文" and view["verification"]["status"] == "verified"


def test_unsupported_summary_repairs_only_failed_part_before_publishing(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=1, length=40)
    generated = 0
    revised = 0

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal generated, revised
        wire = json.loads(request.content)
        if len(wire["messages"]) == 1:
            payload = _payload(request)
            if payload["stage"] == "revision":
                revised += 1
                assert payload["content"]["candidate"] == "没有依据的因果。"
                result = {"summary": "因果关系无法确定。"}
            else:
                generated += 1
                result = {"summary": "没有依据的因果。"}
        else:
            payload = json.loads(wire["messages"][1]["content"])
            result = (
                {"status": "unsupported", "reason": "原文没有因果依据", "evidence_ids": []}
                if payload["candidate"] == "没有依据的因果。"
                else _support(payload)
            )
        return httpx.Response(200, json=response(json.dumps(result)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["summary"] == "因果关系无法确定。"
    assert generated == 1 and revised == 1


def test_summary_plans_for_structured_byte_limit_before_generating_large_source(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=1, length=600_000)
    _wide_route()
    ranges: list[list[int]] = []
    verification_sizes: list[int] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        assert wire["max_tokens"] >= 65_536
        if len(wire["messages"]) > 1:
            verification_sizes.append(len(wire["messages"][1]["content"].encode("utf-8")))
            result = _support(json.loads(wire["messages"][1]["content"]))
        else:
            payload = _payload(request)
            if payload["stage"] == "source":
                ranges.append(payload["content"]["character_range"])
            result = {"summary": "原文事实。"}
        return httpx.Response(200, json=response(json.dumps(result)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    assert not ranges
    assert max(verification_sizes) > 524_288
    with get_session() as db:
        records = db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
        assert len({json.loads(row.value)["operation_id"] for row in records}) == 1


def test_insufficient_summary_plan_budget_sends_no_model_request(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=1, length=40)
    monkeypatch.setattr(settings, "summary_model_max_attempts", 1)
    calls: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=response('{"summary":"不应发送"}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert not _run(session_id)
    assert not calls
    with get_session() as db:
        pending = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert pending["status"] == "failed" and "预算" in pending["last_error"]


def test_previous_operation_draft_cannot_waive_new_root_request_budget(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=6, length=1500)
    calls: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        wire = json.loads(request.content)
        result = (
            {"summary": "完整总结正文"}
            if len(wire["messages"]) == 1
            else _support(json.loads(wire["messages"][1]["content"]))
        )
        return httpx.Response(200, json=response(json.dumps(result)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    calls.clear()
    monkeypatch.setattr(settings, "summary_model_max_attempts", 1)
    assert not _run(session_id)
    assert not calls


def test_json_object_verification_plan_uses_actual_wire_format_at_context_boundary() -> None:
    provider = LLMProvider(
        id="capacity", name="边界", base_url="https://api.deepseek.com", api_key="unused", model="deepseek-flash"
    )
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "evidence_ids": {"type": "array", "items": {"enum": [f"S{index:016x}" for index in range(100)]}}
        },
    }
    payload: dict[str, object] = {
        "candidate": "原文事实",
        "material": [{"evidence_id": "S0000000000000000", "text": "原文"}],
    }
    messages = [
        {"role": "system", "content": llm.structured_system_prompt(VERIFICATION_PROMPT, schema)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]
    wire_format = {"type": "json_object"}
    provider.context_window_tokens = (
        len(json.dumps([messages, wire_format], ensure_ascii=False, separators=(",", ":")).encode()) + 1024
    )
    assert output_budget(provider, settings.highlight_llm_max_tokens, messages, wire_format) == 512
    assert verification_fits(payload, schema, [provider])


def test_whole_revision_cannot_spend_last_request_on_an_unverifiable_result(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=6, length=1500)
    monkeypatch.setattr(settings, "summary_model_max_attempts", 2)
    root_generations = 0

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal root_generations
        wire = json.loads(request.content)
        if len(wire["messages"]) == 1:
            payload = _payload(request)
            if payload["stage"] == "source":
                result = {"summary": f"叶节点{payload['content']['sequence']}"}
            elif payload["stage"] == "revision":
                result = {"summary": "叶节点5修订"}
            else:
                root_generations += 1
                result = {"summary": "完整全场总结"}
        else:
            payload = json.loads(wire["messages"][1]["content"])
            result = (
                {"status": "unsupported", "reason": "该节点需要修订", "evidence_ids": []}
                if payload["candidate"] == "完整全场总结"
                else _support(payload)
            )
        return httpx.Response(200, json=response(json.dumps(result)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert not _run(session_id)
    assert root_generations == 1
    with get_session() as db:
        records = db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
        assert len(records) == 2


def test_local_revision_preserves_the_following_paragraph_and_separator(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=1, length=40)
    _wide_route()
    following = "后段事实。" + "乙" * 100
    initial = "没有依据的因果。" + "甲" * 3970 + "\n\n" + following

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        if len(wire["messages"]) == 1:
            payload = _payload(request)
            result = {"summary": "因果无法确定。" if payload["stage"] == "revision" else initial}
        else:
            payload = json.loads(wire["messages"][1]["content"])
            result = (
                {"status": "unsupported", "reason": "原文没有因果依据", "evidence_ids": []}
                if str(payload["candidate"]).startswith("没有依据")
                else _support(payload)
            )
        return httpx.Response(200, json=response(json.dumps(result)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["summary"] == "因果无法确定。\n\n" + following


def test_many_source_references_are_verified_in_batches_without_truncating_summary(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=40, length=50)
    _wide_route()
    candidate = "\n".join(f"事实{index}：" + "内容" * 100 + "。" for index in range(40))
    verified: set[str] = set()
    batches = 0

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal batches
        wire = json.loads(request.content)
        if len(wire["messages"]) == 1:
            result = {"summary": candidate}
        else:
            batches += 1
            payload = json.loads(wire["messages"][1]["content"])
            wanted = {int(number) for number in re.findall(r"事实(\d+)", payload["candidate"])}
            identities = [item["evidence_id"] for item in payload["material"] if item["sequence"] in wanted]
            assert 0 < len(identities) <= 32
            verified.update(identities)
            result = {"status": "supported", "reason": "所列原文支持这些事实", "evidence_ids": identities}
        return httpx.Response(200, json=response(json.dumps(result)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    assert batches > 1 and len(verified) == 40
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["summary"] == candidate


def test_batch_repair_allowance_survives_restart_without_refilling_total_budget(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(count=1, length=20)
    operation = uuid4().hex
    target = "summary-scope-test"
    answers = iter(["bad-json", '{"answer":"有效正文"}', "bad-json", "bad-json", '{"answer":"另一有效正文"}'])
    seen: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response(next(answers)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    # 此处测试共用调用边界，选择原有 analysis 路由而非替换预算实现。
    from tests.integration.test_structured_llm import catalog

    save_model_configuration(catalog(revision=configuration_view()["revision"]))
    first = ModelCallBudget(
        max_attempts=8, max_elapsed_s=60, journal=ModelRequestJournal(target=target), operation_id=operation
    )
    assert call(first, repair_scope="a" * 64).value is not None
    restored = ModelCallBudget(
        max_attempts=32, max_elapsed_s=600, journal=ModelRequestJournal(target=target), operation_id=operation
    )
    assert call(restored, repair_scope="a" * 64).value is None
    assert call(restored, repair_scope="b" * 64).value is not None
    assert len(seen) == 5 and restored.remaining_requests == 3
    with get_session() as db:
        state = json.loads(db.get(AppSetting, f"model_operation:v1:{operation}").value)
        assert state["max_attempts"] == 8 and state["repair_scopes"] == ["a" * 64, "b" * 64]
