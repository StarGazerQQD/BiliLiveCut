from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlmodel import select

from app.analysis.editorial_contracts import TimeRange
from app.analysis.editorial_review import (
    confirmed_clip_result,
    editorial_auto_approvable,
    judge_editorial,
    review_final_bounds,
)
from app.analysis.editorial_review_sources import automatic_review_is_current
from app.analysis.model_execution import ModelCallBudget
from app.core.config import settings
from app.db.entities import AppSetting, HighlightEvent, HotspotEvent, Transcript
from app.db.session import get_session
from app.pipeline.workers.analyze import score_hotspot_event
from app.web.services.editorial_view import editorial_presentation
from tests.editorial_fixtures import install_editorial_service
from tests.integration.test_editorial_snapshot_stability import _long_recording
from tests.unit.test_clip_scorer import _audio
from tests.unit.test_editorial_contracts import _evidence, _payload, _source
from tests.unit.test_editorial_review import _seed


def _reply(prompt: str, **_kwargs: object) -> str:
    data = json.loads(prompt.rsplit("\n", 1)[1])
    fixed = data["clip_constraints"]["fixed_bounds"]
    return json.dumps(
        _payload(
            evidence_ids=["E1"],
            suggested_bounds=None if fixed else {"start_s": 85.0, "end_s": 140.0, "evidence_ids": ["E1"]},
        )
    )


def test_fixed_review_uses_only_clip_evidence_and_preserves_original_judgement(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(
        evidence=[
            _evidence(start_s=105.0, end_s=115.0),
            _evidence(evidence_id="outside", start_s=70.0, end_s=80.0, context_role="before"),
            _evidence(
                evidence_id="untimed", start_s=None, end_s=None, timing_precision="unknown", context_role="unknown"
            ),
        ]
    )
    calls = install_editorial_service(monkeypatch, _reply)
    budget = ModelCallBudget(max_attempts=3, max_elapsed_s=30)
    draft = judge_editorial(lambda _: source, revision="fixed", budget=budget)
    metadata = review_final_bounds(
        draft, TimeRange(start_s=100.0, end_s=120.0), media_accessible=True, suggestion_applied=False, budget=budget
    )
    assert len(calls) == 2
    initial = json.loads(calls[0]["messages"][1]["content"])
    assert initial["clip_constraints"]["maximum_duration_s"] == settings.clip_max_duration_s
    assert initial["clip_constraints"]["required_peak_s"] == 110.0
    fixed = json.loads(calls[1]["messages"][1]["content"])
    assert fixed["target"]["bounds"] == fixed["window"] == {"start_s": 100.0, "end_s": 120.0}
    assert len(fixed["evidence"]) == 1
    assert fixed["evidence"][0]["start_s"] == 105.0
    assert metadata["execution"] == draft.execution.model_dump(mode="json")
    assert metadata["final_review"]["execution"]["attempt_offset"] == 1
    assert [item["number"] for item in metadata["provenance"]["subsequent_attempts"]] == [2]
    assert editorial_auto_approvable(metadata)
    assert draft.with_final_review(metadata).matches_metadata(metadata)
    for field, value in (("bounds", {"start_s": 101.0, "end_s": 120.0}), ("parent_fingerprint", "f" * 64)):
        changed = copy.deepcopy(metadata)
        changed["final_review"][field] = value
        assert not editorial_auto_approvable(changed)
    changed = copy.deepcopy(metadata)
    changed["final_review"]["input"]["evidence"][0]["excerpts"] = ["篡改后的转写"]
    assert not editorial_auto_approvable(changed)


@pytest.mark.parametrize("failure", ["budget", "media", "truncated", "no_timed_content", "invalid_output", "cancelled"])
def test_unconfirmed_fixed_review_never_reuses_original_success(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    source = _source(evidence=[_evidence(start_s=105.0, end_s=115.0)], truncated=failure == "truncated")
    if failure == "no_timed_content":
        source = _source(evidence=[_evidence()])
    cancelled = False

    def reply(prompt: str, **kwargs: object) -> str:
        if failure == "invalid_output" and json.loads(prompt.rsplit("\n", 1)[1])["clip_constraints"]["fixed_bounds"]:
            return "invalid-json"
        return _reply(prompt, **kwargs)

    calls = install_editorial_service(monkeypatch, reply)
    budget = ModelCallBudget(
        max_attempts=1 if failure == "budget" else 3, max_elapsed_s=30, cancelled=lambda: cancelled
    )
    draft = judge_editorial(lambda _: source, revision="fixed", budget=budget)
    assert draft.execution.result.standalone is True
    cancelled = failure == "cancelled"
    metadata = review_final_bounds(
        draft,
        TimeRange(start_s=100.0, end_s=120.0),
        media_accessible=failure != "media",
        suggestion_applied=False,
        budget=budget,
    )
    assert metadata["clip_standalone"] is None
    assert metadata["final_review"]["execution"]["result"] is None
    assert metadata["final_review"]["execution"]["error"] == {
        "budget": "attempt_budget",
        "cancelled": "cancelled",
        "invalid_output": "invalid_output",
    }.get(failure, "context_unavailable")
    assert len(calls) == (3 if failure == "invalid_output" else 1)
    metadata["clip_standalone"] = True
    assert confirmed_clip_result(metadata) is None


def test_unchanged_suggested_coordinates_do_not_trigger_another_review(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            _payload(
                evidence_ids=["E1"],
                suggested_bounds={"start_s": 85.0, "end_s": 140.0, "evidence_ids": ["E1"]},
                standalone=None,
                boundary_status="uncertain",
            )
        ),
    )
    budget = ModelCallBudget(max_attempts=3, max_elapsed_s=30)
    draft = judge_editorial(lambda _: _source(), revision="same", budget=budget)
    metadata = review_final_bounds(
        draft, TimeRange(start_s=85.0, end_s=140.0), media_accessible=True, suggestion_applied=True, budget=budget
    )
    assert len(calls) == 1 and "final_review" not in metadata


@pytest.mark.parametrize("bounds", [(80.0, 100.0), (90.0, 130.0)])
def test_unusable_suggestions_are_rejected_before_candidate_selection(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, bounds: tuple[float, float]
) -> None:
    monkeypatch.setattr(settings, "clip_max_duration_s", 30)
    calls = install_editorial_service(
        monkeypatch,
        lambda *_args, **_kwargs: json.dumps(
            _payload(
                evidence_ids=["E1"],
                standalone=None,
                boundary_status="uncertain",
                suggested_bounds={"start_s": bounds[0], "end_s": bounds[1], "evidence_ids": ["E1"]},
            )
        ),
    )
    draft = judge_editorial(
        lambda _: _source(), revision="limits", budget=ModelCallBudget(max_attempts=2, max_elapsed_s=30)
    )
    assert draft.execution.result is None and draft.execution.error == "invalid_output"
    assert len(calls) == 2


def test_minimum_config_remains_valid_without_faking_a_usable_suggestion(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "clip_max_duration_s", 5)
    draft = judge_editorial(
        lambda _: _source(), revision="minimum", budget=ModelCallBudget(max_attempts=1, max_elapsed_s=30)
    )
    assert draft.execution.status == "disabled"


def test_fixed_review_does_not_search_even_when_the_route_enables_search(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.analysis.llm_providers import load_providers, save_providers

    calls = install_editorial_service(monkeypatch, _reply)
    send = httpx.AsyncHTTPTransport.handle_async_request

    async def only_fixture(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        if request.url.host == "fixture.invalid":
            return await send(transport, request)
        assert request.url.host == "api.deepseek.com" and request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        calls.append(payload)
        assert json.loads(payload["messages"][1]["content"])["clip_constraints"]["fixed_bounds"] is True
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "fixed",
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": _reply("\n" + payload["messages"][1]["content"])},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", only_fixture)
    budget = ModelCallBudget(max_attempts=3, max_elapsed_s=30)
    draft = judge_editorial(
        lambda _: _source(evidence=[_evidence(start_s=105.0, end_s=115.0)]), revision="search", budget=budget
    )
    routes = load_providers()
    routes[0].base_url = "https://api.deepseek.com/v1"
    routes[0].model = "deepseek-flash"
    routes[0].deepseek_search_enabled = True
    save_providers(routes)
    metadata = review_final_bounds(
        draft, TimeRange(start_s=100.0, end_s=120.0), media_accessible=True, suggestion_applied=False, budget=budget
    )
    assert metadata["clip_standalone"] is True and len(calls) == 2
    assert all(attempt.purpose != "search" for attempt in budget.attempts)


@pytest.mark.parametrize("crosses_clip_boundary", [False, True])
def test_segment_path_persists_fixed_review_with_its_snapshot(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crosses_clip_boundary: bool
) -> None:
    from app.analysis.editorial_evidence import build_segment_input
    from app.analysis.editorial_review_sources import store_segment_reviews
    from app.pipeline.workers.analyze import _score_segment_draft

    _, segment_id = _long_recording(tmp_path)
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        words = [{"w": transcript.final_text, "start": 105.0, "end": 115.0}]
        if crosses_clip_boundary:
            words.insert(0, {"w": "片外前文。", "start": 50.0, "end": 55.0})
            words.insert(1, {"w": "跨越起点。", "start": 79.5, "end": 80.5})
        transcript.words_json = json.dumps(words)

    def reply(prompt: str, **_kwargs: object) -> str:
        data = json.loads(prompt.rsplit("\n", 1)[1])
        fixed = data["clip_constraints"]["fixed_bounds"]
        ids = [item["evidence_id"] for item in data["evidence"] if item["excerpts"]]
        return json.dumps(
            _payload(
                evidence_ids=ids[:1],
                suggested_bounds=None,
                standalone=True if fixed else None,
                boundary_status="complete" if fixed else "uncertain",
            )
        )

    calls = install_editorial_service(monkeypatch, reply)
    result = _score_segment_draft(segment_id, peak_offset_s=100.0, audio_features=_audio(600.0))
    assert result is not None
    metadata = json.loads(result["features_json"])["editorial_review"]
    assert len(calls) == 2 and metadata["clip_standalone"] is True
    fixed_request = calls[1]["messages"][1]["content"]
    assert "片外前文" not in fixed_request and "跨越起点" not in fixed_request
    assert metadata["execution"]["result"]["standalone"] is None
    snapshot = result["editorial_snapshot"]
    assert snapshot.review.matches_metadata(metadata)
    with get_session() as db:
        assert store_segment_reviews(db, segment_id, [snapshot])
    with get_session() as db:
        record = db.get(AppSetting, f"editorial_segment:{segment_id}")
        stored = json.loads(record.value)["windows"][0]["editorial_review"]
    assert stored["final_review"] == metadata["final_review"]
    with get_session() as db:
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).one()
        if crosses_clip_boundary:
            # 词序、正文及初判摘录端点保持一致，仅改变片内可用的词。
            words[1].update(start=81.0, end=82.0)
            transcript.words_json = json.dumps(words)
        else:
            transcript.words_json = json.dumps([{"w": "新的计分规则与说明", "start": 105.0, "end": 115.0}])
    with get_session() as db:
        if crosses_clip_boundary:
            assert (
                build_segment_input(
                    db,
                    segment_id,
                    start_ts=snapshot.start_ts,
                    end_ts=snapshot.end_ts,
                    peak_ts=snapshot.peak_ts,
                    metrics=snapshot.metrics,
                    danmaku=snapshot.danmaku,
                    context_direction=snapshot.review.expanded_context,
                )
                == snapshot.review.source
            )
        assert not store_segment_reviews(db, segment_id, [snapshot])


def test_real_hotspot_path_saves_fixed_review_and_invalidates_adjusted_preview(
    temp_db: None, pcm_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id, _ = _seed(pcm_wav)

    def reply(prompt: str, **_kwargs: object) -> str:
        data = json.loads(prompt.rsplit("\n", 1)[1])
        ids = [item["evidence_id"] for item in data["evidence"] if item["excerpts"]]
        if '"verdict"' not in prompt:
            return json.dumps(
                {
                    "title": "比赛规则",
                    "summary": "说明每轮计分方式",
                    "category": "discussion",
                    "entities": [],
                    "semantic_confidence": 0.8,
                    "evidence_ids": ids[:1],
                }
            )
        fixed = data["clip_constraints"]["fixed_bounds"]
        return json.dumps(
            _payload(
                evidence_ids=ids[:1],
                suggested_bounds=None,
                standalone=True if fixed else None,
                boundary_status="complete" if fixed else "uncertain",
            )
        )

    calls = install_editorial_service(monkeypatch, reply, grounded=True)
    candidate = score_hotspot_event(event_id)
    assert candidate is not None and len(calls) == 4
    with get_session() as db:
        event = db.exec(select(HighlightEvent)).one()
        hotspot = db.get(HotspotEvent, event_id)
        metadata = json.loads(event.features_json)["editorial_review"]
        assert metadata["execution"]["result"]["standalone"] is None
        assert metadata["clip_standalone"] is True
        assert automatic_review_is_current(db, event, candidate)
        assert (
            editorial_presentation(hotspot=hotspot, candidate=candidate, event=event, metadata=metadata).clip_standalone
            is True
        )
        event.adjusted_start_ts = candidate.start_ts + timedelta(seconds=1)
        assert not automatic_review_is_current(db, event, candidate)
        assert (
            editorial_presentation(hotspot=hotspot, candidate=candidate, event=event, metadata=metadata).clip_standalone
            is None
        )
