"""Event-first 检测与 ASR 双向调度行为测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
from sqlmodel import select

from app.analysis.audio import AudioFeatures
from app.db.entities import (
    HotspotEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentTask,
    SessionStatus,
    TaskStatus,
)
from app.db.session import get_session
from app.pipeline.lease import TaskLease

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

_START = datetime(2026, 8, 31, 12, 0, 0)


def _seed_pipeline_task(*, stage: str, context_json: str | None = None) -> tuple[int, int, int]:
    with get_session() as db:
        room = LiveRoom(input_url="https://live.bilibili.com/1", room_id=1, auto_analyze=True)
        db.add(room)
        db.flush()
        assert room.id is not None
        session = RecordingSession(room_id=room.id, status=SessionStatus.RECORDING, started_at=_START)
        db.add(session)
        db.flush()
        assert session.id is not None
        segment = RawSegment(
            session_id=session.id,
            seq=0,
            file_path="segment.ts",
            start_ts=_START,
            end_ts=_START + timedelta(seconds=120),
            duration_s=120.0,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        task = SegmentTask(
            segment_id=segment.id,
            session_id=session.id,
            stage=stage,
            claimed_by="worker-test" if stage in {TaskStatus.ANALYZING, TaskStatus.TRANSCRIBING} else None,
            lease_token="lease-test" if stage in {TaskStatus.ANALYZING, TaskStatus.TRANSCRIBING} else None,
            max_retries=2,
            context_json=context_json,
        )
        db.add(task)
        db.flush()
        assert task.id is not None
        return task.id, segment.id, session.id


def _lease(task_id: int, stage: str) -> TaskLease:
    return TaskLease(
        task_id=task_id,
        worker_id="worker-test",
        lease_token="lease-test",
        expected_stage=stage,
    )


def _hotspot_payload(session_id: int) -> dict[str, object]:
    return {
        "event_key": "detector-v1:event-1",
        "session_id": session_id,
        "start_ts": _START + timedelta(seconds=50),
        "peak_ts": _START + timedelta(seconds=60),
        "end_ts": _START + timedelta(seconds=75),
        "heat_score": 0.9,
        "clip_score": 0.8,
        "semantic_confidence": 0.0,
        "evidence_coverage": 0.7,
        "features_json": "{}",
        "evidence_json": '{"version":1,"items":[]}',
        "transcript_text": None,
    }


def test_event_first_context_update_preserves_other_task_metadata() -> None:
    from app.pipeline.task_context import update_event_first_context

    updated = update_event_first_context(
        '{"reanalysis":{"requested":true},"event_first":{"analysis_pass":"detect"}}',
        asr_mode="hotspot",
    )

    payload = json.loads(updated)
    assert payload["reanalysis"] == {"requested": True}
    assert payload["event_first"] == {
        "analysis_pass": "detect",
        "asr_mode": "hotspot",
    }


def test_signal_pass_persists_hotspot_and_queues_priority_asr(temp_db: None) -> None:
    from app.core.config import settings
    from app.pipeline.workers.analyze import HighlightDecision, commit_highlight

    task_id, segment_id, session_id = _seed_pipeline_task(
        stage=TaskStatus.ANALYZING,
        context_json='{"event_first":{"analysis_pass":"detect"}}',
    )
    commit_highlight(
        _lease(task_id, TaskStatus.ANALYZING),
        {
            "decision": HighlightDecision.SIGNAL_PASS,
            "segment_id": segment_id,
            "session_id": session_id,
            "hotspot_drafts": [_hotspot_payload(session_id)],
        },
        25,
    )

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        event = db.exec(select(HotspotEvent).where(HotspotEvent.session_id == session_id)).one()

    assert event.event_key == "detector-v1:event-1"
    assert task is not None and task.stage == TaskStatus.QUEUED_FOR_TRANS
    assert task.priority == settings.hotspot_asr_priority
    context = json.loads(task.context_json)["event_first"]
    assert context["asr_mode"] == "hotspot"
    assert context["attention_windows"] == [
        {
            "event_key": "detector-v1:event-1",
            "start_offset_s": 25.0,
            "end_offset_s": 115.0,
            "status": "pending",
        }
    ]


def test_signal_pass_without_hotspot_still_queues_background_asr(temp_db: None) -> None:
    from app.core.config import settings
    from app.pipeline.workers.analyze import HighlightDecision, commit_highlight

    task_id, segment_id, session_id = _seed_pipeline_task(
        stage=TaskStatus.ANALYZING,
        context_json='{"event_first":{"analysis_pass":"detect"}}',
    )
    commit_highlight(
        _lease(task_id, TaskStatus.ANALYZING),
        {
            "decision": HighlightDecision.SIGNAL_PASS,
            "segment_id": segment_id,
            "session_id": session_id,
            "hotspot_drafts": [],
        },
        25,
    )

    with get_session() as db:
        task = db.get(SegmentTask, task_id)

    assert task is not None and task.stage == TaskStatus.QUEUED_FOR_TRANS
    assert task.priority == settings.near_live_asr_priority
    context = json.loads(task.context_json)["event_first"]
    assert context["asr_mode"] == "background"
    assert context["asr_evidence_state"] == "pending"
    assert context["attention_windows"] == []


def test_hotspot_asr_compute_runs_window_without_background(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis.transcription import pipeline as pipeline_module
    from app.analysis.transcription.models import ASRTranscriptResult
    from app.pipeline.workers.transcribe import transcribe_compute

    context = json.dumps(
        {
            "event_first": {
                "asr_mode": "hotspot",
                "attention_windows": [
                    {
                        "event_key": "detector-v1:event-1",
                        "start_offset_s": 25.0,
                        "end_offset_s": 115.0,
                    }
                ],
            }
        }
    )
    task_id, _segment_id, _session_id = _seed_pipeline_task(
        stage=TaskStatus.TRANSCRIBING,
        context_json=context,
    )
    calls: list[tuple[str, float, float]] = []

    class FakePipeline:
        def transcribe_window(
            self,
            path: str,
            start_s: float,
            end_s: float,
            initial_prompt: str | None = None,
        ) -> ASRTranscriptResult:
            assert initial_prompt is None
            calls.append((path, start_s, end_s))
            return ASRTranscriptResult(text="热点局部转写", final_text="热点局部转写", backend="funasr-nano")

        def transcribe(self, *_args: object, **_kwargs: object) -> ASRTranscriptResult:
            raise AssertionError("热点局部工作负载不得同步执行后台完整转写")

    monkeypatch.setattr(pipeline_module, "get_task_pipeline", lambda: FakePipeline())
    result = transcribe_compute(task_id)

    assert result["hotspot_asr"] is True
    assert calls == [("segment.ts", 25.0, 115.0)]
    assert result["attention_results"][0]["quality"]["state"] == "available"


def test_hotspot_asr_commit_requeues_background_and_updates_event(temp_db: None) -> None:
    from app.core.config import settings
    from app.pipeline.workers.transcribe import commit_transcript

    context = json.dumps(
        {
            "event_first": {
                "analysis_pass": "candidate",
                "asr_mode": "hotspot",
                "attention_windows": [
                    {
                        "event_key": "detector-v1:event-1",
                        "start_offset_s": 25.0,
                        "end_offset_s": 115.0,
                        "status": "pending",
                    }
                ],
            }
        }
    )
    task_id, segment_id, session_id = _seed_pipeline_task(
        stage=TaskStatus.TRANSCRIBING,
        context_json=context,
    )
    with get_session() as db:
        db.add(HotspotEvent(**_hotspot_payload(session_id)))

    commit_transcript(
        _lease(task_id, TaskStatus.TRANSCRIBING),
        {
            "hotspot_asr": True,
            "segment_id": segment_id,
            "session_id": session_id,
            "attention_results": [
                {
                    "event_key": "detector-v1:event-1",
                    "start_offset_s": 25.0,
                    "end_offset_s": 115.0,
                    "text": "热点局部转写",
                    "quality": {"state": "available", "usable": True, "reason": None},
                    "backend": "funasr-nano",
                }
            ],
        },
        30,
    )

    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        event = db.exec(select(HotspotEvent).where(HotspotEvent.session_id == session_id)).one()

    assert event.transcript_text == "热点局部转写"
    evidence = json.loads(event.evidence_json)
    assert evidence["items"][0]["id"] == "hotspot-asr:detector-v1:event-1"
    assert task is not None and task.stage == TaskStatus.QUEUED_FOR_TRANS
    assert task.priority == settings.near_live_asr_priority
    event_first = json.loads(task.context_json)["event_first"]
    assert event_first["asr_mode"] == "background"
    assert event_first["attention_windows"][0]["status"] == "completed"


def test_background_asr_still_runs_after_hotspot_pass(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis.transcription import pipeline as pipeline_module
    from app.analysis.transcription.models import ASRTranscriptResult
    from app.pipeline.workers.transcribe import transcribe_compute

    context = '{"event_first":{"asr_mode":"background","analysis_pass":"candidate"}}'
    task_id, _segment_id, _session_id = _seed_pipeline_task(
        stage=TaskStatus.TRANSCRIBING,
        context_json=context,
    )
    calls: list[str] = []

    class FakePipeline:
        def transcribe(self, path: str, initial_prompt: str | None = None) -> ASRTranscriptResult:
            assert initial_prompt is None
            calls.append(path)
            return ASRTranscriptResult(text="后台完整转写", final_text="后台完整转写", backend="paraformer")

        def transcribe_window(self, *_args: object, **_kwargs: object) -> ASRTranscriptResult:
            raise AssertionError("后台工作负载不得重复局部 ASR")

    monkeypatch.setattr(pipeline_module, "get_task_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_refine_transcript_for_storage", lambda _text: None)
    result = transcribe_compute(task_id)

    assert result["transcribed"] is True
    assert calls == ["segment.ts"]


def test_asr_retry_exhaustion_continues_to_analysis_as_unavailable(temp_db: None) -> None:
    from app.pipeline.scheduler import advance_transcribed, retry_expired

    task_id, _segment_id, session_id = _seed_pipeline_task(stage=TaskStatus.TRANSIENT_FAILED)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        session = db.get(RecordingSession, session_id)
        assert task is not None
        assert session is not None
        task.failed_stage = TaskStatus.TRANSCRIBING
        task.attempts = task.max_retries
        task.last_error = "RuntimeError: ASR backend unavailable"
        task.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        db.add(task)
        session.status = SessionStatus.STOPPED
        db.add(session)

    retry_expired()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None and task.stage == TaskStatus.TRANSCRIBED
        event_first = json.loads(task.context_json)["event_first"]
        assert event_first["asr_evidence_state"] == "unavailable"
        assert "backend unavailable" in event_first["asr_unavailable_reason"]

    advance_transcribed()
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None and task.stage == TaskStatus.QUEUED_FOR_ANALYSIS


def test_missing_transcript_still_scores_audio_and_danmaku(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis import highlight, llm
    from app.core.config import settings
    from app.pipeline.workers import analyze
    from app.pipeline.workers.analyze import HighlightDecision, _score_segment_draft

    _task_id, segment_id, _session_id = _seed_pipeline_task(stage=TaskStatus.QUEUED_FOR_ANALYSIS)
    audio = AudioFeatures(
        sample_rate=16000,
        hop_s=1.0,
        times=np.asarray([1.0, 2.0]),
        rms=np.asarray([0.2, 1.0]),
        duration_s=120.0,
        silences=[],
    )
    monkeypatch.setattr(analyze.audio_mod, "analyze_audio", lambda _path: audio)
    monkeypatch.setattr(highlight, "_danmaku_score", lambda *_args: 1.0)
    monkeypatch.setattr(highlight, "_is_duplicate", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(highlight, "danmaku_score_explain", lambda *_args: {})
    monkeypatch.setattr(highlight, "weighted_rule_score", lambda *_args: 0.9)
    monkeypatch.setattr(highlight, "fuse_scores", lambda primary, *_args: primary)

    def fail_llm(*_args: object) -> None:
        raise AssertionError("缺失 ASR 时不得调用 LLM")

    monkeypatch.setattr(llm, "judge_highlight", fail_llm)
    monkeypatch.setattr(settings, "highlight_init_threshold", 0.2)

    result = _score_segment_draft(segment_id)

    assert result is not None and result["decision"] == HighlightDecision.CANDIDATE
    metadata = json.loads(result["features_json"])
    assert metadata["asr_evidence"]["state"] == "unavailable"
    assert metadata["asr_evidence"]["reason"] == "missing_transcript"
    assert result["reason"] == "ASR 语义证据不可用，依据音频与互动信号命中"


def test_transcription_claim_prefers_hotspot_priority(temp_db: None) -> None:
    from app.pipeline.claiming import pop_and_claim

    background_id, _segment_id, _session_id = _seed_pipeline_task(stage=TaskStatus.QUEUED_FOR_TRANS)
    hotspot_id, _segment_id, _session_id = _seed_pipeline_task(stage=TaskStatus.QUEUED_FOR_TRANS)
    with get_session() as db:
        background = db.get(SegmentTask, background_id)
        hotspot = db.get(SegmentTask, hotspot_id)
        assert background is not None and hotspot is not None
        background.priority = 100
        hotspot.priority = 10
        db.add(background)
        db.add(hotspot)

    claimed = pop_and_claim(TaskStatus.QUEUED_FOR_TRANS)

    assert claimed is not None and claimed.id == hotspot_id
