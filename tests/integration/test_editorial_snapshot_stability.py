from __future__ import annotations

import json
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

from app.analysis.editorial_review import review_fingerprint, review_policy_identity
from app.analysis.editorial_review_sources import media_validation_scope, segment_review_revision
from app.analysis.event_enricher import build_event_evidence_bundle
from app.analysis.reanalysis_targets import list_reanalysis_targets, target_fingerprint
from app.core.config import settings
from app.db.entities import (
    AppSetting,
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    HotspotStatus,
    RawSegment,
    SegmentTask,
    TaskStatus,
    Transcript,
)
from app.db.session import get_session
from app.pipeline.workers.analyze import (
    _commit_event_analysis,
    _compute_event_clip_result,
    _reusable_event_result,
    _score_pending_hotspot_events,
)
from tests.integration.test_editorial_commit_order import _task
from tests.unit.test_editorial_review import START, _seed, _service


def _long_recording(tmp_path: Path) -> tuple[int, int]:
    path = tmp_path / "complete.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        for _ in range(600):
            output.writeframes(b"\x00\x00" * 16000)
    event_id, segment_id = _seed(path)
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        segment.end_ts = START + timedelta(seconds=600)
        segment.duration_s = 600
    return event_id, segment_id


def test_outside_recording_growth_preserves_event_draft_and_media_preflight(
    temp_db: None, tmp_path: Path, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id, segment_id = _long_recording(tmp_path)
    monkeypatch.setattr(settings, "clip_max_duration_s", 180)
    lease = _task(segment_id)
    calls = _service(monkeypatch, "recommend")
    draft = _compute_event_clip_result(event_id)
    assert draft is not None and len(calls) == 3
    with get_session() as db:
        before = segment_review_revision(db, segment_id)
        session_id = db.get(HotspotEvent, event_id).session_id
        target = list_reanalysis_targets(db, session_id)[0]
    with media_validation_scope(session_ids=[session_id]):
        with get_session() as db:
            db.add(
                RawSegment(
                    session_id=session_id,
                    seq=1,
                    file_path=str(pcm_wav),
                    start_ts=START + timedelta(seconds=600),
                    end_ts=START + timedelta(seconds=608.336),
                    duration_s=8.336,
                )
            )
        assert _reusable_event_result(draft, event_id, lease) is draft
        with get_session() as db:
            assert target_fingerprint(db, target) == target.fingerprint
    assert len(calls) == 3
    with get_session() as db:
        assert segment_review_revision(db, segment_id) == before


def test_execution_settings_preserve_semantic_identity_and_full_execution_provenance(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id, _ = _long_recording(tmp_path)
    _service(monkeypatch, "recommend")
    with get_session() as db:
        initial = build_event_evidence_bundle(db, event_id)
    policy = review_policy_identity()
    fingerprint = review_fingerprint(initial.source, initial.source, initial.fingerprint, policy=policy)
    monkeypatch.setattr(settings, "llm_request_timeout_s", settings.llm_request_timeout_s + 1)
    monkeypatch.setattr(settings, "editorial_model_deadline_s", settings.editorial_model_deadline_s + 1)
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 9)
    monkeypatch.setattr(settings, "hotspot_enrichment_llm_max_tokens", 8192)
    with get_session() as db:
        row = db.get(AppSetting, "configuration_revision")
        row.value = str(int(row.value) + 1)
    with get_session() as db:
        current = build_event_evidence_bundle(db, event_id)
    assert current.fingerprint == initial.fingerprint
    assert review_policy_identity() != policy
    assert review_fingerprint(current.source, current.source, current.fingerprint) == fingerprint


@pytest.mark.parametrize("word_time,changed", [(3.0, True), (500.0, False)])
def test_only_selected_transcript_words_change_event_evidence(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word_time: float, changed: bool
) -> None:
    event_id, segment_id = _long_recording(tmp_path)
    _service(monkeypatch, "recommend")
    with get_session() as db:
        original = build_event_evidence_bundle(db, event_id)
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        words = json.loads(transcript.words_json)
        words.append({"w": "实际新增词语", "start": word_time, "end": word_time + 1})
        transcript.words_json = json.dumps(words)
    with get_session() as db:
        current = build_event_evidence_bundle(db, event_id)
    assert (current.fingerprint != original.fingerprint) is changed


@pytest.mark.parametrize("dismiss", [False, True])
@pytest.mark.parametrize("change", ["none", "source", "media", "manual"])
def test_one_unstable_event_preserves_stable_candidate_through_bounded_task_resume(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch, dismiss: bool, change: str
) -> None:
    from app.pipeline import scheduler
    from app.pipeline.claiming import pop_and_claim

    stable_id, segment_id = _seed(pcm_wav)
    lease = _task(segment_id)
    with get_session() as db:
        original = db.get(HotspotEvent, stable_id)
        other = HotspotEvent(**original.model_dump(exclude={"id"}))
        other.event_key = "continually-changing"
        db.add(other)
        db.flush()
        changing_id, session_id = other.id, original.session_id
        task = db.get(SegmentTask, lease.task_id)
        task.attempts, task.max_retries = 1, 2
    requests = _service(monkeypatch, "recommend")
    original_send = httpx.AsyncHTTPTransport.handle_async_request
    stable_requests = 0
    changes = 0

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        nonlocal changes, stable_requests
        response = await original_send(transport, request)
        prompt = json.loads(request.content)["messages"][-1]["content"]
        payload = json.loads(prompt)
        target = payload.get("target", {}).get("target_id")
        if target == changing_id:
            changes += 1
            with get_session() as db:
                db.get(HotspotEvent, changing_id).transcript_text += f" 第{changes}轮新事实。"
        elif target == stable_id:
            stable_requests += 1
        return response

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    budgets = {}
    results = _score_pending_hotspot_events(
        session_id, audio_features=None, audio_segment_id=segment_id, budgets=budgets, lease=lease
    )
    _commit_event_analysis(lease, results, 1, budgets)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        assert task.stage == TaskStatus.TRANSIENT_FAILED and task.attempts == 1
        stable = db.get(HotspotEvent, stable_id)
        saved = stable.features_json
        assert stable.candidate_id == task.candidate_id is not None
        assert len(db.exec(select(HighlightCandidate)).all()) == len(db.exec(select(HighlightEvent)).all()) == 1
        checkpoint = json.loads(task.context_json)["event_first"]["event_analysis_v1"]
        assert checkpoint["remaining_ids"] == [changing_id]
        saved_target = checkpoint["candidates"][0]["target"]
        assert (saved_target["candidate_id"], saved_target["event_id"]) == (task.candidate_id, task.event_id)
        candidate_id, highlight_id = task.candidate_id, task.event_id
        if change == "source":
            stable.transcript_text += " 新的窗口内事实。"
        elif change == "manual":
            db.get(HighlightEvent, highlight_id).review_by = "manual"
        elif change == "media":
            with pcm_wav.open("r+b") as media:
                media.seek(-2, 2)
                media.write(b"\x01\x00")
        if dismiss:
            db.get(HotspotEvent, changing_id).status = HotspotStatus.DISMISSED
    first_count, first_stable = len(requests), stable_requests
    monkeypatch.setattr(scheduler, "now_utc", lambda: datetime.now(UTC) + timedelta(hours=1))
    scheduler.retry_expired()
    claim = pop_and_claim(TaskStatus.QUEUED_FOR_ANALYSIS)
    assert claim is not None
    scheduler.execute_task(claim.id, claim.stage, claim.lease_token)
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        expected = TaskStatus.CANDIDATE_CREATED if change == "none" else TaskStatus.COMPLETED
        assert task.stage == (expected if dismiss else TaskStatus.TRANSIENT_FAILED)
        if dismiss:
            assert json.loads(task.context_json)["event_first"]["event_analysis_v1"] is None
        assert db.get(HotspotEvent, stable_id).features_json == saved
        assert len(db.exec(select(HighlightCandidate)).all()) == 1
        if change != "none":
            assert task.candidate_id is None and task.event_id is None
            assert db.get(HotspotEvent, stable_id).candidate_id == candidate_id
            event = db.get(HighlightEvent, highlight_id)
            if change == "manual":
                assert event.review_by == "manual" and event.review_reason is None
            else:
                assert event.review_status == "pending" and "来源已变化" in event.review_reason
    assert stable_requests == first_stable
    assert len(requests) == first_count if dismiss else len(requests) > first_count
    if not dismiss:
        scheduler.retry_expired()
        with get_session() as db:
            task = db.get(SegmentTask, lease.task_id)
            assert task.stage == TaskStatus.FAILED and task.attempts == 2
            assert task.candidate_id == (candidate_id if change == "none" else None)


def test_empty_admitted_batch_does_not_absorb_newly_confirmed_event(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id, segment_id = _seed(pcm_wav)
    lease = _task(segment_id)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        event.status = HotspotStatus.PROVISIONAL
        session_id = event.session_id
    calls = _service(monkeypatch, "recommend")
    budgets = {}
    results = _score_pending_hotspot_events(
        session_id, audio_features=None, audio_segment_id=segment_id, budgets=budgets, lease=lease
    )
    assert results == [] and budgets == {}
    with get_session() as db:
        db.get(HotspotEvent, event_id).status = HotspotStatus.CONFIRMED
    _commit_event_analysis(lease, results, 1, budgets)
    assert calls == []
    with get_session() as db:
        assert db.get(SegmentTask, lease.task_id).stage == TaskStatus.COMPLETED
        assert db.get(HotspotEvent, event_id).candidate_id is None
