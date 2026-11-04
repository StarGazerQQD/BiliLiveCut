"""Event-first 检测与 ASR 双向调度行为测试。"""

from __future__ import annotations

import json
import wave
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import numpy as np
import pytest
from sqlmodel import select

from app.analysis.audio import AudioFeatures
from app.core.paths import resolve_storage_path
from app.db.entities import (
    HotspotEvent,
    HotspotStatus,
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
    from pathlib import Path

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


def test_empty_detection_pass_confirms_stable_prior_hotspot(temp_db: None) -> None:
    """后续分段没有新热点时，也必须推进已稳定事件而非永久 provisional。"""
    from app.pipeline.workers.analyze import HighlightDecision, commit_highlight

    task_id, segment_id, session_id = _seed_pipeline_task(
        stage=TaskStatus.ANALYZING,
        context_json='{"event_first":{"analysis_pass":"detect"}}',
    )
    payload = _hotspot_payload(session_id)
    payload.update(
        {
            "event_key": "detector-v1:stable-prior",
            "start_ts": _START + timedelta(seconds=5),
            "peak_ts": _START + timedelta(seconds=15),
            "end_ts": _START + timedelta(seconds=30),
        }
    )
    with get_session() as db:
        db.add(HotspotEvent(**payload))

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
        event = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == "detector-v1:stable-prior")).one()

    assert event.status == HotspotStatus.CONFIRMED


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
    assert calls == [(str(resolve_storage_path("segment.ts")), 25.0, 115.0)]
    assert result["attention_results"][0]["quality"]["state"] == "available"


@pytest.mark.parametrize("segment_offset", [0, 300])
def test_hotspot_asr_commit_requeues_background_and_updates_event(temp_db: None, segment_offset: int) -> None:
    from app.analysis.event_enricher import build_event_evidence_bundle
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
        segment = db.get(RawSegment, segment_id)
        segment.seq = segment_offset // 300
        segment.start_ts += timedelta(seconds=segment_offset)
        segment.end_ts += timedelta(seconds=segment_offset)
        hotspot = _hotspot_payload(session_id)
        for key in ("start_ts", "peak_ts", "end_ts"):
            hotspot[key] += timedelta(seconds=segment_offset)
        db.add(HotspotEvent(**hotspot))

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
        source = build_event_evidence_bundle(db, event.id).source

    assert event.transcript_text == "热点局部转写"
    evidence = json.loads(event.evidence_json)
    assert evidence["items"][0]["id"] == "hotspot-asr:detector-v1:event-1"
    local = evidence["items"][0]
    assert local["detail"]["segment_id"] == segment_id
    assert datetime.fromisoformat(local["start_ts"]) == _START.replace(tzinfo=UTC) + timedelta(
        seconds=segment_offset + 25
    )
    assert source is not None
    projected = next(item for item in source.evidence if item.evidence_id == local["id"])
    assert (projected.start_s, projected.end_s, projected.context_role) == (
        segment_offset + 25,
        segment_offset + 115,
        "mixed",
    )
    assert projected.detail.segment_id == segment_id
    assert task is not None and task.stage == TaskStatus.QUEUED_FOR_TRANS
    assert task.priority == settings.near_live_asr_priority
    event_first = json.loads(task.context_json)["event_first"]
    assert event_first["asr_mode"] == "background"
    assert event_first["attention_windows"][0]["status"] == "completed"


def test_hotspot_asr_for_merged_alias_updates_canonical_event(temp_db: None) -> None:
    """跨分段合并后，已排队的旧 event_key 仍应把 ASR 写入主事件。"""
    from app.pipeline.workers.transcribe import commit_transcript

    alias_key = "detector-v1:event-alias"
    context = json.dumps(
        {
            "event_first": {
                "analysis_pass": "candidate",
                "asr_mode": "hotspot",
                "attention_windows": [
                    {
                        "event_key": alias_key,
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
        canonical = HotspotEvent(**_hotspot_payload(session_id))
        db.add(canonical)
        db.flush()
        assert canonical.id is not None
        alias_payload = _hotspot_payload(session_id)
        alias_payload["event_key"] = alias_key
        alias_payload["status"] = HotspotStatus.MERGED
        alias_payload["merged_into_id"] = canonical.id
        db.add(HotspotEvent(**alias_payload))

    commit_transcript(
        _lease(task_id, TaskStatus.TRANSCRIBING),
        {
            "hotspot_asr": True,
            "segment_id": segment_id,
            "session_id": session_id,
            "attention_results": [
                {
                    "event_key": alias_key,
                    "start_offset_s": 25.0,
                    "end_offset_s": 115.0,
                    "text": "跨分段主事件转写",
                    "quality": {"state": "available", "usable": True, "reason": None},
                    "backend": "funasr-nano",
                }
            ],
        },
        30,
    )

    with get_session() as db:
        canonical = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == "detector-v1:event-1")).one()
        alias = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == alias_key)).one()

    assert canonical.transcript_text == "跨分段主事件转写"
    assert alias.status == HotspotStatus.MERGED
    assert alias.transcript_text is None
    evidence = json.loads(canonical.evidence_json or "{}")
    assert evidence["items"][0]["id"] == f"hotspot-asr:{alias_key}"
    assert evidence["items"][0]["detail"]["segment_id"] == segment_id
    assert datetime.fromisoformat(evidence["items"][0]["start_ts"]) == _START.replace(tzinfo=UTC) + timedelta(
        seconds=25
    )


@pytest.mark.parametrize(
    "start,end", [(True, 10), ("1", 10), (0, float("inf")), (-1, 10), (10, 10), (20, 10), (0, 121)]
)
def test_hotspot_asr_invalid_window_is_rejected(start: object, end: object) -> None:
    from app.pipeline.workers.transcribe import _merge_attention_evidence

    segment = RawSegment(
        id=1, session_id=1, seq=0, file_path="x.ts", start_ts=_START, end_ts=_START + timedelta(seconds=120)
    )
    with pytest.raises(ValueError, match="局部 ASR 时间"):
        _merge_attention_evidence(
            None, "event", {"start_offset_s": start, "end_offset_s": end}, "available", segment=segment
        )


@pytest.mark.parametrize("known_start", [False, True])
def test_hotspot_asr_time_uses_actual_segment_timezone_or_remains_unknown(known_start: bool) -> None:
    from app.pipeline.workers.transcribe import _merge_attention_evidence

    start = _START.replace(tzinfo=UTC).astimezone(timezone(timedelta(hours=8))) if known_start else None
    segment = RawSegment(id=7, session_id=1, seq=3, file_path="x.ts", start_ts=start)
    payload = json.loads(
        _merge_attention_evidence(
            None, "event", {"start_offset_s": 25, "end_offset_s": 115}, "available", segment=segment
        )
    )
    item = payload["items"][0]
    assert item["detail"]["segment_id"] == 7
    assert item["start_ts"] == (
        (_START.replace(tzinfo=UTC) + timedelta(seconds=25)).isoformat() if known_start else None
    )
    assert item["end_ts"] == (
        (_START.replace(tzinfo=UTC) + timedelta(seconds=115)).isoformat() if known_start else None
    )


def test_attention_window_preserves_timezone_and_exact_segment_end() -> None:
    from app.pipeline.workers.analyze import _build_hotspot_attention_windows

    segment = RawSegment(
        id=1, session_id=1, seq=0, file_path="x.ts", start_ts=_START, end_ts=_START + timedelta(seconds=120.0006)
    )
    peak = (_START.replace(tzinfo=UTC) + timedelta(seconds=110)).astimezone(timezone(timedelta(hours=8)))
    windows = _build_hotspot_attention_windows(segment, [{"event_key": "event", "peak_ts": peak}])
    assert len(windows) == 1
    assert 0 <= windows[0]["start_offset_s"] < 110
    assert windows[0]["end_offset_s"] == 120.0006


@pytest.mark.parametrize("stored_end", [120.001, 120.002])
def test_persisted_rounded_attention_end_recovers_before_real_asr(
    temp_db: None, tmp_path: Path, monkeypatch: MonkeyPatch, stored_end: float
) -> None:
    from app.analysis.transcription.backends import FunASRBackend
    from app.analysis.transcription.pipeline import ASRPipeline
    from app.core.config import settings
    from app.db.entities import Transcript
    from app.pipeline.claiming import pop_and_claim
    from app.pipeline.workers.transcribe import commit_transcript, transcribe_compute

    duration = 120.000625
    source = tmp_path / "legacy-window.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x00" * 1920010)
    context = json.dumps(
        {
            "event_first": {
                "asr_mode": "hotspot",
                "attention_windows": [
                    {
                        "event_key": "detector-v1:event-1",
                        "start_offset_s": 110.0,
                        "end_offset_s": stored_end,
                    }
                ],
            }
        }
    )
    task_id, segment_id, session_id = _seed_pipeline_task(stage=TaskStatus.TRANSCRIBING, context_json=context)
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        assert segment is not None
        segment.file_path = str(source)
        segment.end_ts = _START + timedelta(seconds=duration)
        segment.duration_s = duration
        db.add(segment)
        db.add(HotspotEvent(**_hotspot_payload(session_id)))
    observed_durations: list[float] = []

    class ModelBoundary:
        def generate(self, **kwargs: object) -> list[dict[str, str]]:
            inputs = kwargs["input"]
            assert isinstance(inputs, list) and len(inputs) == 1
            with wave.open(inputs[0], "rb") as audio:
                observed_durations.append(audio.getnframes() / audio.getframerate())
            return [{"text": "片尾仍有一段完整讲话，需要保留原文。"}]

    backend = FunASRBackend(sensevoice=False)
    monkeypatch.setattr(backend, "_load_funasr", lambda *, for_primary=False: ModelBoundary())
    monkeypatch.setattr(settings, "asr_primary", "funasr_nano")
    pipeline = ASRPipeline(primary_backend=backend)
    if stored_end != round(duration, 3):
        with pytest.raises(ValueError, match="超出执行片段"):
            transcribe_compute(task_id, pipeline=pipeline)
        assert observed_durations == []
        return
    result = transcribe_compute(task_id, pipeline=pipeline)
    assert len(observed_durations) == 1
    assert observed_durations[0] == pytest.approx(duration - 110, abs=1 / 16000)
    assert result["attention_results"][0]["end_offset_s"] == duration
    commit_transcript(_lease(task_id, TaskStatus.TRANSCRIBING), result, 1)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        event = db.exec(select(HotspotEvent).where(HotspotEvent.session_id == session_id)).one()
        assert task is not None and task.stage == TaskStatus.QUEUED_FOR_TRANS
        context = json.loads(task.context_json)["event_first"]
        assert context["asr_mode"] == "background"
        assert context["attention_windows"][0]["end_offset_s"] == duration
        evidence = json.loads(event.evidence_json)["items"][0]
        assert db.exec(select(Transcript)).all() == []
        assert evidence["detail"]["end_offset_s"] == duration
        assert datetime.fromisoformat(evidence["end_ts"]) == _START.replace(tzinfo=UTC) + timedelta(seconds=duration)
    claimed = pop_and_claim(TaskStatus.QUEUED_FOR_TRANS)
    assert claimed is not None and claimed.id == task_id
    assert claimed.claimed_by is not None and claimed.lease_token is not None
    background = transcribe_compute(task_id, pipeline=pipeline)
    assert background["transcribed"] is True
    assert observed_durations == [pytest.approx(duration - 110, abs=1 / 16000), duration]
    commit_transcript(TaskLease(task_id, claimed.claimed_by, claimed.lease_token, claimed.stage), background, 1)
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        assert task is not None and task.stage == TaskStatus.TRANSCRIBED
        assert task.claimed_by is None and task.lease_token is None
        transcripts = db.exec(select(Transcript)).all()
        assert len(transcripts) == 1 and transcripts[0].segment_id == segment_id
        assert transcripts[0].final_text == "片尾仍有一段完整讲话，需要保留原文。"


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
    result = transcribe_compute(task_id)

    assert result["transcribed"] is True
    assert calls == [str(resolve_storage_path("segment.ts"))]


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
    assert result["reason"] == "编辑判断未完成（disabled），需人工复核"
    assert metadata["editorial_review"]["execution"]["result"] is None
    assert result["initial_status"] == "pending"


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
