from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo

import httpx
import pytest
from sqlmodel import select

from app.analysis import llm
from app.analysis import segment_summary as summaries
from app.core import settings_store
from app.core.cancellation import OperationCancelledError
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession, Transcript
from app.db.session import get_session
from app.models.configuration import save_model_configuration
from tests.integration.test_structured_llm import catalog, response


@pytest.fixture
def service(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, object]]]:
    config = catalog()
    save_model_configuration(
        config.model_copy(
            update={
                "routes": [route.model_copy(update={"capability": "transcript"}) for route in config.routes],
            }
        )
    )
    monkeypatch.setattr(settings_store, "transcript_llm_refine_enabled", lambda: True)
    calls: list[dict[str, object]] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        payload = json.loads(body["messages"][1]["content"])
        target = payload.get("target")
        if target:
            value = {
                "summary": "本段继续讨论赛制。",
                "needs_context": False,
                "memory": [{"text": "正在讨论赛制。", "source_ids": [target["segment_id"]]}],
            }
        else:
            value = {"memory": payload["memory"]}
        return httpx.Response(200, json=response(json.dumps(value, ensure_ascii=False)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    yield calls
    llm.close_clients()


def seed(texts: list[str | None]) -> tuple[int, list[int]]:
    with get_session() as db:
        room = LiveRoom(input_url="summary-fixture")
        db.add(room)
        db.flush()
        session = RecordingSession(room_id=room.id, started_at=datetime(2026, 11, 6))
        db.add(session)
        db.flush()
        ids: list[int] = []
        for index, text in enumerate(texts):
            segment = RawSegment(
                session_id=session.id,
                seq=index,
                file_path=f"raw/{session.id}/{index}.ts",
                start_ts=datetime(2026, 11, 6) + timedelta(minutes=index * 5),
                end_ts=datetime(2026, 11, 6) + timedelta(minutes=(index + 1) * 5),
            )
            db.add(segment)
            db.flush()
            ids.append(segment.id)
            if text is not None:
                db.add(Transcript(segment_id=segment.id, final_text=text, words_json="[]"))
        summaries.request_segment_summary(db, session.id)
        return session.id, ids


def run_one() -> summaries.SegmentSummaryClaim:
    claim = summaries.claim_pending_segment_summary()
    assert claim is not None
    assert summaries.execute_segment_summary_claim(claim)
    return claim


def test_summary_uses_committed_originals_in_recording_order_and_keeps_asr(service: list[dict[str, object]]) -> None:
    session, ids = seed(["先说明小明的身份和赛制。", "他接着解释加赛。"])
    run_one()
    run_one()
    assert len(service) == 2
    second = json.loads(service[1]["messages"][1]["content"])
    assert second["target"]["segment_id"] == ids[1]
    assert second["history"][0]["text"] == "先说明小明的身份和赛制。"
    assert second["memory"][0]["source_ids"] == [ids[0]]
    assert "summary" not in second["history"][0]
    with get_session() as db:
        original = db.exec(select(Transcript).where(Transcript.segment_id == ids[0])).one()
        assert original.final_text == "先说明小明的身份和赛制。" and original.words_json == "[]"
        assert summaries.segment_summary_view(db, session, ids[1])["summary_status"] == "ready"


def test_late_predecessor_waits_without_http_and_then_replays_in_order(service: list[dict[str, object]]) -> None:
    session, ids = seed([None, "后一段已经转写完成。"])
    assert summaries.claim_pending_segment_summary() is None and not service
    with get_session() as db:
        db.add(Transcript(segment_id=ids[0], final_text="迟到的前段。"))
        summaries.request_segment_summary(db, session)
    assert run_one().step.segment_id == ids[0]
    assert run_one().step.segment_id == ids[1]


def test_other_sessions_never_receive_each_others_memory(service: list[dict[str, object]]) -> None:
    first, _ = seed(["第一场的私有上下文。"])
    second, _ = seed(["第二场的独立上下文。"])
    assert run_one().step.session_id == first
    assert run_one().step.session_id == second
    payload = json.loads(service[1]["messages"][1]["content"])
    assert payload["memory"] == [] and payload["history"] == []
    assert "第一场" not in json.dumps(payload, ensure_ascii=False)


def test_tail_append_keeps_running_identity_and_model_budget(service: list[dict[str, object]]) -> None:
    session, ids = seed(["本段内容。"])
    claim = summaries.claim_pending_segment_summary()
    assert claim is not None
    with get_session() as db:
        segment = RawSegment(session_id=session, seq=1, file_path="raw/new.ts", start_ts=datetime(2026, 11, 6, 0, 5))
        db.add(segment)
        db.flush()
        db.add(Transcript(segment_id=segment.id, final_text="新追加内容。"))
        summaries.request_segment_summary(db, session)
    assert summaries.execute_segment_summary_claim(claim)
    assert run_one().step.segment_id != ids[0]
    assert len(service) == 2


def test_edit_revokes_old_claim_before_http_and_invalidates_suffix(service: list[dict[str, object]]) -> None:
    session, ids = seed(["原来的人物。", "随后他说话。"])
    run_one()
    claim = summaries.claim_pending_segment_summary()
    assert claim is not None
    with get_session() as db:
        original = db.exec(select(Transcript).where(Transcript.segment_id == ids[0])).one()
        original.final_text, original.final_text_source = "人工改正的人物。", "manual"
        db.add(original)
        summaries.request_segment_summary(db, session)
    with pytest.raises(OperationCancelledError):
        summaries.execute_segment_summary_claim(claim)
    assert len(service) == 1
    summaries.release_segment_summary(claim)
    assert run_one().step.segment_id == ids[0]
    with get_session() as db:
        assert summaries.segment_summary_view(db, session, ids[1])["context_summary"] is None


def test_restart_reuses_step_operation_and_does_not_duplicate_ready_summary(service: list[dict[str, object]]) -> None:
    _, _ = seed(["可恢复的摘要。"])
    claim = summaries.claim_pending_segment_summary()
    assert claim is not None
    summaries.recover_segment_summaries()
    resumed = summaries.claim_pending_segment_summary()
    assert resumed is not None and resumed.step.operation_id == claim.step.operation_id
    assert resumed.step.lease != claim.step.lease
    assert summaries.execute_segment_summary_claim(resumed)
    assert summaries.claim_pending_segment_summary() is None and len(service) == 1


def test_late_release_cannot_revoke_a_committed_success(service: list[dict[str, object]]) -> None:
    session, ids = seed(["已经提交的摘要。"])
    claim = run_one()
    summaries.release_segment_summary(claim, failed=True)
    with get_session() as db:
        view = summaries.segment_summary_view(db, session, ids[0])
        assert view["summary_status"] == "ready" and view["summary_error"] is None
    assert summaries.claim_pending_segment_summary() is None and len(service) == 1


def test_terminal_predecessor_gap_wakes_waiting_queue_without_a_transcript(service: list[dict[str, object]]) -> None:
    from app.db.entities import SegmentTask, TaskStatus

    session, ids = seed([None, "缺失片段之后的原文。"])
    assert summaries.claim_pending_segment_summary() is None
    with get_session() as db:
        db.add(SegmentTask(segment_id=ids[0], session_id=session, stage=TaskStatus.FAILED))
    summaries.compensate_segment_summaries()
    assert run_one().step.segment_id == ids[1]
    payload = json.loads(service[0]["messages"][1]["content"])
    assert payload["history"][0]["gap"] and payload["history"][0]["segment_id"] == ids[0]


@pytest.mark.parametrize("resume", ["direct", "release", "restart"])
def test_forced_failure_preserves_same_source_success_and_separate_error(
    service: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    resume: str,
) -> None:
    session, ids = seed(["有效原文。"])
    run_one()

    async def invalid(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        service.append(json.loads(request.content))
        return httpx.Response(200, json=response("not-json"))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", invalid)
    with get_session() as db:
        summaries.request_segment_summary(db, session, force=True)
    claim = summaries.claim_pending_segment_summary()
    assert claim is not None
    if resume != "direct":
        operation = claim.step.operation_id
        if resume == "release":
            summaries.release_segment_summary(claim)
        else:
            summaries.recover_segment_summaries()
        claim = summaries.claim_pending_segment_summary()
        assert claim is not None and claim.step.operation_id == operation
    assert claim is not None and not summaries.execute_segment_summary_claim(claim)
    with get_session() as db:
        view = summaries.segment_summary_view(db, session, ids[0])
        assert view["summary_status"] == "ready" and view["context_summary"] == "本段继续讨论赛制。"
        assert view["summary_error"] == "invalid_output"
    assert summaries.claim_pending_segment_summary() is None


def test_memory_cannot_cite_future_or_unknown_sources(service: list[dict[str, object]]) -> None:
    value = {
        "summary": "本段摘要。",
        "needs_context": False,
        "memory": [{"text": "来自未来的猜测。", "source_ids": [99]}],
    }
    with pytest.raises(ValueError, match="原文段号"):
        summaries._checked(value, summaries.SegmentSummaryOutput, {1})


@pytest.mark.parametrize("recover", [True, False])
def test_transient_failure_resumes_once_with_same_operation_after_restart(
    service: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch, recover: bool
) -> None:
    session, ids = seed(["第一段原文。", "下一段原文。"])
    clock = datetime.now(UTC)
    responses = 0

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return clock.astimezone(tz)

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal responses
        body = json.loads(request.content)
        service.append(body)
        target = json.loads(body["messages"][1]["content"])["target"]["segment_id"]
        if target == ids[0]:
            responses += 1
            if responses <= 2 or not recover:
                return httpx.Response(503, headers={"Retry-After": "0"}, json={"error": {"message": "unavailable"}})
        value = {"summary": "依据本段内容的摘要。", "needs_context": False, "memory": []}
        return httpx.Response(200, json=response(json.dumps(value, ensure_ascii=False)))

    monkeypatch.setattr(summaries, "datetime", Clock)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    first = summaries.claim_pending_segment_summary()
    assert first is not None and not summaries.execute_segment_summary_claim(first)
    assert responses == 2
    summaries.recover_segment_summaries()
    with get_session() as db:
        summaries.request_segment_summary(db, session)
        view = summaries.segment_summary_view(db, session, ids[0])
        assert view["summary_status"] == "pending" and view["summary_error"] == "provider_error"
    other, _ = seed(["另一场原文。"])
    assert run_one().step.session_id == other
    assert summaries.claim_pending_segment_summary() is None
    assert responses == 2
    clock += timedelta(seconds=5)
    second = summaries.claim_pending_segment_summary()
    assert second is not None and second.step.segment_id == ids[0]
    assert second.step.operation_id == first.step.operation_id and second.step.lease != first.step.lease
    assert summaries.execute_segment_summary_claim(second) is recover
    assert responses == 3
    with get_session() as db:
        view = summaries.segment_summary_view(db, session, ids[0])
        assert view["summary_status"] == ("ready" if recover else "failed")
    assert run_one().step.segment_id == ids[1]
    assert summaries.claim_pending_segment_summary() is None


@pytest.mark.parametrize("defect", ["blank_summary", "blank_memory", "unknown_source", "long_summary"])
def test_model_repair_receives_exact_segment_validation_location(
    service: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    session, ids = seed(["有效原文。"])
    bad_summary, bad_memory, issue = {
        "blank_summary": (" ", [], {"location": ["summary"], "type": "blank_text"}),
        "blank_memory": (
            "本段摘要。",
            [{"text": " ", "source_ids": ids}],
            {"location": ["memory", 0, "text"], "type": "blank_text"},
        ),
        "unknown_source": (
            "本段摘要。",
            [{"text": "不得引用其他来源。", "source_ids": [ids[0] + 100]}],
            {"location": ["memory", 0, "source_ids", 0], "type": "not_in_enum"},
        ),
        "long_summary": (
            "长" * 1001,
            [],
            {"location": ["summary"], "type": "string_too_long", "limits": {"max_length": 1000}},
        ),
    }[defect]
    recover = defect != "unknown_source"

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        service.append(body)
        if len(service) == 1:
            value = {"summary": bad_summary, "needs_context": False, "memory": bad_memory}
        else:
            feedback = json.loads(body["messages"][-1]["content"])["validation_feedback"]
            assert feedback["issues"] == [issue]
            value = {"summary": "本段摘要。" if recover else " ", "needs_context": False, "memory": []}
        return httpx.Response(200, json=response(json.dumps(value, ensure_ascii=False)))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    claim = summaries.claim_pending_segment_summary()
    assert claim is not None and summaries.execute_segment_summary_claim(claim) is recover
    with get_session() as db:
        view = summaries.segment_summary_view(db, session, ids[0])
        assert view["summary_status"] == ("ready" if recover else "failed")
        assert view["summary_error"] == (None if recover else "invalid_output")
    assert len(service) == 2 and summaries.claim_pending_segment_summary() is None


def test_source_revision_is_visible_as_stale_without_overwriting_history(service: list[dict[str, object]]) -> None:
    session, ids = seed(["旧原文。"])
    run_one()
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == ids[0])).one()
        transcript.final_text = "改变了的原文。"
        db.add(transcript)
    with get_session() as db:
        view = summaries.segment_summary_view(db, session, ids[0])
        assert view["summary_status"] == "stale" and view["context_summary"] is None
        saved = db.get(AppSetting, f"segment_summary_step:v1:{session}:{ids[0]}")
        assert "本段继续讨论赛制" in saved.value


def test_rebuilt_parent_keeps_downstream_stale_while_next_step_is_running(service: list[dict[str, object]]) -> None:
    session, ids = seed(["第一版前文。", "后段内容。", "最后一段。"])
    run_one()
    run_one()
    run_one()
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == ids[0])).one()
        transcript.final_text = "修订后的前文。"
        db.add(transcript)
        summaries.request_segment_summary(db, session)
    run_one()
    assert summaries.claim_pending_segment_summary().step.segment_id == ids[1]
    with get_session() as db:
        view = summaries.segment_summary_views(db, session)
        assert view[ids[1]]["summary_status"] == "running"
        assert view[ids[2]]["summary_status"] == "stale"
        assert view[ids[2]]["context_summary"] is None


def test_following_asr_revises_waiting_summary_without_consuming_future_memory(
    service: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, ids = seed(["他刚才说的那件事。"])

    async def respond(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        service.append(body)
        payload = json.loads(body["messages"][1]["content"])
        after = payload["following"]
        return httpx.Response(
            200,
            json=response(
                json.dumps(
                    {
                        "summary": "正在讨论加赛。" if after else "指代尚不明确。",
                        "needs_context": not bool(after),
                        "memory": [{"text": "仍在讨论先前事件。", "source_ids": [payload["target"]["segment_id"]]}],
                    }
                )
            ),
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    run_one()
    with get_session() as db:
        segment = RawSegment(session_id=session, seq=1, start_ts=datetime(2026, 11, 6, 0, 5), file_path="raw/next.ts")
        db.add(segment)
        db.flush()
        db.add(Transcript(segment_id=segment.id, final_text="这里的他是小明，他在讨论加赛。"))
        summaries.request_segment_summary(db, session)
    backfill = run_one()
    assert backfill.step.segment_id == ids[0]
    assert json.loads(service[-1]["messages"][1]["content"])["following"][0]["sequence"] == 1
    following = run_one()
    assert following.step.segment_id != ids[0]
    assert summaries.claim_pending_segment_summary() is None
    assert len(service) == 3


def test_capacity_rollover_compresses_then_keeps_overlap_originals(service: list[dict[str, object]]) -> None:
    config = catalog(revision=1)
    save_model_configuration(
        config.model_copy(
            update={
                "routes": [
                    r.model_copy(
                        update={
                            "capability": "transcript",
                            "context_window_tokens": 20000,
                        }
                    )
                    for r in config.routes
                ]
            }
        )
    )
    session, ids = seed([f"segment-{i}:" + "a" * 1600 for i in range(12)])
    for _ in ids:
        run_one()
    payloads = [json.loads(c["messages"][1]["content"]) for c in service]
    compressions = [p for p in payloads if "target" not in p]
    assert compressions
    for payload in compressions:
        assert payload["history"] and all("text" in c for c in payload["history"])
    last = payloads[-1]
    assert last["target"]["segment_id"] == ids[-1]
    assert last["history"][-1]["segment_id"] == ids[-2]
    assert last["memory"]
    with get_session() as db:
        assert summaries.segment_summary_view(db, session, ids[-1])["summary_status"] == "ready"


def test_restart_after_compression_reuses_paid_checkpoint(
    service: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.cancellation import OperationShutdownError

    config = catalog(revision=1)
    save_model_configuration(
        config.model_copy(
            update={
                "routes": [
                    r.model_copy(
                        update={
                            "capability": "transcript",
                            "context_window_tokens": 20000,
                        }
                    )
                    for r in config.routes
                ]
            }
        )
    )
    seed([f"segment-{i}:" + "a" * 1600 for i in range(12)])
    send = httpx.AsyncHTTPTransport.handle_async_request
    compacted = False

    async def interrupt_after_compression(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        nonlocal compacted
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        if compacted and "target" in payload:
            raise OperationShutdownError("测试关闭边界")
        result = await send(transport, request)
        if "target" not in payload:
            compacted = True
        return result

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", interrupt_after_compression)
    for _ in range(12):
        claim = summaries.claim_pending_segment_summary()
        assert claim is not None
        try:
            summaries.execute_segment_summary_claim(claim)
        except OperationShutdownError:
            break
    assert compacted
    before = len([c for c in service if "target" not in json.loads(c["messages"][1]["content"])])
    summaries.recover_segment_summaries()
    resumed = summaries.claim_pending_segment_summary()
    assert resumed is not None and resumed.step.operation_id == claim.step.operation_id
    assert resumed.step.prepared_memory is not None
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    assert summaries.execute_segment_summary_claim(resumed)
    assert len([c for c in service if "target" not in json.loads(c["messages"][1]["content"])]) == before


@pytest.mark.asyncio
async def test_worker_tracks_segment_execution_and_never_claims_it_twice(service: list[dict[str, object]]) -> None:
    import asyncio

    from app.pipeline.task_worker import TaskWorker

    session, ids = seed(["后台独立处理的原文。"])
    worker = TaskWorker()
    await worker._dispatch_segment_summaries()
    occupied = tuple(worker._segment_summaries)
    assert len(occupied) == 1 and occupied[0] in worker._executions()
    await worker._dispatch_segment_summaries()
    await asyncio.gather(*occupied)
    await worker._dispatch_segment_summaries()
    assert not worker._segment_summaries and not worker._segment_summary_claims
    assert len(service) == 1
    with get_session() as db:
        assert summaries.segment_summary_view(db, session, ids[0])["summary_status"] == "ready"


def test_full_session_input_uses_original_asr_not_refinement_or_rolling_memory(
    service: list[dict[str, object]],
) -> None:
    from app.analysis.session_summary import _session_transcript_blocks

    session, ids = seed(["必须保留的完整原始ASR。"])
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == ids[0])).one()
        transcript.auxiliary_json = json.dumps(
            {
                "transcript_refinement": {
                    "version": 1,
                    "applied": True,
                    "clean_text": "整理后的另一种表述。",
                    "summary": "短概括。",
                }
            }
        )
        db.add(transcript)
    run_one()
    with get_session() as db:
        blocks = _session_transcript_blocks(db, session)
        assert [b.text for b in blocks] == ["必须保留的完整原始ASR。"]


def test_repair_feedback_keeps_exact_array_location_and_numeric_limit_out_of_diagnostic(
    service: list[dict[str, object]],
) -> None:
    from pydantic import ValidationError

    from app.analysis.editorial_explanation import GroundingOutput

    payload = {
        "judgements": [
            {"field": f, "supported": False, "reason": "长" * 241, "quotes": []} for f in ("title", "summary")
        ]
    }
    with pytest.raises(ValidationError) as caught:
        GroundingOutput.model_validate(payload)
    feedback = llm._validation_repair_feedback(caught.value, GroundingOutput.model_json_schema())
    assert feedback["issues"][0]["location"] == ["judgements", 0, "reason"]
    assert feedback["issues"][0]["limits"] == {"max_length": 240}
    diagnostic = llm._validation_failure_diagnostic(caught.value, GroundingOutput.model_json_schema())
    assert "长" not in diagnostic.summary and "241" not in diagnostic.summary
