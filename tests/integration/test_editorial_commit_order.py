from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlmodel import select

from app.db.entities import AppSetting, HighlightCandidate, HotspotEvent, RawSegment, SegmentTask, TaskStatus
from app.db.session import get_session
from app.pipeline.lease import TaskLease
from app.pipeline.workers.analyze import _commit_event_analysis, _compute_event_clip_result, _reusable_event_result
from tests.unit.test_editorial_review import _seed, _service

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _task(segment_id: int) -> TaskLease:
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        task = SegmentTask(
            segment_id=segment_id,
            session_id=segment.session_id,
            stage=TaskStatus.ANALYZING,
            claimed_by="commit-worker",
            lease_token="commit-token",
            context_json='{"event_first":{"analysis_pass":"candidate"}}',
        )
        db.add(task)
        db.flush()
        return TaskLease(
            task_id=task.id, worker_id="commit-worker", lease_token="commit-token", expected_stage=TaskStatus.ANALYZING
        )


def test_batch_retry_reuses_unchanged_success_and_keeps_changed_event_shared_budget(
    temp_db: None, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    first_id, segment_id = _seed(pcm_wav)
    with get_session() as db:
        first = db.get(HotspotEvent, first_id)
        second = HotspotEvent(**first.model_dump(exclude={"id"}))
        second.event_key = "editorial:second"
        db.add(second)
        db.flush()
        second_id = second.id
    lease = _task(segment_id)
    requests = _service(monkeypatch, "recommend")
    first = _compute_event_clip_result(first_id)
    second = _compute_event_clip_result(second_id)
    assert first is not None and second is not None and len(requests) == 6
    with get_session() as db:
        event = db.get(HotspotEvent, second_id)
        event.transcript_text += " 新增真实测试输入。"
    _commit_event_analysis(lease, [first, second], 10)
    assert len(requests) == 9
    with get_session() as db:
        stable = json.loads(db.get(HotspotEvent, first_id).features_json)["editorial_review"]
        changed = json.loads(db.get(HotspotEvent, second_id).features_json)["editorial_review"]
        assert db.get(SegmentTask, lease.task_id).stage == TaskStatus.CANDIDATE_CREATED
    assert stable["execution"] == first["review"].execution.model_dump(mode="json")
    assert stable["execution"]["status"] == "succeeded"
    assert changed["execution"]["status"] == "succeeded"
    assert changed["provenance"]["operation_id"] == second["model_budget"].operation_id
    assert [item["number"] for item in changed["provenance"]["prior_attempts"]] == [1, 2, 3, 4, 5]
    assert [item["number"] for item in changed["execution"]["attempts"]] == [6]


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_changed_media_rejects_cached_clip_before_reuse_and_commit(
    temp_db: None, pcm_wav: Path, tmp_path: Path, monkeypatch: MonkeyPatch, change: str
) -> None:
    from app.analysis.clip_scorer import validate_event_clip_draft

    event_id, segment_id = _seed(pcm_wav)
    lease = _task(segment_id)
    calls = _service(monkeypatch, "recommend")
    original = _compute_event_clip_result(event_id)
    assert original is not None and len(calls) == 3
    assert json.loads(original["clip_draft"].features_json)["editorial_review"]["media_accessible"] is True
    if change == "delete":
        pcm_wav.unlink()
    else:
        # 同大小、同 mtime 的替换仍须由文件身份识别。
        before = pcm_wav.stat()
        replacement = tmp_path / "replacement.wav"
        replacement.write_bytes(pcm_wav.read_bytes())
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(pcm_wav)
    assert _reusable_event_result(original, event_id, lease) is None
    with get_session() as db:
        assert (
            validate_event_clip_draft(
                db, original["clip_draft"], enrichment=original["enrichment"], review=original["review"]
            )
            is None
        )
        assert not db.exec(select(HighlightCandidate)).all()
    assert len(calls) == 3


def test_candidate_commit_persists_new_facts_before_model_without_holding_write_transaction(
    temp_db: None, monkeypatch: MonkeyPatch
) -> None:
    from app.analysis.hotspot_detector import HotspotDetector, HotspotDetectorConfig, SignalBucket
    from app.pipeline.workers.analyze import HighlightDecision, commit_highlight
    from tests.editorial_fixtures import install_editorial_service
    from tests.unit.pipeline.test_event_first_clip_scoring import _START, _lease, _seed_candidate_pass

    task_id, event_id, session_id = _seed_candidate_pass(strong=True)
    buckets = [
        SignalBucket(
            start_ts=_START + timedelta(seconds=index * 10),
            end_ts=_START + timedelta(seconds=(index + 1) * 10),
            danmaku_count=80 if index in (11, 12) else 10,
            danmaku_unique_users=60 if index in (11, 12) else 8,
            danmaku_intensity=0.9 if index in (11, 12) else 0.05,
            danmaku_repetition=0.35 if index in (11, 12) else 0.1,
            asr_text="本轮新增转写事实" if index in (11, 12) else None,
            representative_messages=("观众正在讨论本轮规则",),
        )
        for index in range(20)
    ]
    drafts = HotspotDetector(HotspotDetectorConfig(min_baseline_buckets=3)).detect(session_id, buckets)
    assert len(drafts) == 1
    with get_session() as db:
        segment_id = db.get(SegmentTask, task_id).segment_id

    def respond(_prompt: str, **_kwargs: object) -> None:
        with get_session() as db:
            db.connection().exec_driver_sql("PRAGMA busy_timeout=100")
            event = db.get(HotspotEvent, event_id)
            assert "本轮新增转写事实" in event.transcript_text
            db.merge(AppSetting(key="external-write-during-model", value="ok"))
        time.sleep(0.03)

    calls = install_editorial_service(monkeypatch, respond)
    commit_highlight(
        _lease(task_id),
        {
            "decision": HighlightDecision.EVENT_PASS,
            "segment_id": segment_id,
            "session_id": session_id,
            "hotspot_drafts": [item.to_payload() for item in drafts],
        },
        1,
    )
    assert len(calls) == 3
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task.stage == TaskStatus.CANDIDATE_CREATED and task.processing_time_ms >= 60
        metadata = json.loads(db.get(HotspotEvent, event_id).features_json)["editorial_review"]
    assert metadata["execution"]["error"] == "provider_error"
    assert [item["number"] for item in metadata["provenance"]["prior_attempts"]] == [1, 2]
    assert [item["number"] for item in metadata["execution"]["attempts"]] == [3]


def test_replaced_lease_before_candidate_commit_sends_no_model_requests(
    temp_db: None, pcm_wav: Path, monkeypatch: MonkeyPatch
) -> None:
    from app.pipeline.workers.analyze import analyze_compute, commit_highlight

    _event_id, segment_id = _seed(pcm_wav)
    lease = _task(segment_id)
    calls = _service(monkeypatch, "recommend")
    computed = analyze_compute(lease.task_id)
    assert not calls
    with get_session() as db:
        db.get(SegmentTask, lease.task_id).lease_token = "new-owner"
    commit_highlight(lease, computed, 1)
    assert not calls
    with get_session() as db:
        assert not db.exec(select(HighlightCandidate)).all()
