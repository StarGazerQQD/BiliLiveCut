from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest
from sqlmodel import select

from app.analysis import llm, session_summary
from app.core.cancellation import shutdown_event
from app.core.config import settings
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession, Transcript
from app.db.session import get_session
from app.models.configuration import save_model_configuration
from tests.integration.test_structured_llm import catalog, response


def _seed(*, count: int = 6, length: int = 1500, endpoint: str = "https://fixture.invalid/v1") -> int:
    config = catalog(base_url=endpoint)
    save_model_configuration(
        config.model_copy(
            update={
                "routes": [
                    route.model_copy(update={"capability": "summary", "context_window_tokens": 6000})
                    for route in config.routes
                ]
            }
        )
    )
    started = datetime(2026, 10, 30, 11, tzinfo=UTC)
    with get_session() as db:
        room = LiveRoom(input_url="summary-fixture", title="初始来源")
        db.add(room)
        db.flush()
        recording = RecordingSession(
            room_id=room.id, status="stopped", started_at=started, ended_at=started + timedelta(hours=2)
        )
        db.add(recording)
        db.flush()
        for index in range(count):
            segment = RawSegment(
                session_id=recording.id,
                seq=index,
                file_path=f"part-{index}.ts",
                duration_s=300,
                status="scored",
                start_ts=started + timedelta(minutes=5 * index),
                end_ts=started + timedelta(minutes=5 * (index + 1)),
            )
            db.add(segment)
            db.flush()
            db.add(Transcript(segment_id=segment.id, final_text=f"segment{index}:" + "x" * length))
        return recording.id


def _payload(request: httpx.Request) -> dict[str, object]:
    prompt = json.loads(request.content)["messages"][0]["content"]
    if '"stage":' not in prompt:
        return {"stage": "whole", "content": prompt}
    return json.loads(prompt.split("\n", 1)[1].split("\n本次总结请求标识：", 1)[0])


def _reply(payload: dict[str, object], *, large_leaf: bool = False) -> str:
    stage = payload["stage"]
    if stage == "source":
        content = payload["content"]
        return f"节点{content['sequence']}:{content['character_range']}" + ("a" * 1400 if large_leaf else "事实与时间")
    return "完整全场总结" if stage in {"final", "whole"} else "归并事件与已知时间"


def _run(session_id: int) -> bool:
    assert session_summary.request_session_timeline_summary(session_id, reason="manual", force=True, renew_budget=True)
    return _continue(session_id)


def _continue(session_id: int) -> bool:
    for _ in range(33):
        claim = session_summary.claim_pending_session_summary()
        assert claim is not None
        if session_summary.execute_session_summary_claim(claim):
            return True
        with get_session() as db:
            row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
            payload = json.loads(row.value)
            if payload["status"] != "pending" or payload["last_error"] != "本轮成功节点已保存，等待继续生成":
                return False
            # 推进调度到下一轮到期；请求身份、授权预算和失败次数保持原值。
            payload["next_retry_at"] = None
            row.value = json.dumps(payload)
    raise AssertionError("正常分批未能在授权请求上限内结束")


@pytest.mark.parametrize("large_leaf", [False, True])
def test_hierarchy_uses_all_blocks_and_only_invalidates_changed_ancestors(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, large_leaf: bool
) -> None:
    session_id = _seed()
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 32)
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append(payload)
        return httpx.Response(200, json=response(json.dumps({"summary": _reply(payload, large_leaf=large_leaf)})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    assert [item["content"]["sequence"] for item in calls if item["stage"] == "source"] == list(range(6))
    assert calls[-1]["stage"] == "final"
    assert any(item["stage"] == "reduce" for item in calls) is large_leaf
    with get_session() as db:
        requests = [
            json.loads(row.value)
            for row in db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
        ]
        assert len({item["operation_id"] for item in requests}) == 1
        assert len(requests) == len(calls)
        plan = json.loads(db.get(AppSetting, f"summary_plan:v1:{session_id}").value)
        assert len(plan["nodes"]) == 6
        target = db.exec(
            select(Transcript).join(RawSegment, Transcript.segment_id == RawSegment.id).where(RawSegment.seq == 2)
        ).one()
        target.final_text = "changed:" + "y" * 1500
    calls.clear()
    assert _run(session_id)
    assert [item["content"]["sequence"] for item in calls if item["stage"] == "source"] == [2]
    assert sum(item["stage"] == "reduce" for item in calls) == (1 if large_leaf else 0)
    assert calls[-1]["stage"] == "final"


@pytest.mark.parametrize("known_end", [False, True])
def test_short_summary_preserves_dates_gaps_and_unknown_end(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, known_end: bool
) -> None:
    session_id = _seed(count=2, length=50)
    with get_session() as db:
        segments = db.exec(select(RawSegment).order_by(RawSegment.seq)).all()
        recording = db.get(RecordingSession, session_id)
        recording.started_at = datetime(2026, 10, 30, 15, 58, tzinfo=UTC)
        recording.ended_at = datetime(2026, 10, 30, 16, 25, tzinfo=UTC)
        segments[0].start_ts = recording.started_at
        segments[0].end_ts = datetime(2026, 10, 30, 16, 3, tzinfo=UTC)
        segments[1].start_ts = datetime(2026, 10, 30, 16, 20, tzinfo=UTC)
        segments[1].end_ts = recording.ended_at if known_end else None
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(_payload(request))
        return httpx.Response(200, json=response('{"summary":"有间隙的两段素材，无法确认期间情况。"}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    assert len(calls) == 1 and calls[0]["stage"] == "whole"
    prompt = calls[0]["content"]
    records = json.loads(prompt.split("整场 ASR：\n", 1)[1].split("\n覆盖情况（", 1)[0])
    assert [item["start_at"] for item in records] == ["2026-10-30T23:58:00+08:00", "2026-10-31T00:20:00+08:00"]
    assert [item["end_at"] for item in records] == [
        "2026-10-31T00:03:00+08:00",
        "2026-10-31T00:25:00+08:00" if known_end else None,
    ]
    assert [item["text"] for item in records] == [f"segment{index}:" + "x" * 50 for index in range(2)]
    assert "录制间隙" in prompt and "精确事件时间" in prompt and "因果" in prompt


@pytest.mark.parametrize("length", [50, 12000])
def test_summary_keeps_uploader_and_replay_title_in_every_request(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, length: int
) -> None:
    session_id = _seed(count=1, length=length)
    with get_session() as db:
        room = db.exec(select(LiveRoom)).one()
        room.uploader_name = "素材上传者"
        room.title = "历史直播循环回放"
    prompts: list[str] = []
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        payload = _payload(request)
        calls.append(payload)
        return httpx.Response(200, json=response(json.dumps({"summary": _reply(payload)})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    assert calls[-1]["stage"] == ("whole" if length == 50 else "final")
    assert all("素材上传者" in prompt and "历史直播循环回放" in prompt for prompt in prompts)
    assert all("录制日期不能用作原直播或内容事件的发生日期" in prompt for prompt in prompts)
    if length == 50:
        records = json.loads(prompts[0].split("整场 ASR：\n", 1)[1].split("\n覆盖情况（", 1)[0])
    else:
        records = [item["content"] for item in calls if item["stage"] == "source"]
    assert records and all(item["time_basis"] == "recording" for item in records)


def test_single_large_block_is_partitioned_without_missing_text_or_guessed_time(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=1, length=12000)
    with get_session() as db:
        segment = db.exec(select(RawSegment)).one()
        segment.start_ts = datetime(2026, 10, 30, 15, 58, tzinfo=UTC)
        segment.end_ts = datetime(2026, 10, 30, 16, 3, tzinfo=UTC)
    calls: list[dict[str, object]] = []
    prompts: list[str] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append(payload)
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, json=response(json.dumps({"summary": _reply(payload)})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    leaves = [item["content"] for item in calls if item["stage"] == "source"]
    assert len(leaves) > 1
    assert "".join(item["text"] for item in leaves) == "segment0:" + "x" * 12000
    assert len({(item["start_at"], item["end_at"]) for item in leaves}) == 1
    assert leaves[0]["start_at"] == "2026-10-30T23:58:00+08:00"
    assert leaves[0]["end_at"] == "2026-10-31T00:03:00+08:00"
    assert all("GMT+8" in prompt for prompt in prompts)
    cursor = 0
    for item in leaves:
        assert item["character_range"] == [cursor, cursor + len(item["text"])]
        cursor += len(item["text"])


def test_large_multibyte_intermediate_is_compressed_completely_in_shared_budget(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=2, length=3000)
    config = catalog(revision=1)
    save_model_configuration(
        config.model_copy(
            update={
                "routes": [
                    route.model_copy(
                        update={"capability": "summary", "context_window_tokens": 6000, "max_output_tokens": 4096}
                    )
                    for route in config.routes
                ]
            }
        )
    )
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 32)
    calls: list[dict[str, object]] = []
    oversized = "内容" * 1200

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append(payload)
        text = oversized if payload["stage"] == "source" else _reply(payload)
        return httpx.Response(200, json=response(json.dumps({"summary": text})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    compressed = [item["content"] for item in calls if item["stage"] == "compress"]
    assert compressed
    source_count = sum(item["stage"] == "source" for item in calls)
    carried = []
    for item in calls:
        texts = (
            item["content"]
            if item["stage"] == "reduce"
            else (item["content"]["summaries"] if item["stage"] == "final" else [])
        )
        carried.extend(text for text in texts if text and set(text) <= set("内容"))
    groups: list[list[dict[str, object]]] = []
    for part in compressed:
        if part["character_range"][0] == 0:
            groups.append([])
        groups[-1].append(part)
    assert len(groups) == len(carried) == source_count
    for parts, tail in zip(groups, carried, strict=True):
        cursor = 0
        for part in parts:
            start, end = part["character_range"]
            assert start == cursor and part["summary"] == oversized[start:end]
            cursor = end
        assert "".join(part["summary"] for part in parts) + tail == oversized
    assert calls[-1]["stage"] == "final"
    with get_session() as db:
        records = [
            json.loads(row.value)
            for row in db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
        ]
        assert len({record["operation_id"] for record in records}) == 1


def test_compression_preserves_single_character_tail_without_an_impossible_request(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.session_summary_tree import SessionSummaryTree, SummaryNode

    _seed(count=1)
    tree = SessionSummaryTree(
        policy="test-policy",
        budget=llm.new_editorial_budget(),
        guard=lambda: None,
        load=lambda _key: None,
        save=lambda _node: None,
        plan=lambda _keys: None,
    )
    parent = SummaryNode("a" * 64, "x" * 10000)
    low, high = 0, len(parent.text)
    while low < high:
        middle = (low + high + 1) // 2
        if tree.fits(tree._compression_prompt(parent, 0, middle)):
            low = middle
        else:
            high = middle - 1
    parent = SummaryNode(parent.key, "x" * low + "!")
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(_payload(request))
        return httpx.Response(200, json=response('{"summary":"compressed"}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    nodes = tree._compress(parent)
    assert len(calls) == 1 and calls[0]["content"]["summary"] == "x" * low
    assert [node.text for node in nodes] == ["compressed", "!"]


def test_shared_budget_exhaustion_requires_explicit_renewal_and_preserves_nodes(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed()
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append(payload)
        return httpx.Response(200, json=response(json.dumps({"summary": _reply(payload)})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 2)
    assert not _run(session_id)
    assert len(calls) == 2
    with get_session() as db:
        assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None
        pending = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
        payload = json.loads(pending.value)
        assert payload["attempts"] == 0 and payload["status"] == "failed"
        operation_id = payload["operation_id"]
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 8)
    assert session_summary.recover_running_session_summary_requests() == 0
    assert not session_summary.request_session_timeline_summary(session_id, reason="automatic", force=True)
    assert session_summary.claim_pending_session_summary() is None
    assert len(calls) == 2
    assert _run(session_id)
    with get_session() as db:
        request = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert request["operation_id"] != operation_id and request["status"] == "completed"
    assert [item["content"]["sequence"] for item in calls if item["stage"] == "source"] == list(range(6))
    assert len(calls) == 7


def test_successful_slices_share_one_fixed_budget_without_business_failures(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _seed()
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 8)
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append(payload)
        return httpx.Response(200, json=response(json.dumps({"summary": _reply(payload)})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert session_summary.request_session_timeline_summary(session_id, reason="fixed-budget")
    identities: set[tuple[str, str]] = set()
    for round_index in range(4):
        claim = session_summary.claim_pending_session_summary()
        assert claim is not None
        assert session_summary.execute_session_summary_claim(claim) is (round_index == 3)
        assert len(calls) == min((round_index + 1) * 2, 7)
        with get_session() as db:
            row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
            payload = json.loads(row.value)
            assert payload["attempts"] == 0
            assert payload["status"] == ("completed" if round_index == 3 else "pending")
            identities.add((payload["operation_id"], payload["requested_at"]))
            payload["next_retry_at"] = None
            row.value = json.dumps(payload)
    assert len(identities) == 1
    operation_id, _ = identities.pop()
    with get_session() as db:
        records = [
            json.loads(row.value)
            for row in db.exec(
                select(AppSetting).where(AppSetting.key.startswith(f"model_request:v1:{operation_id}:"))
            ).all()
        ]
        assert sorted(record["number"] for record in records) == list(range(1, 8))
        assert all(record["state"] == "finished" for record in records)
    assert session_summary.request_session_timeline_summary(session_id, reason="automatic", force=True)
    assert _continue(session_id)
    assert len(calls) == 7


@pytest.mark.parametrize("change", ["source", "text", "claim", "shutdown", "policy"])
def test_late_node_cannot_write_after_input_or_claim_changes(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    session_id = _seed()
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append(payload)
        if change in {"source", "text"}:
            with get_session() as db:
                if change == "source":
                    db.exec(select(LiveRoom)).one().title = "新的来源"
                else:
                    db.exec(select(Transcript)).first().final_text = "人工修订的新正文"
        elif change == "claim":
            session_summary.request_session_timeline_summary(session_id, reason="new-owner", force=True)
        elif change == "shutdown":
            shutdown_event.set()
        else:
            config = catalog(revision=1)
            save_model_configuration(
                config.model_copy(
                    update={
                        "routes": [
                            route.model_copy(
                                update={
                                    "capability": "summary",
                                    "model": "changed-model",
                                    "context_window_tokens": 6000,
                                }
                            )
                            for route in config.routes
                        ]
                    }
                )
            )
        return httpx.Response(200, json=response(json.dumps({"summary": _reply(payload)})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    try:
        assert not _run(session_id)
    finally:
        shutdown_event.clear()
    assert len(calls) == 1
    with get_session() as db:
        assert db.exec(select(AppSetting).where(AppSetting.key.startswith("summary_cache:v1:"))).all() == []
        assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None
        pending = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert pending["attempts"] == 0


def test_changed_source_expires_result_without_get_scheduling_or_reusing_old_final(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = _seed(count=1, length=20)
    with get_session() as db:
        db.exec(select(LiveRoom)).one().uploader_name = "固定上传者"
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append(payload)
        return httpx.Response(200, json=response(json.dumps({"summary": _reply(payload)})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert _run(session_id)
    assert len(calls) == 1 and calls[0]["stage"] == "whole"
    assert (
        session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)["status"]
        == "ready"
    )
    with get_session() as db:
        db.exec(select(LiveRoom)).one().title = "更正来源"
        before = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["needs_regeneration"] is True and view["summary"] is None
    with get_session() as db:
        assert (
            json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)["status"]
            == "completed"
        )
    assert session_summary.ensure_session_timeline_summary_requested(session_id)
    claim = session_summary.claim_pending_session_summary()
    assert claim is not None and session_summary.execute_session_summary_claim(claim)
    assert len(calls) == 2 and "更正来源" in calls[1]["content"]
    with get_session() as db:
        after = json.loads(db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value)
        assert after["operation_id"] == before["operation_id"]
        assert after["requested_at"] == before["requested_at"]


def _summary_child() -> None:
    from app.db.session import init_db

    init_db()
    with get_session() as db:
        session_id = db.exec(select(RecordingSession.id)).one()
    assert _continue(session_id)


@pytest.mark.parametrize("failure_at", ["cache", "result", "expired_result"])
def test_storage_failure_stops_requests_and_completed_root_is_reused(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    from sqlalchemy import event

    session_id = _seed()
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 8 if failure_at == "cache" else 7)
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        payload = _payload(request)
        calls.append(payload)
        return httpx.Response(200, json=response(json.dumps({"summary": _reply(payload)})))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with get_session() as db:
        engine = db.get_bind()
    triggered = False

    def fail_storage(
        connection: object, cursor: object, statement: str, parameters: object, context: object, many: bool
    ) -> None:
        nonlocal triggered
        prefix = "summary_cache:v1:" if failure_at == "cache" else "session_timeline_summary:"
        if statement.startswith("INSERT INTO app_settings") and any(
            isinstance(value, str) and value.startswith(prefix) for value in parameters
        ):
            triggered = True
            raise sqlite3.OperationalError("controlled summary storage failure")

    event.listen(engine, "before_cursor_execute", fail_storage)
    try:
        assert not _run(session_id)
    finally:
        event.remove(engine, "before_cursor_execute", fail_storage)
    assert triggered and len(calls) == (1 if failure_at == "cache" else 7)
    before_retry = len(calls)
    if failure_at == "expired_result":
        import app.analysis.model_execution as execution
        import app.analysis.model_journal as journal

        future = datetime.now(UTC) + timedelta(days=1)

        class FutureClock(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return future

        monkeypatch.setattr(execution, "datetime", FutureClock)
        monkeypatch.setattr(journal, "datetime", FutureClock)
    with get_session() as db:
        assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None
        row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
        pending = json.loads(row.value)
        assert pending["attempts"] == 1 and pending["status"] == "pending"
        pending["next_retry_at"] = None
        row.value = json.dumps(pending)
    assert _continue(session_id)
    assert len(calls) - before_retry == (7 if failure_at == "cache" else 0)


def test_real_invalid_summary_stops_after_three_failures_without_resetting_budget(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = _seed(count=1, length=20)
    calls: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=response("{}"))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert session_summary.request_session_timeline_summary(session_id, reason="invalid-response")
    identities: set[str] = set()
    for attempt in range(1, 4):
        assert not _continue(session_id)
        with get_session() as db:
            row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
            payload = json.loads(row.value)
            assert payload["attempts"] == attempt and payload["status"] == ("failed" if attempt == 3 else "pending")
            identities.add(payload["operation_id"])
            payload["next_retry_at"] = None
            row.value = json.dumps(payload)
    assert len(calls) == 3 and len(identities) == 1
    assert not session_summary.request_session_timeline_summary(session_id, reason="automatic", force=True)
    assert session_summary.claim_pending_session_summary() is None


def test_old_active_request_is_preserved_until_explicit_budget_renewal(temp_db: None) -> None:
    session_id = _seed(count=1, length=20)
    assert session_summary.request_session_timeline_summary(session_id, reason="old-request")
    with get_session() as db:
        row = db.get(AppSetting, f"session_timeline_summary_request:{session_id}")
        payload = json.loads(row.value)
        del payload["operation_id"]
        payload.update({"version": 3, "status": "running"})
        original = row.value = json.dumps(payload)
    assert session_summary.recover_running_session_summary_requests() == 0
    assert session_summary.claim_pending_session_summary() is None
    assert not session_summary.ensure_session_timeline_summary_requested(session_id)
    assert not session_summary.request_session_timeline_summary(session_id, reason="automatic", force=True)
    with get_session() as db:
        assert db.get(AppSetting, f"session_timeline_summary_request:{session_id}").value == original
    view = session_summary.session_timeline_summary_view(session_id, processing_state="completed", ended=True)
    assert view["status"] == "failed" and "人工重新生成" in view["error"]
    assert session_summary.request_session_timeline_summary(session_id, reason="manual", force=True, renew_budget=True)
    assert session_summary.claim_pending_session_summary() is not None


def test_real_process_restart_reuses_committed_leaf_and_keeps_unknown_inflight_request(
    temp_db: None, tmp_path: Path
) -> None:
    arrived = Event()
    release = Event()
    calls: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = _payload(httpx.Request("POST", "http://127.0.0.1", content=body))
            calls.append(payload)
            if len(calls) == 2:
                arrived.set()
                release.wait(25)
                self.close_connection = True
                return
            encoded = json.dumps(response(json.dumps({"summary": _reply(payload)}))).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session_id = _seed(count=4, length=2000, endpoint=f"http://127.0.0.1:{server.server_port}/v1")
    assert session_summary.request_session_timeline_summary(session_id, reason="process-test")
    environment = dict(os.environ, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    log_path = tmp_path / "summary-child.log"
    with log_path.open("wb") as log:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from tests.integration.test_session_summary_recovery import _summary_child; _summary_child()",
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        try:
            assert arrived.wait(15), log_path.read_text(encoding="utf-8", errors="replace")
            child.kill()
            child.wait(timeout=5)
            with get_session() as db:
                assert len(db.exec(select(AppSetting).where(AppSetting.key.startswith("summary_cache:v1:"))).all()) == 1
                assert db.get(AppSetting, f"session_timeline_summary:{session_id}") is None
                records = [
                    json.loads(row.value)
                    for row in db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
                ]
                unknown = [record for record in records if record["state"] == "unconfirmed"]
                assert len(unknown) == 1 and unknown[0]["usage"]["estimated_cost_usd"] is None
            assert session_summary.recover_running_session_summary_requests() == 1
            assert _continue(session_id)
            assert [item["content"]["sequence"] for item in calls if item["stage"] == "source"] == [0, 1, 1, 2, 3]
            assert calls[-1]["stage"] == "final"
            with get_session() as db:
                records = [
                    json.loads(row.value)
                    for row in db.exec(select(AppSetting).where(AppSetting.key.startswith("model_request:v1:"))).all()
                ]
                assert len({record["operation_id"] for record in records}) == 1
                assert sorted(record["number"] for record in records) == list(range(1, len(calls) + 1))
                assert len([record for record in records if record["state"] == "unconfirmed"]) == 1
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            release.set()
            llm.close_clients()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
