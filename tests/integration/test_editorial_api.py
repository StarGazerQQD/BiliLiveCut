from __future__ import annotations

import json
import time
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.entities import AppSetting, HighlightCandidate, HighlightEvent, HotspotEvent, RecordingSession, ReviewStatus
from app.db.session import get_session
from app.pipeline.workers.analyze import score_hotspot_event, score_segment_direct
from app.web.services.editorial_view import editorial_presentation
from tests.integration.test_review_workflow import review_client as review_client
from tests.unit.test_editorial_review import START, _seed, _service

if TYPE_CHECKING:
    from pytest import MonkeyPatch

ADMIN = ("admin", "admin-pass")
REVIEWER = ("alice", "alice-pass")


def test_failed_model_diagnostic_survives_real_pipeline_and_timeline_api(
    review_client: TestClient, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    hotspot_id, _ = _seed(pcm_wav, heat=0.95)
    _service(monkeypatch, "recommend")

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"code": "server_error", "message": "private model request must not be exposed"}},
            headers={"x-request-id": "editorial-request-123"},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    score_hotspot_event(hotspot_id)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        assert hotspot is not None
        session_id = hotspot.session_id
        stored = json.loads(hotspot.features_json)["editorial_review"]["execution"]
    with nullcontext(review_client) as client:
        result = client.get(f"/api/sessions/{session_id}/timeline?include_rejected=true", auth=ADMIN)
    assert result.status_code == 200
    execution = result.json()["points"][0]["editorial"]["execution"]
    assert execution == stored and execution["status"] == "failed" and execution["result"] is None
    diagnostic = execution["attempts"][0]["diagnostic"]
    assert diagnostic["http_status"] == 500 and diagnostic["request_id"] == "editorial-request-123"
    assert "private model request" not in result.text


def test_shared_budget_failure_keeps_prior_calls_in_read_only_presentation(
    review_client: TestClient, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    from app.core.config import settings
    from tests.editorial_fixtures import install_editorial_service

    hotspot_id, _ = _seed(pcm_wav, heat=0.95)
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 3)
    calls = install_editorial_service(monkeypatch, lambda *_args, **_kwargs: "{}")
    score_hotspot_event(hotspot_id)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        session_id = hotspot.session_id
        original = json.loads(hotspot.features_json)["editorial_review"]
    assert len(calls) == 3
    # 复现提交重算已耗尽共享预算的历史记录，保留真实三次失败的来源。
    from app.analysis.editorial_contracts import ModelAttempt
    from app.analysis.editorial_review_sources import compute_event_review
    from app.analysis.model_execution import ModelCallBudget

    prior = original["provenance"]["prior_attempts"] + original["execution"]["attempts"]
    budget = ModelCallBudget(max_attempts=3, max_elapsed_s=30)
    for item in prior:
        budget.record(ModelAttempt.model_validate(item))
    draft = compute_event_review(hotspot_id, budget)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        features = json.loads(hotspot.features_json)
        features["editorial_review"] = draft.metadata()
        hotspot.features_json = json.dumps(features)
    response = review_client.get(f"/api/sessions/{session_id}/timeline?include_rejected=true", auth=ADMIN)
    assert response.status_code == 200
    view = response.json()["points"][0]["editorial"]
    assert view["execution"]["error"] == "attempt_budget" and view["execution"]["attempts"] == []
    assert view["execution"]["attempt_offset"] == len(view["previous_attempts"]) == 3
    assert [item["purpose"] for item in view["previous_attempts"]] == ["explanation", "repair", "review"]
    assert all(item["diagnostic"]["category"] == "validation" for item in view["previous_attempts"])
    assert len(calls) == 3


@pytest.mark.parametrize("change", ["number", "policy", "fingerprint", "missing_initial"])
def test_damaged_prior_attempt_provenance_is_not_projected(
    temp_db: None, pcm_wav: Path, monkeypatch: MonkeyPatch, change: str
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    score_hotspot_event(hotspot_id)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
    metadata = json.loads(hotspot.features_json)["editorial_review"]
    if change == "number":
        metadata["provenance"]["prior_attempts"][0]["number"] = 2
    elif change == "policy":
        metadata["provenance"]["policy"]["private-secret"] = "private-secret"
    elif change == "fingerprint":
        metadata["execution"]["input_fingerprint"] = "0" * 64
    else:
        del metadata["provenance"]["initial_input"]
    view = editorial_presentation(hotspot=hotspot, metadata=metadata)
    assert view.editorial_state == "invalid" and view.execution is None and view.previous_attempts == []
    assert "private-secret" not in view.model_dump_json()


@pytest.mark.parametrize("with_provenance", [False, True])
def test_legacy_zero_offset_and_unknown_initial_input_remain_readable(temp_db: None, with_provenance: bool) -> None:
    from app.analysis.editorial_review import judge_editorial
    from app.analysis.model_execution import ModelCallBudget

    draft = judge_editorial(
        lambda _direction: None, revision="legacy", budget=ModelCallBudget(max_attempts=3, max_elapsed_s=30)
    )
    metadata = draft.metadata()
    del metadata["execution"]["attempt_offset"]
    if not with_provenance:
        del metadata["provenance"]
    view = editorial_presentation(metadata=metadata)
    assert view.editorial_state == "failed" and view.execution.error == "context_unavailable"
    assert view.previous_attempts == [] and view.execution.attempt_offset == 0


@pytest.mark.parametrize("version", ["editorial-decision-v1", "editorial-decision-v2", "unsupported"])
def test_frozen_review_policy_uses_its_own_version_without_live_defaults(
    temp_db: None, pcm_wav: Path, monkeypatch: MonkeyPatch, tmp_path: Path, version: str
) -> None:
    from app.analysis.editorial_evaluation_io import export_snapshot
    from app.analysis.editorial_provenance import canonical_hash
    from app.core.config import settings

    hotspot_id, _ = _seed(pcm_wav)
    calls = _service(monkeypatch, "recommend")
    score_hotspot_event(hotspot_id)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        features = json.loads(hotspot.features_json)
        metadata = features["editorial_review"]
        provenance = metadata["provenance"]
        provenance["policy"]["policy"] = version
        for route in provenance["policy"]["routes"]:
            route.pop("endpoint_sha256")
            route.pop("capacity")
        metadata["execution"]["policy_version"] = version
        metadata["execution"]["input_fingerprint"] = canonical_hash(
            {
                "initial": provenance["initial_input"],
                "source": metadata["input"],
                "revision": metadata["revision"],
                "policy": provenance["policy"],
            }
        )
        hotspot.features_json = json.dumps(features)
        session_id = hotspot.session_id
    monkeypatch.setattr(settings, "editorial_model_max_attempts", 5)
    view = editorial_presentation(hotspot=hotspot)
    if version == "unsupported":
        assert view.editorial_state == "invalid" and view.execution is None
        with pytest.raises(ValueError, match="策略版本"):
            export_snapshot(tmp_path / "test.db", dataset_id="frozen-policy", session_ids=[session_id])
    else:
        assert view.editorial_state == "succeeded" and view.execution.result.verdict == "recommend"
        snapshot = export_snapshot(tmp_path / "test.db", dataset_id="frozen-policy", session_ids=[session_id])
        assert snapshot.records[0].provenance.policy == provenance["policy"]
    assert len(calls) == 3


def test_timeline_and_review_share_persisted_evidence_without_model_calls(
    review_client: TestClient, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    calls = _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(hotspot_id)
    assert candidate is not None
    with get_session() as db:
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate.id)).one()
        event.adjusted_start_ts = START + timedelta(seconds=1)
        event.adjusted_end_ts = START + timedelta(seconds=7)
        event_id = event.id
        session = db.get(RecordingSession, candidate.session_id)
        assert session is not None
        session.started_at = START + timedelta(seconds=3)
        session.ended_at = START + timedelta(seconds=9)
        session.status = "stopped"
        settings_before = [(row.key, row.value) for row in db.exec(select(AppSetting)).all()]
    previous_calls = len(calls)
    # 单独测试 HTTP 读取；启动时的恢复调度由其他集成测试覆盖。
    with nullcontext(review_client) as client:
        timeline = client.get(f"/api/sessions/{candidate.session_id}/timeline", auth=ADMIN)
        review = client.get(f"/review/api/{candidate.id}", auth=ADMIN)
        assert timeline.status_code == review.status_code == 200
        point = timeline.json()["points"][0]
        view = point["editorial"]
        assert review.json()["editorial"] == view
        assert (view["hotspot_event_id"], view["candidate_id"], view["event_id"]) == (
            hotspot_id,
            candidate.id,
            event_id,
        )
        assert view["title"] == "主播解释比赛规则"
        assert view["explanation_state"] == "available"
        assert view["execution"]["result"]["verdict"] == "recommend"
        assert view["evidence"] and view["execution"]["attempts"]
        assert view["technical_score"] == candidate.highlight_score
        assert point["provenance"]["llm_score"] is None
        assert review.json()["candidate"]["llm_score"] is None
        assert point["hotspot_duration_s"] == 4 and point["clip_duration_s"] == 6
        assert point["offset_s"] == 4
        assert view["time_origin_epoch_s"] == timeline.json()["session"]["time_origin_epoch_s"]
        assert client.get(f"/api/sessions/{candidate.session_id}/timeline", auth=ADMIN).json() == timeline.json()
    assert len(calls) == previous_calls
    with get_session() as db:
        assert [(row.key, row.value) for row in db.exec(select(AppSetting)).all()] == settings_before


@pytest.mark.parametrize("legacy", [False, True])
def test_rejected_results_remain_searchable_without_fabricated_candidate_ids(
    review_client: TestClient, pcm_wav: Path, monkeypatch: MonkeyPatch, legacy: bool
) -> None:
    hotspot_id, segment_id = _seed(pcm_wav)
    calls = _service(monkeypatch, "reject")
    candidate = score_segment_direct(segment_id) if legacy else score_hotspot_event(hotspot_id)
    assert candidate is None
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        assert hotspot is not None
        session_id = hotspot.session_id
        if legacy:
            db.delete(hotspot)
    before = len(calls)
    with review_client as client:
        path = f"/api/sessions/{session_id}/timeline"
        assert client.get(path, auth=ADMIN).json()["points"] == []
        expanded = client.get(path + "?include_rejected=true", auth=ADMIN)
        assert expanded.status_code == 200
        points = expanded.json()["points"]
        assert points and all(point["rejected"] for point in points)
        overview = client.get(f"/api/sessions/timeline?session_id={session_id}", auth=ADMIN).json()[0]
        assert overview["timeline_count"] == len(points)
        assert overview["rejected_count"] == len(points)
        for point in points:
            assert point["candidate_id"] is None and point["event_id"] is None
            assert point["review_url"] is None and point["clip_duration_s"] is None
            assert point["editorial"]["execution"]["result"]["verdict"] == "reject"
    assert len(calls) == before


@pytest.mark.parametrize("status,actor", [(ReviewStatus.PENDING, ""), (ReviewStatus.REJECTED, "auto")])
def test_blind_review_hides_new_editorial_fields_and_adjacent_machine_advice(
    review_client: TestClient, pcm_wav: Path, monkeypatch: MonkeyPatch, status: str, actor: str
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(hotspot_id)
    assert candidate is not None
    with get_session() as db:
        event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate.id)).one()
        event.review_status, event.review_by = status, actor
        adjacent = HighlightCandidate(
            session_id=candidate.session_id,
            peak_ts=START + timedelta(seconds=10),
            start_ts=START + timedelta(seconds=9),
            end_ts=START + timedelta(seconds=12),
            dedup_hash="blind-adjacent",
            reason="不能泄露的机器建议",
            highlight_score=0.98,
        )
        db.add(adjacent)
        db.flush()
        db.add(HighlightEvent(candidate_id=adjacent.id, session_id=candidate.session_id))
    with review_client as client:
        body = client.get(f"/review/api/{candidate.id}", auth=REVIEWER).json()
        assert body["viewer"]["blinded"] is True
        assert body["editorial"] is None and body["features"] == {}
        assert body["candidate"]["llm_score"] is None
        assert "不能泄露的机器建议" not in json.dumps(body, ensure_ascii=False)
        queue = client.get("/review/api/queue?status=all", auth=REVIEWER).json()
        own = next(item for item in queue["items"] if item["id"] == candidate.id)
        assert own["score"] is None and own["reason"] is None


def test_preview_is_read_only_and_submission_revalidates_permissions_and_fingerprint(
    review_client: TestClient, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    calls = _service(monkeypatch, "recommend")
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        assert hotspot is not None
        session_id = hotspot.session_id
    path = f"/api/sessions/{session_id}/editorial-reanalysis"
    selection = {"session_id": session_id}
    with review_client as client:
        assert client.post(path + "/preview", json=selection).status_code == 401
        assert client.post(path + "/preview", json=selection, auth=REVIEWER).status_code == 403
        assert (
            client.post(path, json={"selection": selection, "fingerprint": "0" * 64}, auth=REVIEWER).status_code == 403
        )
        assert client.post(path + "/preview", json={"session_id": session_id + 1}, auth=ADMIN).status_code == 422
        assert client.post(path + "/preview", json={**selection, "limit": 101}, auth=ADMIN).status_code == 422
        assert client.post(path + "/preview", json={**selection, "targets": []}, auth=ADMIN).status_code == 422
        response = client.post(path + "/preview", json=selection, auth=ADMIN)
        assert response.status_code == 200
        preview = response.json()
        assert preview["total"] == 1 and preview["targets"][0]["source_id"] == hotspot_id
        assert client.post(path + "/preview", json=selection, auth=ADMIN).json() == preview
        assert not calls and client.get("/api/jobs", auth=ADMIN).json()["jobs"] == []
        with get_session() as db:
            hotspot = db.get(HotspotEvent, hotspot_id)
            assert hotspot is not None
            hotspot.transcript_text += "内容已变化"
        stale = client.post(path, json={"selection": selection, "fingerprint": preview["fingerprint"]}, auth=ADMIN)
        assert stale.status_code == 409 and "预览已过期" in stale.json()["detail"]
        assert not calls and client.get("/api/jobs", auth=ADMIN).json()["jobs"] == []


def test_explicit_reanalysis_job_runs_real_pipeline_and_restores_result_via_http(
    review_client: TestClient, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    calls = _service(monkeypatch, "recommend")
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        assert hotspot is not None
        session_id = hotspot.session_id
    path = f"/api/sessions/{session_id}/editorial-reanalysis"
    selection = {"session_id": session_id}
    with review_client as client:
        preview = client.post(path + "/preview", json=selection, auth=ADMIN).json()
        queued = client.post(path, json={"selection": selection, "fingerprint": preview["fingerprint"]}, auth=ADMIN)
        assert queued.status_code == 202
        status_url = queued.json()["status_url"]
        deadline = time.monotonic() + 10
        while True:
            response = client.get(status_url, auth=ADMIN)
            assert response.status_code == 200
            job = response.json()
            if job["status"] not in {"queued", "running", "cancelling"} or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert job["status"] == "succeeded", job
        assert job["result"]["updated"] == 1 and job["result"]["skipped"] == 0
        assert len(calls) == 3
        assert client.get(status_url, auth=REVIEWER).status_code == 403
        point = client.get(f"/api/sessions/{session_id}/timeline", auth=ADMIN).json()["points"][0]
        assert point["editorial"]["execution"]["result"]["verdict"] == "recommend"
        assert point["candidate_id"] == job["result"]["targets"][0]["candidate_id"]
    with get_session() as db:
        assert db.get(HotspotEvent, hotspot_id) is not None


@pytest.mark.parametrize("damage", ["direction", "evidence", "execution", "identity"])
def test_invalid_persisted_judgment_never_becomes_a_trusted_view(
    temp_db: None, pcm_wav: Path, monkeypatch: MonkeyPatch, damage: str
) -> None:
    hotspot_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    score_hotspot_event(hotspot_id)
    with get_session() as db:
        hotspot = db.get(HotspotEvent, hotspot_id)
        assert hotspot is not None
        features = json.loads(hotspot.features_json)
        metadata = features["editorial_review"]
        if damage == "direction":
            metadata["expanded_context"] = ["before"]
        elif damage == "evidence":
            metadata["execution"]["result"]["evidence_ids"] = ["nonexistent"]
        elif damage == "identity":
            metadata["input"]["target"]["target_id"] += 1
        else:
            metadata["execution"]["unexpected"] = "private-value"
        features["event_enrichment"]["error"] = "legacy-private-error"
        hotspot.features_json = json.dumps(features)
        view = editorial_presentation(hotspot=hotspot)
    assert view.editorial_state == "invalid"
    assert view.execution is None and view.evidence == []
    assert view.explanation_error == "invalid_output"
    assert "private" not in view.model_dump_json()


def test_projection_keeps_distinct_domain_ids() -> None:
    hotspot = HotspotEvent(id=81, session_id=7, event_key="id-check", start_ts=START, peak_ts=START, end_ts=START)
    candidate = HighlightCandidate(
        id=22, session_id=7, dedup_hash="id-check", start_ts=START, peak_ts=START, end_ts=START
    )
    event = HighlightEvent(id=17, session_id=7, candidate_id=22)
    view = editorial_presentation(hotspot=hotspot, candidate=candidate, event=event)
    assert (view.hotspot_event_id, view.candidate_id, view.event_id) == (81, 22, 17)


def test_human_approval_keeps_machine_rejection_visible_without_hiding_the_candidate(
    temp_db: None, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    from app.analysis.editorial_reanalysis import commit_reanalysis, compute_reanalysis
    from app.analysis.reanalysis_targets import list_reanalysis_targets
    from app.pipeline.approval import approve_event_and_task
    from app.web.services.timeline import get_session_timeline, list_session_timelines

    hotspot_id, _ = _seed(pcm_wav)
    _service(monkeypatch, "recommend")
    candidate = score_hotspot_event(hotspot_id)
    assert candidate is not None
    _service(monkeypatch, "reject")
    with get_session() as db:
        targets = list_reanalysis_targets(db, candidate.session_id)
    drafts = compute_reanalysis(targets, cancelled=lambda: False)
    with get_session() as db:
        assert commit_reanalysis(db, drafts)["updated"] == 1
        event_id = db.exec(select(HighlightEvent.id).where(HighlightEvent.candidate_id == candidate.id)).one()
    assert get_session_timeline(candidate.session_id, include_summary=False)["points"] == []
    assert approve_event_and_task(task_id=None, event_id=event_id, source="human", approved_by="reviewer")
    timeline = get_session_timeline(candidate.session_id, include_summary=False)
    assert len(timeline["points"]) == 1
    point = timeline["points"][0]
    assert point["candidate_id"] == candidate.id
    assert point["review_status"] == ReviewStatus.APPROVED_SOLO
    assert point["rejected"] is False
    assert point["editorial"]["execution"]["result"]["verdict"] == "reject"
    assert list_session_timelines(session_id=candidate.session_id)[0]["rejected_count"] == 0
