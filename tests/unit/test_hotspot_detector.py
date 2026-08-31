"""Event-first 信号桶、滚动基线与 provisional 生命周期测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pytest
from sqlmodel import select

from app.analysis.hotspot_detector import (
    EvidenceItem,
    HotspotDetector,
    HotspotDetectorConfig,
    HotspotDraft,
    SignalBucket,
    build_segment_signal_buckets,
    dynamic_weighted_score,
    persist_provisional_hotspots,
    robust_relative_uplift,
)

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


_START = datetime(2026, 8, 31, 12, 0, 0)
_CONFIG = HotspotDetectorConfig(
    bucket_s=10.0,
    baseline_window_s=60.0,
    detector_tick_s=20.0,
    min_baseline_buckets=3,
    detection_threshold=0.55,
)


def _danmaku_bucket(
    index: int,
    *,
    count: int,
    users: int,
    intensity: float = 0.05,
    repetition: float = 0.10,
    high_emotion: float = 0.0,
) -> SignalBucket:
    start = _START + timedelta(seconds=index * 10)
    return SignalBucket(
        start_ts=start,
        end_ts=start + timedelta(seconds=10),
        danmaku_count=count,
        danmaku_unique_users=users,
        danmaku_repetition=repetition,
        danmaku_intensity=intensity,
        danmaku_high_emotion=high_emotion,
        representative_messages=("高能",) if high_emotion else ("日常",),
    )


def _audio_bucket(index: int, *, mean: float, peak: float, change: float, local: float) -> SignalBucket:
    start = _START + timedelta(seconds=index * 10)
    return SignalBucket(
        start_ts=start,
        end_ts=start + timedelta(seconds=10),
        audio_rms_mean=mean,
        audio_rms_peak=peak,
        audio_energy_change=change,
        audio_silence_ratio=0.05,
        audio_local_peak=local,
    )


def _create_recording() -> tuple[int, int]:
    from app.db.entities import LiveRoom, RecordingSession
    from app.db.session import get_session

    with get_session() as db:
        room = LiveRoom(input_url="100", room_id=100, authorized=True)
        db.add(room)
        db.flush()
        assert room.id is not None
        recording = RecordingSession(room_id=room.id, started_at=_START)
        db.add(recording)
        db.flush()
        assert recording.id is not None
        return recording.id, room.id


def test_detector_configuration_rejects_unaligned_windows() -> None:
    with pytest.raises(ValueError, match="整数倍"):
        HotspotDetectorConfig(bucket_s=10.0, baseline_window_s=90.0, detector_tick_s=15.0)


def test_dynamic_normalization_ignores_missing_asr() -> None:
    weights = {"danmaku": 0.4, "audio": 0.3, "asr": 0.3}
    missing_score, coverage = dynamic_weighted_score(
        {"danmaku": 0.9, "audio": 0.8, "asr": None},
        weights,
    )
    omitted_score, omitted_coverage = dynamic_weighted_score(
        {"danmaku": 0.9, "audio": 0.8},
        weights,
    )

    assert missing_score == pytest.approx(omitted_score)
    assert coverage == pytest.approx(0.7)
    assert omitted_coverage == pytest.approx(0.7)


def test_relative_baseline_favors_small_room_eightfold_burst() -> None:
    small_room = robust_relative_uplift(80.0, [10.0] * 9)
    large_room = robust_relative_uplift(230.0, [200.0] * 9)

    assert small_room > 0.9
    assert large_room < 0.15


def test_danmaku_burst_without_asr_creates_provisional_draft() -> None:
    buckets = [_danmaku_bucket(index, count=10, users=8) for index in range(12)]
    for index in (7, 8):
        buckets[index] = _danmaku_bucket(
            index,
            count=80,
            users=60,
            intensity=0.9,
            repetition=0.35,
            high_emotion=0.8,
        )

    drafts = HotspotDetector(_CONFIG).detect(7, buckets)

    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.session_id == 7
    assert draft.heat_score >= _CONFIG.detection_threshold
    assert draft.transcript_text is None
    assert draft.evidence_coverage == pytest.approx(0.38)
    assert {item.type for item in draft.evidence} == {"danmaku"}


def test_large_room_small_absolute_increase_does_not_trigger() -> None:
    buckets = [_danmaku_bucket(index, count=200, users=150) for index in range(10)]
    for index in (7, 8):
        buckets[index] = _danmaku_bucket(index, count=230, users=165)

    assert HotspotDetector(_CONFIG).detect(8, buckets) == []


def test_audio_only_evidence_can_trigger_and_records_partial_coverage() -> None:
    buckets = [_audio_bucket(index, mean=0.10, peak=0.12, change=0.01, local=0.02) for index in range(10)]
    for index in (7, 8):
        buckets[index] = _audio_bucket(index, mean=0.80, peak=1.0, change=0.70, local=0.55)

    drafts = HotspotDetector(_CONFIG).detect(9, buckets)

    assert len(drafts) == 1
    assert drafts[0].evidence_coverage == pytest.approx(0.32)
    assert {item.type for item in drafts[0].evidence} == {"audio"}


def test_single_strong_audio_modality_survives_available_quiet_danmaku() -> None:
    buckets = []
    for index in range(10):
        audio = _audio_bucket(index, mean=0.10, peak=0.12, change=0.01, local=0.02)
        buckets.append(
            SignalBucket(
                start_ts=audio.start_ts,
                end_ts=audio.end_ts,
                danmaku_count=0,
                danmaku_unique_users=0,
                danmaku_repetition=0.0,
                danmaku_intensity=0.0,
                danmaku_high_emotion=0.0,
                audio_rms_mean=audio.audio_rms_mean,
                audio_rms_peak=audio.audio_rms_peak,
                audio_energy_change=audio.audio_energy_change,
                audio_silence_ratio=audio.audio_silence_ratio,
                audio_local_peak=audio.audio_local_peak,
            )
        )
    for index in (7, 8):
        audio = _audio_bucket(index, mean=0.80, peak=1.0, change=0.70, local=0.55)
        buckets[index] = SignalBucket(
            start_ts=audio.start_ts,
            end_ts=audio.end_ts,
            danmaku_count=0,
            danmaku_unique_users=0,
            danmaku_repetition=0.0,
            danmaku_intensity=0.0,
            danmaku_high_emotion=0.0,
            audio_rms_mean=audio.audio_rms_mean,
            audio_rms_peak=audio.audio_rms_peak,
            audio_energy_change=audio.audio_energy_change,
            audio_silence_ratio=audio.audio_silence_ratio,
            audio_local_peak=audio.audio_local_peak,
        )

    drafts = HotspotDetector(_CONFIG).detect(12, buckets)

    assert len(drafts) == 1
    assert drafts[0].evidence_coverage == pytest.approx(0.70)


def test_sensevoice_event_is_optional_but_can_trigger_without_asr() -> None:
    buckets = []
    for index in range(10):
        start = _START + timedelta(seconds=index * 10)
        buckets.append(
            SignalBucket(
                start_ts=start,
                end_ts=start + timedelta(seconds=10),
                sensevoice_intensity=1.0 if index == 7 else 0.0,
                sensevoice_events=("surprise",) if index == 7 else (),
            )
        )

    drafts = HotspotDetector(_CONFIG).detect(11, buckets)

    assert len(drafts) == 1
    assert drafts[0].transcript_text is None
    assert drafts[0].evidence_coverage == pytest.approx(0.15)
    assert {item.type for item in drafts[0].evidence} == {"sensevoice"}


def test_separated_bursts_remain_separate_provisional_events() -> None:
    buckets = [_danmaku_bucket(index, count=10, users=8) for index in range(13)]
    for index in (3, 4, 9, 10):
        buckets[index] = _danmaku_bucket(
            index,
            count=100,
            users=75,
            intensity=0.9,
            high_emotion=0.8,
        )

    drafts = HotspotDetector(_CONFIG).detect(10, buckets)

    assert len(drafts) == 2
    assert drafts[0].event_key != drafts[1].event_key
    assert drafts[0].end_ts < drafts[1].start_ts


def test_evidence_schema_accepts_future_visual_type() -> None:
    item = EvidenceItem(
        type="visual",
        start_ts=_START,
        end_ts=_START + timedelta(seconds=10),
        metrics={"motion": 0.8},
    )

    assert item.to_dict()["type"] == "visual"


def test_builder_compensates_danmaku_receive_lag(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.db.entities import Danmaku, RawSegment
    from app.db.session import get_session

    session_id, _room_id = _create_recording()
    monkeypatch.setattr(settings, "collect_danmaku", True)
    monkeypatch.setattr(settings, "danmaku_event_lag_s", 7.5)
    with get_session() as db:
        segment = RawSegment(
            session_id=session_id,
            seq=0,
            file_path="segment.ts",
            start_ts=_START,
            end_ts=_START + timedelta(seconds=120),
            duration_s=120.0,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        segment_id = segment.id
        db.add(
            Danmaku(
                session_id=session_id,
                room_id=100,
                ts=_START + timedelta(seconds=27.5),
                user="viewer",
                content="高能",
            )
        )

    buckets, actual_session_id = build_segment_signal_buckets(
        segment_id,
        audio_features=None,
        config=_CONFIG,
    )

    assert actual_session_id == session_id
    assert buckets[2].danmaku_count == 1
    assert buckets[1].danmaku_count == 0


def test_builder_excludes_degraded_asr_but_keeps_sensevoice(temp_db: None) -> None:
    """低质量正文不能污染语义信号，但同次识别的非语义音频事件仍可用。"""
    from app.db.entities import RawSegment, Transcript
    from app.db.session import get_session

    session_id, _room_id = _create_recording()
    with get_session() as db:
        segment = RawSegment(
            session_id=session_id,
            seq=0,
            file_path="segment.ts",
            start_ts=_START,
            end_ts=_START + timedelta(seconds=120),
            duration_s=120.0,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        segment_id = segment.id
        db.add(
            Transcript(
                segment_id=segment_id,
                final_text="高能高能高能高能高能高能高能高能",
                words_json='[{"w":"高能","start":20,"end":25}]',
                auxiliary_json=json.dumps(
                    {
                        "asr_quality": {
                            "state": "degraded",
                            "usable": False,
                            "reason": "degenerate_repetition",
                        },
                        "emotions": [
                            {
                                "type": "surprise",
                                "start": 20,
                                "end": 25,
                                "confidence": 0.9,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
        )

    buckets, _actual_session_id = build_segment_signal_buckets(
        segment_id,
        audio_features=None,
        config=_CONFIG,
    )

    assert all(bucket.asr_text is None for bucket in buckets)
    assert all(bucket.asr_keyword_score is None for bucket in buckets)
    assert buckets[2].sensevoice_intensity is not None
    assert buckets[2].sensevoice_intensity > 0


def test_provisional_persistence_is_idempotent_and_refreshes_retry(
    temp_db: None,
) -> None:
    from app.db.entities import HotspotEvent, HotspotStatus
    from app.db.session import get_session

    session_id, _room_id = _create_recording()
    evidence = EvidenceItem(
        type="audio",
        start_ts=_START,
        end_ts=_START + timedelta(seconds=20),
        metrics={"score": 0.9},
    )
    draft = HotspotDraft(
        event_key="stable-provisional",
        session_id=session_id,
        start_ts=_START,
        peak_ts=_START + timedelta(seconds=10),
        end_ts=_START + timedelta(seconds=20),
        heat_score=0.8,
        clip_score=0.7,
        semantic_confidence=0.0,
        evidence_coverage=0.32,
        features={"version": 1},
        evidence=(evidence,),
        transcript_text=None,
    )
    with get_session() as db:
        first = persist_provisional_hotspots(db, [draft], expected_session_id=session_id)
    updated_payload = draft.to_payload()
    updated_payload["heat_score"] = 0.95
    with get_session() as db:
        second = persist_provisional_hotspots(db, [updated_payload], expected_session_id=session_id)

    assert first == second
    with get_session() as db:
        events = db.exec(select(HotspotEvent).where(HotspotEvent.session_id == session_id)).all()
    assert len(events) == 1
    assert events[0].status == HotspotStatus.PROVISIONAL
    assert events[0].candidate_id is None
    assert events[0].heat_score == pytest.approx(0.95)
    assert json.loads(events[0].evidence_json or "{}")["version"] == 1


def test_commit_writes_hotspot_in_same_analysis_transaction(temp_db: None) -> None:
    from app.db.entities import HotspotEvent, RawSegment, SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline.lease import TaskLease
    from app.pipeline.workers.analyze import HighlightDecision, commit_highlight

    session_id, _room_id = _create_recording()
    with get_session() as db:
        segment = RawSegment(
            session_id=session_id,
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
            session_id=session_id,
            stage=TaskStatus.ANALYZING,
            claimed_by="hotspot-worker",
            lease_token="hotspot-token",
            pipeline_key=f"pipeline:{segment.id}",
        )
        db.add(task)
        db.flush()
        assert task.id is not None
        task_id = task.id
        segment_id = segment.id

    payload = HotspotDraft(
        event_key="commit-provisional",
        session_id=session_id,
        start_ts=_START + timedelta(seconds=30),
        peak_ts=_START + timedelta(seconds=40),
        end_ts=_START + timedelta(seconds=50),
        heat_score=0.9,
        clip_score=0.8,
        semantic_confidence=0.0,
        evidence_coverage=0.38,
        features={},
        evidence=(),
        transcript_text=None,
    ).to_payload()
    lease = TaskLease(
        task_id=task_id,
        worker_id="hotspot-worker",
        lease_token="hotspot-token",
        expected_stage=TaskStatus.ANALYZING,
    )

    commit_highlight(
        lease,
        {
            "decision": HighlightDecision.SKIPPED,
            "segment_id": segment_id,
            "session_id": session_id,
            "reason": "legacy candidate path skipped",
            "hotspot_drafts": [payload],
        },
        20,
    )

    with get_session() as db:
        events = db.exec(select(HotspotEvent).where(HotspotEvent.session_id == session_id)).all()
        task = db.get(SegmentTask, task_id)
    assert len(events) == 1
    assert task is not None and task.stage == TaskStatus.COMPLETED


def test_analyze_compute_detects_hotspot_before_missing_asr_gate(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis.audio import AudioFeatures
    from app.core.config import settings
    from app.db.entities import RawSegment, SegmentTask, TaskStatus
    from app.db.session import get_session
    from app.pipeline.workers import analyze
    from app.pipeline.workers.analyze import HighlightDecision

    session_id, _room_id = _create_recording()
    with get_session() as db:
        segment = RawSegment(
            session_id=session_id,
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
            session_id=session_id,
            stage=TaskStatus.QUEUED_FOR_ANALYSIS,
            pipeline_key=f"pipeline:{segment.id}",
            context_json='{"event_first":{"analysis_pass":"detect"}}',
        )
        db.add(task)
        db.flush()
        assert task.id is not None
        task_id = task.id

    times = np.arange(0.5, 120.0, 1.0)
    rms = np.full(times.shape, 0.10)
    rms[(times >= 70.0) & (times < 90.0)] = 1.0
    audio = AudioFeatures(
        sample_rate=16000,
        hop_s=1.0,
        times=times,
        rms=rms,
        duration_s=120.0,
        silences=[],
    )
    monkeypatch.setattr(settings, "collect_danmaku", False)
    monkeypatch.setattr(analyze.audio_mod, "analyze_audio", lambda _path: audio)

    result = analyze.analyze_compute(task_id)

    assert result["decision"] == HighlightDecision.SIGNAL_PASS
    assert isinstance(result["hotspot_drafts"], list)
    assert len(result["hotspot_drafts"]) == 1
