"""完整 HotspotEvent 成片评分、动态边界与幂等提交测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np
from sqlmodel import select

from app.analysis.audio import AudioFeatures
from app.analysis.clip_scorer import ClipScorerConfig, compute_hotspot_clip_draft
from app.analysis.timeline import datetime_epoch
from app.db.entities import (
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    HotspotStatus,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SessionStatus,
)
from app.db.session import get_session

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch

_START = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _audio(duration_s: float = 400.0) -> AudioFeatures:
    return AudioFeatures(
        sample_rate=16_000,
        hop_s=1.0,
        times=np.asarray([0.0, duration_s]),
        rms=np.asarray([0.1, 0.8]),
        duration_s=duration_s,
        silences=[(18.0, 20.0), (88.0, 90.0)],
    )


def _seed_session(*, threshold: float = 0.38) -> tuple[int, int]:
    with get_session() as db:
        room = LiveRoom(
            input_url="clip-scorer",
            room_id=181800,
            auto_analyze=True,
            highlight_threshold=threshold,
            review_threshold=0.30,
        )
        db.add(room)
        db.flush()
        assert room.id is not None
        session = RecordingSession(
            room_id=room.id,
            status=SessionStatus.STOPPED,
            started_at=_START,
            ended_at=_START + timedelta(minutes=10),
        )
        db.add(session)
        db.flush()
        assert session.id is not None
        return session.id, room.id


def _add_segment(
    session_id: int,
    *,
    seq: int,
    start_s: float,
    end_s: float,
) -> int:
    with get_session() as db:
        segment = RawSegment(
            session_id=session_id,
            seq=seq,
            file_path=f"segment-{seq}.ts",
            start_ts=_START + timedelta(seconds=start_s),
            end_ts=_START + timedelta(seconds=end_s),
            duration_s=end_s - start_s,
        )
        db.add(segment)
        db.flush()
        assert segment.id is not None
        return segment.id


def _add_event(
    session_id: int,
    *,
    key: str,
    start_s: float,
    peak_s: float,
    end_s: float,
    heat: float,
    detector_clip: float,
    modalities: dict[str, float | None],
    semantic_confidence: float = 0.2,
    transcript: str | None = None,
    title: str | None = None,
    summary: str | None = None,
) -> int:
    evidence: list[dict[str, object]] = []
    for name, score in modalities.items():
        if score is None:
            continue
        metrics: dict[str, float] = {"score": score}
        if name == "asr":
            metrics.update({"semantic_novelty": score, "topic_change": score})
        evidence.append(
            {
                "id": f"{key}:{name}",
                "type": name,
                "start_ts": (_START + timedelta(seconds=start_s)).isoformat(),
                "end_ts": (_START + timedelta(seconds=end_s)).isoformat(),
                "metrics": metrics,
                "excerpts": [transcript] if name == "asr" and transcript else [],
            }
        )
    with get_session() as db:
        event = HotspotEvent(
            event_key=key,
            session_id=session_id,
            start_ts=_START + timedelta(seconds=start_s),
            peak_ts=_START + timedelta(seconds=peak_s),
            end_ts=_START + timedelta(seconds=end_s),
            status=HotspotStatus.CONFIRMED,
            heat_score=heat,
            clip_score=detector_clip,
            semantic_confidence=semantic_confidence,
            evidence_coverage=0.85,
            title=title,
            summary=summary,
            category="reaction" if title else None,
            features_json=json.dumps(
                {
                    "detector_version": "test-v1",
                    "ticks": [
                        {"modality_scores": modalities},
                        {"modality_scores": modalities},
                    ],
                    "event_lifecycle": {"version": 1},
                },
                ensure_ascii=False,
            ),
            evidence_json=json.dumps({"version": 1, "items": evidence}, ensure_ascii=False),
            representative_danmaku_json=json.dumps(
                [{"text": "太精彩了", "count": 8, "role": "reaction"}],
                ensure_ascii=False,
            ),
            transcript_text=transcript,
        )
        db.add(event)
        db.flush()
        assert event.id is not None
        return event.id


def test_missing_asr_with_strong_multisignal_creates_candidate_draft(temp_db: None) -> None:
    session_id, _room_id = _seed_session()
    segment_id = _add_segment(session_id, seq=0, start_s=0.0, end_s=300.0)
    event_id = _add_event(
        session_id,
        key="strong-no-asr",
        start_s=100.0,
        peak_s=120.0,
        end_s=145.0,
        heat=0.92,
        detector_clip=0.88,
        modalities={"danmaku": 0.90, "audio": 0.85, "sensevoice": 0.80, "asr": None, "trend": None},
        summary="弹幕与现场反应同时明显升高，具体内容仍需结合画面确认。",
    )

    draft = compute_hotspot_clip_draft(
        event_id,
        audio_features=_audio(300.0),
        audio_segment_id=segment_id,
    )
    features = json.loads(draft.features_json)

    assert draft.decision == "candidate"
    assert draft.clip_score >= draft.threshold
    assert features["asr_quality"]["state"] == "unavailable"
    assert features["hotspot_event_id"] == event_id
    assert features["signal_scores"]["danmaku"] == 0.9
    assert features["representative_danmaku"][0]["text"] == "太精彩了"


def test_semantic_only_event_is_kept_but_not_forced_into_candidate(temp_db: None) -> None:
    session_id, _room_id = _seed_session()
    segment_id = _add_segment(session_id, seq=0, start_s=0.0, end_s=300.0)
    event_id = _add_event(
        session_id,
        key="semantic-only",
        start_s=80.0,
        peak_s=100.0,
        end_s=125.0,
        heat=0.25,
        detector_clip=0.45,
        modalities={"danmaku": None, "audio": None, "sensevoice": None, "asr": 0.95, "trend": 0.90},
        semantic_confidence=0.95,
        transcript="主播完整说明了接下来的活动安排和规则。",
        title="主播说明活动安排",
        summary="主播完整说明了接下来的活动安排和规则。",
    )

    draft = compute_hotspot_clip_draft(
        event_id,
        audio_features=_audio(300.0),
        audio_segment_id=segment_id,
    )

    assert draft.decision == "below_threshold"
    assert draft.clip_score == 0.35
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
    assert event is not None and event.status == HotspotStatus.CONFIRMED
    assert event.candidate_id is None


def test_dynamic_bounds_never_cross_recording_gap(temp_db: None) -> None:
    session_id, _room_id = _seed_session()
    _add_segment(session_id, seq=0, start_s=0.0, end_s=100.0)
    source_segment_id = _add_segment(session_id, seq=1, start_s=120.0, end_s=240.0)
    event_id = _add_event(
        session_id,
        key="event-after-gap",
        start_s=90.0,
        peak_s=150.0,
        end_s=175.0,
        heat=0.95,
        detector_clip=0.90,
        modalities={"danmaku": 0.9, "audio": 0.9, "sensevoice": 0.8, "asr": None, "trend": None},
    )

    draft = compute_hotspot_clip_draft(
        event_id,
        audio_features=_audio(120.0),
        audio_segment_id=source_segment_id,
    )
    features = json.loads(draft.features_json)

    assert datetime_epoch(draft.start_ts) >= datetime_epoch(_START + timedelta(seconds=120))
    assert datetime_epoch(draft.end_ts) <= datetime_epoch(_START + timedelta(seconds=240))
    assert features["scoring_components"]["recording_continuity"] == 0.0
    assert datetime_epoch(datetime.fromisoformat(features["timeline"]["recording_block_start"])) == datetime_epoch(
        _START + timedelta(seconds=120)
    )


def test_dynamic_bounds_obey_max_duration(temp_db: None) -> None:
    session_id, _room_id = _seed_session()
    segment_id = _add_segment(session_id, seq=0, start_s=0.0, end_s=400.0)
    event_id = _add_event(
        session_id,
        key="long-event",
        start_s=20.0,
        peak_s=180.0,
        end_s=360.0,
        heat=0.9,
        detector_clip=0.85,
        modalities={"danmaku": 0.85, "audio": 0.8, "sensevoice": 0.8, "asr": None, "trend": None},
    )
    config = ClipScorerConfig(max_duration_s=90.0)

    draft = compute_hotspot_clip_draft(
        event_id,
        audio_features=_audio(400.0),
        audio_segment_id=segment_id,
        config=config,
    )

    assert (draft.end_ts - draft.start_ts).total_seconds() == 90.0
    assert draft.start_ts <= draft.peak_ts <= draft.end_ts


def test_score_hotspot_event_is_idempotent_and_links_existing_review_chain(
    temp_db: None,
    monkeypatch: MonkeyPatch,
) -> None:
    from app.analysis import event_enricher
    from app.pipeline.workers.analyze import score_hotspot_event

    session_id, _room_id = _seed_session()
    _add_segment(session_id, seq=0, start_s=0.0, end_s=300.0)
    event_id = _add_event(
        session_id,
        key="direct-commit",
        start_s=100.0,
        peak_s=120.0,
        end_s=145.0,
        heat=0.95,
        detector_clip=0.90,
        modalities={"danmaku": 0.9, "audio": 0.9, "sensevoice": 0.8, "asr": None, "trend": None},
    )
    monkeypatch.setattr(event_enricher.llm, "call_text", lambda *_args, **_kwargs: None)

    first = score_hotspot_event(event_id)
    second = score_hotspot_event(event_id)

    assert first is not None and second is not None
    assert first.id == second.id
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        candidates = db.exec(select(HighlightCandidate)).all()
        highlight_events = db.exec(select(HighlightEvent)).all()
    assert event is not None and event.candidate_id == first.id
    assert len(candidates) == 1
    assert len(highlight_events) == 1
    assert highlight_events[0].candidate_id == first.id
    features = json.loads(candidates[0].features_json or "{}")
    assert features["hotspot_event_id"] == event_id
    assert features["timeline"]["dynamic_bounds"] is True


def test_distinct_nearby_events_are_not_suppressed_by_time_only_cooldown(temp_db: None) -> None:
    from app.pipeline.workers.analyze import _draft_clusters_existing

    session_id, _room_id = _seed_session()
    with get_session() as db:
        db.add(
            HighlightCandidate(
                session_id=session_id,
                peak_ts=_START + timedelta(seconds=30),
                start_ts=_START,
                end_ts=_START + timedelta(seconds=40),
                highlight_score=0.9,
                features_json=json.dumps(
                    {
                        "hotspot_event_id": 1,
                        "event": {"title": "游戏首领战胜利", "category": "gameplay", "entities": ["游戏甲"]},
                    },
                    ensure_ascii=False,
                ),
                dedup_hash="existing-event",
            )
        )
    draft = {
        "hotspot_event_id": 2,
        "session_id": session_id,
        "peak_ts": _START + timedelta(seconds=75),
        "start_ts": _START + timedelta(seconds=60),
        "end_ts": _START + timedelta(seconds=90),
        "features_json": json.dumps(
            {
                "hotspot_event_id": 2,
                "event": {"title": "主播公布抽奖规则", "category": "announcement", "entities": ["抽奖"]},
            },
            ensure_ascii=False,
        ),
    }

    with get_session() as db:
        suppressed = _draft_clusters_existing(
            db,
            draft,
            dedup_hash="new-event",
            cooldown_s=60.0,
            iou_threshold=0.5,
        )

    assert suppressed is False
