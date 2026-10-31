from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

from app.analysis import llm
from app.analysis.reanalysis import (
    process_pending_session_reanalyses,
    queue_session_reanalysis,
    request_session_reanalysis,
)
from app.analysis.transcription.content import transcript_text
from app.analysis.transcription.refinement import compute_refinement, refinement_needs_retry, store_refinement
from app.core import settings_store
from app.core.cancellation import OperationCancelledError, shutdown_event
from app.db.entities import AppSetting, LiveRoom, RawSegment, RecordingSession, SegmentTask, TaskStatus, Transcript
from app.db.session import get_session
from app.models.configuration import save_model_configuration
from app.pipeline.claiming import pop_and_claim
from app.pipeline.lease import TaskLease
from app.pipeline.workers.transcribe import commit_transcript, transcribe_compute
from tests.integration.test_structured_llm import catalog, response
from tests.unit.test_editorial_review import _seed


@pytest.mark.parametrize("change", ["none", "manual", "source", "lease", "shutdown"])
def test_failed_refinement_reuses_persisted_raw_text_and_rejects_late_changes(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _, segment_id = _seed(pcm_wav)
    config = catalog()
    save_model_configuration(
        config.model_copy(
            update={"routes": [route.model_copy(update={"capability": "transcript"}) for route in config.routes]}
        )
    )
    monkeypatch.setattr(settings_store, "transcript_llm_refine_enabled", lambda: True)
    with get_session() as db:
        original = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        session_id = db.get(RawSegment, segment_id).session_id
        raw, words, transcript_id = original.final_text, original.words_json, original.id
    calls: list[httpx.Request] = []
    task_id: int | None = None

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(403, json={"error": {"message": "controlled denial"}})
        assert raw in json.loads(request.content)["messages"][0]["content"]
        if change in {"manual", "source", "lease"}:
            with get_session() as db:
                transcript = db.get(Transcript, transcript_id)
                if change == "manual":
                    transcript.final_text_source = "manual"
                    transcript.final_text = "用户保存的人工正文"
                elif change == "source":
                    transcript.words_json = "[]"
                else:
                    db.get(SegmentTask, task_id).lease_token = "new-owner"
        elif change == "shutdown":
            shutdown_event.set()
        return httpx.Response(200, json=response('{"clean_text":"整理后的规则讲解。","summary":"比赛规则"}'))

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    first = compute_refinement(raw, target=f"segment:{segment_id}")
    assert first.status == "failed" and len(first.attempts) == 1
    with get_session() as db:
        transcript = db.get(Transcript, transcript_id)
        assert store_refinement(db, transcript, first)
        assert transcript.final_text == raw and transcript.words_json == words
        assert refinement_needs_retry(db, transcript)
    assert queue_session_reanalysis(session_id, reason="manual", retranscribe=False, retry_refinement=True).queued == 1
    claim = pop_and_claim(TaskStatus.QUEUED_FOR_TRANS)
    assert claim is not None
    task_id = claim.id
    lease = TaskLease(claim.id, claim.claimed_by, claim.lease_token, TaskStatus.TRANSCRIBING)

    from app.analysis.transcription import pipeline

    def unexpected_asr() -> None:
        raise AssertionError("整理补做不得获取 ASR 或准备 PCM")

    monkeypatch.setattr(pipeline, "get_task_pipeline", unexpected_asr)
    try:
        if change in {"lease", "shutdown"}:
            with pytest.raises(OperationCancelledError):
                transcribe_compute(task_id)
        else:
            result = transcribe_compute(task_id)
            assert result["refinement_only"] is True
            commit_transcript(lease, result, 1)
        with get_session() as db:
            transcript = db.get(Transcript, transcript_id)
            saved = db.get(AppSetting, f"transcript_refinement:v1:{segment_id}")
            assert len(db.exec(select(Transcript)).all()) == 1
            if change == "none":
                assert transcript_text(transcript) == "整理后的规则讲解。"
                assert transcript.final_text == raw and transcript.words_json == words
                assert json.loads(saved.value)["status"] == "succeeded"
                assert db.get(SegmentTask, task_id).stage == TaskStatus.TRANSCRIBED
                assert not refinement_needs_retry(db, transcript)
            else:
                assert json.loads(saved.value)["status"] == "failed"
                assert "transcript_refinement" not in json.loads(transcript.auxiliary_json or "{}")
                assert transcript.final_text == ("用户保存的人工正文" if change == "manual" else raw)
                assert transcript.words_json == ("[]" if change == "source" else words)
        assert len(calls) == 2
    finally:
        shutdown_event.clear()
        llm.close_clients()


def test_manual_reanalysis_of_direct_transcript_can_create_task_without_enabling_automatic_analysis(
    temp_db: None, pcm_wav: Path
) -> None:
    from app.web.routers.segments import ReanalysisRequest, reanalyze_session

    _, segment_id = _seed(pcm_wav)
    with get_session() as db:
        session_id = db.get(RawSegment, segment_id).session_id
        recording = db.get(RecordingSession, session_id)
        db.get(LiveRoom, recording.room_id).auto_analyze = False
    assert not request_session_reanalysis(session_id, reason="session_finalized")
    assert reanalyze_session(session_id, ReanalysisRequest(reason="manual"))["requested"] is True
    results = process_pending_session_reanalyses()
    assert len(results) == 1 and results[0].queued == 1
    with get_session() as db:
        assert db.exec(select(SegmentTask)).one().stage == TaskStatus.QUEUED_FOR_ANALYSIS
        assert db.get(LiveRoom, recording.room_id).auto_analyze is False


@pytest.mark.parametrize(
    "request_mode", ["automatic", "legacy", "manual", "manual_then_automatic", "manual_during_consume"]
)
def test_only_persisted_manual_request_authorizes_failed_refinement_retry(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, request_mode: str
) -> None:
    _, segment_id = _seed(pcm_wav)
    config = catalog()
    save_model_configuration(
        config.model_copy(
            update={"routes": [route.model_copy(update={"capability": "transcript"}) for route in config.routes]}
        )
    )
    monkeypatch.setattr(settings_store, "transcript_llm_refine_enabled", lambda: True)
    calls: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(403, json={"error": {"message": "controlled denial"}})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        session_id = db.get(RawSegment, segment_id).session_id
        raw = transcript.final_text
        db.add(SegmentTask(segment_id=segment_id, session_id=session_id, stage=TaskStatus.COMPLETED))
    draft = compute_refinement(raw, target=f"segment:{segment_id}")
    assert draft.status == "failed"
    with get_session() as db:
        assert store_refinement(db, db.get(Transcript, transcript.id), draft)
    manual = request_mode in {"manual", "manual_then_automatic"}
    assert request_session_reanalysis(session_id, reason="manual" if manual else "session_finalized", explicit=manual)
    if request_mode == "manual_then_automatic":
        assert request_session_reanalysis(session_id, reason="session_finalized")
    elif request_mode == "legacy":
        with get_session() as db:
            pending = db.get(AppSetting, f"session_reanalysis:{session_id}")
            payload = json.loads(pending.value)
            del payload["retry_refinement"]
            pending.value = json.dumps(payload)
    if request_mode == "manual_during_consume":
        from sqlalchemy import event

        with get_session() as db:
            engine = db.get_bind()
        changed = False

        def request_before_queue_transaction(
            connection: object, cursor: object, statement: str, parameters: object, context: object, many: bool
        ) -> None:
            nonlocal changed
            if statement == "BEGIN IMMEDIATE" and not changed:
                changed = True
                assert request_session_reanalysis(session_id, reason="manual", explicit=True)

        event.listen(engine, "before_cursor_execute", request_before_queue_transaction)
        try:
            first_results = process_pending_session_reanalyses()
        finally:
            event.remove(engine, "before_cursor_execute", request_before_queue_transaction)
        assert changed and first_results[0].queued == 1
        with get_session() as db:
            pending = db.get(AppSetting, f"session_reanalysis:{session_id}")
            assert pending is not None and json.loads(pending.value)["retry_refinement"] is True
            task = db.exec(select(SegmentTask)).one()
            assert task.stage == TaskStatus.QUEUED_FOR_ANALYSIS
            task.stage = TaskStatus.COMPLETED
        manual = True
    results = process_pending_session_reanalyses()
    assert len(results) == 1 and results[0].queued == 1
    with get_session() as db:
        task = db.exec(select(SegmentTask)).one()
        assert task.stage == (TaskStatus.QUEUED_FOR_TRANS if manual else TaskStatus.QUEUED_FOR_ANALYSIS)
        assert json.loads(task.context_json)["reanalysis"]["refinement_only"] is manual
        assert db.get(AppSetting, f"session_reanalysis:{session_id}") is None
        assert db.get(Transcript, transcript.id).final_text == raw
    assert len(calls) == 1
