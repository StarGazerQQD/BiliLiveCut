"""基于完整 :class:`HotspotEvent` 的成片价值评分与动态边界计算。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlmodel import Session, select

from app.analysis.event_enricher import (
    EventEnrichmentDraft,
    build_event_evidence_bundle,
)
from app.analysis.highlight import candidate_time_bounds, contiguous_recording_range
from app.analysis.scoring_config import get_scoring_config
from app.analysis.timeline import datetime_epoch
from app.analysis.transcription.quality import assess_transcript_quality, transcript_quality_payload
from app.core.config import settings
from app.db.entities import (
    CandidateStatus,
    HotspotEvent,
    HotspotStatus,
    LiveRoom,
    RawSegment,
    RecordingSession,
)
from app.db.session import get_session

if TYPE_CHECKING:
    from app.analysis.audio import AudioFeatures

EVENT_CLIP_SCORER_VERSION = 1

_DEFAULT_WEIGHTS: dict[str, float] = {
    "heat": 0.22,
    "detector_clip": 0.16,
    "reaction": 0.22,
    "event_duration": 0.08,
    "event_completeness": 0.10,
    "speech_completeness": 0.04,
    "novelty": 0.05,
    "audio_strength": 0.05,
    "evidence_coverage": 0.04,
    "recording_continuity": 0.04,
}


@dataclass(frozen=True, slots=True)
class ClipScorerConfig:
    """事件级成片评分、留白与连续录像约束。"""

    weights: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    pre_roll_s: float = 60.0
    post_roll_s: float = 30.0
    minimum_pre_roll_s: float = 20.0
    minimum_post_roll_s: float = 30.0
    max_duration_s: float = 180.0
    gap_tolerance_s: float = 1.0
    semantic_only_score_cap: float = 0.35

    def __post_init__(self) -> None:
        """拒绝无效权重与无法生成有效切片的配置。"""
        if set(self.weights) != set(_DEFAULT_WEIGHTS):
            raise ValueError(f"ClipScorer weights 必须精确包含 {sorted(_DEFAULT_WEIGHTS)}")
        if any(not math.isfinite(value) or value < 0.0 for value in self.weights.values()):
            raise ValueError("ClipScorer weights 必须是非负有限数")
        if sum(self.weights.values()) <= 0.0:
            raise ValueError("ClipScorer weights 总和必须大于 0")
        if min(self.pre_roll_s, self.post_roll_s, self.minimum_pre_roll_s, self.minimum_post_roll_s) < 0.0:
            raise ValueError("ClipScorer 留白不能为负数")
        if self.max_duration_s <= 5.0:
            raise ValueError("ClipScorer max_duration_s 必须大于 5 秒")
        if self.gap_tolerance_s < 0.0:
            raise ValueError("ClipScorer gap_tolerance_s 不能为负数")
        if not 0.0 <= self.semantic_only_score_cap <= 1.0:
            raise ValueError("semantic_only_score_cap 必须在 0~1 之间")

    @classmethod
    def from_settings(cls) -> ClipScorerConfig:
        """复用现有评分留白、最大时长与录制连续性配置。"""
        scoring = get_scoring_config()
        return cls(
            pre_roll_s=scoring.pre_roll_s,
            post_roll_s=scoring.post_roll_s,
            minimum_pre_roll_s=min(scoring.pre_roll_s, settings.highlight_min_pre_roll_s),
            minimum_post_roll_s=min(scoring.post_roll_s, settings.highlight_min_post_roll_s),
            max_duration_s=float(settings.clip_max_duration_s),
            gap_tolerance_s=settings.hotspot_recording_gap_tolerance_s,
        )


@dataclass(frozen=True, slots=True)
class EventClipDraft:
    """可在提交事务中重新校验的事件级成片评分结果。"""

    hotspot_event_id: int
    event_key: str
    session_id: int
    room_id: int | None
    segment_id: int
    decision: str
    threshold: float
    heat_score: float
    detector_clip_score: float
    semantic_confidence: float
    evidence_coverage: float
    clip_score: float
    start_ts: datetime
    peak_ts: datetime
    end_ts: datetime
    reason: str
    dedup_hash: str
    features_json: str
    asr_text: str | None
    initial_status: str
    bundle_fingerprint: str
    input_fingerprint: str

    def to_payload(self) -> dict[str, object]:
        """转换为既有 Candidate/Event 提交链可消费的纯值载荷。"""
        return {
            "decision": self.decision,
            "hotspot_event_id": self.hotspot_event_id,
            "event_key": self.event_key,
            "session_id": self.session_id,
            "room_id": self.room_id,
            "segment_id": self.segment_id,
            "threshold": self.threshold,
            "heat_score": self.heat_score,
            "detector_clip_score": self.detector_clip_score,
            "semantic_confidence": self.semantic_confidence,
            "evidence_coverage": self.evidence_coverage,
            "score": self.clip_score,
            "rule_score": self.clip_score,
            "llm_score": 0.0,
            "highlight_score": self.clip_score,
            "start_ts": self.start_ts,
            "peak_ts": self.peak_ts,
            "end_ts": self.end_ts,
            "reason": self.reason,
            "dedup_hash": self.dedup_hash,
            "features_json": self.features_json,
            "asr_text": self.asr_text,
            "initial_status": self.initial_status,
            "bundle_fingerprint": self.bundle_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "config_hash": self.input_fingerprint,
        }


def pending_hotspot_event_ids(
    session_id: int,
    *,
    config: ClipScorerConfig | None = None,
) -> list[int]:
    """返回证据或配置变化后仍需事件级评分的已确认热点。"""
    cfg = config or ClipScorerConfig.from_settings()
    with get_session() as db:
        rows = db.exec(
            select(HotspotEvent)
            .where(
                HotspotEvent.session_id == session_id,
                HotspotEvent.status == HotspotStatus.CONFIRMED,
                HotspotEvent.candidate_id.is_(None),
            )
            .order_by(HotspotEvent.peak_ts.asc(), HotspotEvent.id.asc())
        ).all()
        pending: list[int] = []
        for event in rows:
            if event.id is None:
                continue
            if not event_clip_score_is_current(db, event, config=cfg):
                pending.append(event.id)
        return pending


def event_clip_score_is_current(
    db: Session,
    event: HotspotEvent,
    *,
    config: ClipScorerConfig | None = None,
) -> bool:
    """判断事件评分元数据是否与当前证据、阈值和边界配置一致。"""
    if event.id is None or event.status != HotspotStatus.CONFIRMED or event.candidate_id is not None:
        return event.candidate_id is not None
    cfg = config or ClipScorerConfig.from_settings()
    try:
        bundle = build_event_evidence_bundle(db, event.id)
        context = _load_event_context(db, event, bundle.fingerprint, config=cfg)
    except ValueError as exc:
        logger.warning("event_clip_current_invalid: event_id={} error={}", event.id, exc)
        return False
    metadata = _json_mapping(event.features_json).get("event_clip_score")
    previous = metadata.get("input_fingerprint") if isinstance(metadata, Mapping) else None
    return previous == context.input_fingerprint


def compute_hotspot_clip_draft(
    event_id: int,
    *,
    enrichment: EventEnrichmentDraft | None = None,
    audio_features: AudioFeatures | None = None,
    audio_segment_id: int | None = None,
    config: ClipScorerConfig | None = None,
) -> EventClipDraft:
    """在事务外按完整事件评分，并生成不跨断流缺口的动态候选边界。"""
    cfg = config or ClipScorerConfig.from_settings()
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        if event is None:
            raise ValueError(f"HotspotEvent 不存在: event_id={event_id}")
        if event.status != HotspotStatus.CONFIRMED:
            raise ValueError(f"仅 confirmed HotspotEvent 可评分: event_id={event_id} status={event.status}")
        if event.candidate_id is not None:
            raise ValueError(f"HotspotEvent 已关联候选: event_id={event_id} candidate_id={event.candidate_id}")
        bundle = build_event_evidence_bundle(db, event_id)
        if enrichment is not None and enrichment.bundle_fingerprint != bundle.fingerprint:
            raise ValueError("EventEnrichmentDraft 与当前 HotspotEvent 证据快照不一致")
        context = _load_event_context(
            db,
            event,
            bundle.fingerprint,
            enrichment=enrichment,
            config=cfg,
        )

    silences: list[tuple[float, float]] = []
    if audio_features is not None and audio_segment_id == context.segment.id:
        silences = list(audio_features.silences)
    else:
        try:
            from app.analysis.audio import analyze_audio

            silences = list(analyze_audio(context.segment.file_path).silences)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.info(
                "event_clip_silence_snap_unavailable: event_id={} segment_id={} error={}",
                event_id,
                context.segment.id,
                exc,
            )

    start_ts, end_ts, peak_ts = _dynamic_event_bounds(context, silences=silences, config=cfg)
    components = _score_components(context)
    clip_score = _weighted_score(components, cfg.weights)
    if context.heat_score < 0.45 and components["reaction"] < 0.25:
        clip_score = min(clip_score, cfg.semantic_only_score_cap)
    clip_score = _clamp(clip_score)
    decision = "candidate" if clip_score >= context.threshold else "below_threshold"
    initial_status = _initial_candidate_status(context, clip_score)
    reason = _clip_reason(context, components, clip_score, decision)
    features = _candidate_features(
        context,
        components=components,
        clip_score=clip_score,
        decision=decision,
        start_ts=start_ts,
        end_ts=end_ts,
        config=cfg,
    )
    return EventClipDraft(
        hotspot_event_id=context.event_id,
        event_key=context.event_key,
        session_id=context.session_id,
        room_id=context.room_id,
        segment_id=context.segment.id,
        decision=decision,
        threshold=context.threshold,
        heat_score=context.heat_score,
        detector_clip_score=context.detector_clip_score,
        semantic_confidence=context.semantic_confidence,
        evidence_coverage=context.evidence_coverage,
        clip_score=clip_score,
        start_ts=start_ts,
        peak_ts=peak_ts,
        end_ts=end_ts,
        reason=reason,
        dedup_hash=hashlib.sha256(f"hotspot-event-candidate-v1:{context.event_key}".encode()).hexdigest(),
        features_json=json.dumps(features, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        asr_text=context.asr_text,
        initial_status=initial_status,
        bundle_fingerprint=context.bundle_fingerprint,
        input_fingerprint=context.input_fingerprint,
    )


def validate_event_clip_draft(
    db: Session,
    draft: EventClipDraft,
    *,
    config: ClipScorerConfig | None = None,
) -> HotspotEvent | None:
    """提交前重算指纹，拒绝计算期间发生边界、证据或配置变化的结果。"""
    cfg = config or ClipScorerConfig.from_settings()
    event = db.get(HotspotEvent, draft.hotspot_event_id)
    if (
        event is None
        or event.status != HotspotStatus.CONFIRMED
        or event.candidate_id is not None
        or event.event_key != draft.event_key
    ):
        return None
    try:
        bundle = build_event_evidence_bundle(db, draft.hotspot_event_id)
        context = _load_event_context(db, event, bundle.fingerprint, config=cfg)
    except ValueError:
        return None
    if bundle.fingerprint != draft.bundle_fingerprint or context.input_fingerprint != draft.input_fingerprint:
        logger.warning(
            "event_clip_stale: event_id={} expected={} actual={}",
            draft.hotspot_event_id,
            draft.input_fingerprint[:12],
            context.input_fingerprint[:12],
        )
        return None
    return event


def mark_event_clip_evaluated(db: Session, event: HotspotEvent, draft: EventClipDraft) -> None:
    """持久化事件级评分元数据；低于阈值的热点仍保留且不生成候选。"""
    features = _json_mapping(event.features_json)
    candidate_features = _json_mapping(draft.features_json)
    features["event_clip_score"] = {
        "version": EVENT_CLIP_SCORER_VERSION,
        "input_fingerprint": draft.input_fingerprint,
        "bundle_fingerprint": draft.bundle_fingerprint,
        "detector_clip_score": draft.detector_clip_score,
        "clip_score": draft.clip_score,
        "threshold": draft.threshold,
        "decision": draft.decision,
        "bounds": {
            "start_ts": draft.start_ts.isoformat(),
            "peak_ts": draft.peak_ts.isoformat(),
            "end_ts": draft.end_ts.isoformat(),
        },
        "components": candidate_features.get("scoring_components", {}),
    }
    event.clip_score = draft.clip_score
    event.features_json = json.dumps(features, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    db.add(event)


@dataclass(frozen=True, slots=True)
class _EventContext:
    event_id: int
    event_key: str
    session_id: int
    room_id: int | None
    segment: RawSegment
    segments: tuple[RawSegment, ...]
    block_start: datetime
    block_end: datetime
    event_start: datetime
    event_peak: datetime
    event_end: datetime
    heat_score: float
    detector_clip_score: float
    semantic_confidence: float
    evidence_coverage: float
    threshold: float
    review_threshold: float
    auto_approve: bool
    auto_approve_threshold: float
    title: str | None
    summary: str | None
    category: str | None
    entities: tuple[str, ...]
    enrichment_source: str | None
    evidence_ids: tuple[str, ...]
    asr_text: str | None
    representative_danmaku: tuple[Mapping[str, object], ...]
    modality_scores: Mapping[str, float | None]
    novelty: float
    speech_completeness: float
    asr_quality: Mapping[str, object]
    event_lifecycle_version: object
    bundle_fingerprint: str
    input_fingerprint: str


def _load_event_context(
    db: Session,
    event: HotspotEvent,
    bundle_fingerprint: str,
    *,
    enrichment: EventEnrichmentDraft | None = None,
    config: ClipScorerConfig,
) -> _EventContext:
    if event.id is None:
        raise ValueError("HotspotEvent 缺少主键")
    session = db.get(RecordingSession, event.session_id)
    if session is None:
        raise ValueError(f"RecordingSession 不存在: session_id={event.session_id}")
    room = db.get(LiveRoom, session.room_id)
    segments = tuple(
        db.exec(
            select(RawSegment)
            .where(
                RawSegment.session_id == event.session_id,
                RawSegment.start_ts.is_not(None),
                RawSegment.end_ts.is_not(None),
            )
            .order_by(RawSegment.seq.asc())
        ).all()
    )
    segment = _segment_for_peak(segments, event.peak_ts)
    if segment.id is None or segment.start_ts is None or segment.end_ts is None:
        raise ValueError(f"HotspotEvent 峰值没有可用录像分段: event_id={event.id}")
    block_start, block_end = contiguous_recording_range(
        list(segments),
        segment,
        gap_tolerance_s=config.gap_tolerance_s,
    )
    event_features = _json_mapping(event.features_json)
    enrichment_meta = event_features.get("event_enrichment")
    meta = dict(enrichment_meta) if isinstance(enrichment_meta, Mapping) else {}
    title = enrichment.title if enrichment is not None else event.title
    summary = enrichment.summary if enrichment is not None else event.summary
    category = enrichment.category if enrichment is not None else event.category
    semantic_confidence = enrichment.semantic_confidence if enrichment is not None else event.semantic_confidence
    entities = enrichment.entities if enrichment is not None else _string_tuple(meta.get("entities"))
    enrichment_source = enrichment.source if enrichment is not None else _optional_str(meta.get("source"))
    evidence_ids = enrichment.evidence_ids if enrichment is not None else _string_tuple(meta.get("evidence_ids"))
    modality_scores = _event_modality_scores(event_features, event.evidence_json)
    novelty = _event_novelty(event.evidence_json)
    quality, speech_completeness = _asr_quality(event.transcript_text)
    representative = _representative_danmaku(event.representative_danmaku_json)
    threshold = _score_or_default(room.highlight_threshold if room is not None else None, settings.highlight_threshold)
    review_threshold = _score_or_default(
        room.review_threshold if room is not None else None,
        settings.highlight_review_threshold,
    )
    auto_threshold = _score_or_default(
        room.auto_approve_threshold if room is not None else None,
        settings.highlight_auto_approve_threshold,
    )
    detector_clip = _detector_clip_score(event, event_features)
    lifecycle = event_features.get("event_lifecycle")
    lifecycle_version = lifecycle.get("version") if isinstance(lifecycle, Mapping) else None
    fingerprint_payload = {
        "version": EVENT_CLIP_SCORER_VERSION,
        "event": {
            "id": event.id,
            "event_key": event.event_key,
            "start_ts": event.start_ts.isoformat(),
            "peak_ts": event.peak_ts.isoformat(),
            "end_ts": event.end_ts.isoformat(),
            "heat_score": round(float(event.heat_score), 6),
            "detector_clip_score": round(detector_clip, 6),
            "evidence_coverage": round(float(event.evidence_coverage), 6),
            "title": title,
            "summary": summary,
            "category": category,
            "semantic_confidence": round(float(semantic_confidence), 6),
            "entities": list(entities),
            "representative_danmaku": [dict(item) for item in representative],
        },
        "bundle_fingerprint": bundle_fingerprint,
        "modality_scores": dict(modality_scores),
        "novelty": novelty,
        "speech_completeness": speech_completeness,
        "recording_block": [block_start.isoformat(), block_end.isoformat()],
        "threshold": threshold,
        "review_threshold": review_threshold,
        "auto_approve": bool(room.auto_approve) if room is not None else False,
        "auto_approve_threshold": auto_threshold,
        "config": asdict(config),
    }
    input_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return _EventContext(
        event_id=event.id,
        event_key=event.event_key,
        session_id=event.session_id,
        room_id=room.id if room is not None else None,
        segment=segment,
        segments=segments,
        block_start=block_start,
        block_end=block_end,
        event_start=event.start_ts,
        event_peak=event.peak_ts,
        event_end=event.end_ts,
        heat_score=_clamp(float(event.heat_score)),
        detector_clip_score=detector_clip,
        semantic_confidence=_clamp(float(semantic_confidence)),
        evidence_coverage=_clamp(float(event.evidence_coverage)),
        threshold=threshold,
        review_threshold=review_threshold,
        auto_approve=bool(room.auto_approve) if room is not None else False,
        auto_approve_threshold=auto_threshold,
        title=title,
        summary=summary,
        category=category,
        entities=entities,
        enrichment_source=enrichment_source,
        evidence_ids=evidence_ids,
        asr_text=event.transcript_text,
        representative_danmaku=representative,
        modality_scores=modality_scores,
        novelty=novelty,
        speech_completeness=speech_completeness,
        asr_quality=quality,
        event_lifecycle_version=lifecycle_version,
        bundle_fingerprint=bundle_fingerprint,
        input_fingerprint=input_fingerprint,
    )


def _score_components(context: _EventContext) -> dict[str, float]:
    duration_s = max(0.0, datetime_epoch(context.event_end) - datetime_epoch(context.event_start))
    modality = context.modality_scores
    reaction = _weighted_available(
        {
            "danmaku": modality.get("danmaku"),
            "sensevoice": modality.get("sensevoice"),
            "audio": modality.get("audio"),
        },
        {"danmaku": 0.55, "sensevoice": 0.30, "audio": 0.15},
    )
    event_within_block = (
        datetime_epoch(context.event_start) >= datetime_epoch(context.block_start) - 1e-6
        and datetime_epoch(context.event_end) <= datetime_epoch(context.block_end) + 1e-6
    )
    return {
        "heat": context.heat_score,
        "detector_clip": context.detector_clip_score,
        "reaction": reaction,
        "event_duration": _clamp(duration_s / 30.0),
        "event_completeness": _clamp(0.5 + min(duration_s / max(settings.hotspot_detector_tick_s, 1.0), 1.0) * 0.5),
        "speech_completeness": context.speech_completeness,
        "novelty": context.novelty,
        "audio_strength": _clamp(modality.get("audio") or 0.0),
        "evidence_coverage": context.evidence_coverage,
        "recording_continuity": 1.0 if event_within_block else 0.0,
    }


def _dynamic_event_bounds(
    context: _EventContext,
    *,
    silences: list[tuple[float, float]],
    config: ClipScorerConfig,
) -> tuple[datetime, datetime, datetime]:
    segment_start = context.segment.start_ts
    if segment_start is None:
        raise ValueError("来源分段缺少 start_ts")
    segment_start_epoch = datetime_epoch(segment_start)
    start_ts, end_ts, peak_ts = candidate_time_bounds(
        segment_start=segment_start,
        available_start=context.block_start,
        available_end=context.block_end,
        peak_offset_s=datetime_epoch(context.event_peak) - segment_start_epoch,
        pre_roll_s=config.pre_roll_s,
        post_roll_s=config.post_roll_s,
        suggested_start_offset_s=datetime_epoch(context.event_start) - segment_start_epoch,
        suggested_end_offset_s=datetime_epoch(context.event_end) - segment_start_epoch,
        silences=silences,
        minimum_pre_roll_s=config.minimum_pre_roll_s,
        minimum_post_roll_s=config.minimum_post_roll_s,
    )
    return _cap_bounds(
        start_ts,
        end_ts,
        peak_ts,
        available_start=context.block_start,
        available_end=context.block_end,
        max_duration_s=config.max_duration_s,
    )


def _cap_bounds(
    start_ts: datetime,
    end_ts: datetime,
    peak_ts: datetime,
    *,
    available_start: datetime,
    available_end: datetime,
    max_duration_s: float,
) -> tuple[datetime, datetime, datetime]:
    start_epoch = datetime_epoch(start_ts)
    end_epoch = datetime_epoch(end_ts)
    peak_epoch = datetime_epoch(peak_ts)
    if end_epoch - start_epoch <= max_duration_s:
        return start_ts, end_ts, peak_ts
    block_start = datetime_epoch(available_start)
    block_end = datetime_epoch(available_end)
    before_target = max_duration_s * 0.60
    capped_start = max(block_start, peak_epoch - before_target)
    capped_end = min(block_end, capped_start + max_duration_s)
    if capped_end - capped_start < max_duration_s:
        capped_start = max(block_start, capped_end - max_duration_s)
    capped_start = min(capped_start, peak_epoch)
    capped_end = max(capped_end, peak_epoch)
    if capped_end <= capped_start:
        raise ValueError("最大时长约束后没有有效候选边界")
    return (
        _epoch_like(capped_start, start_ts),
        _epoch_like(capped_end, end_ts),
        peak_ts,
    )


def _candidate_features(
    context: _EventContext,
    *,
    components: Mapping[str, float],
    clip_score: float,
    decision: str,
    start_ts: datetime,
    end_ts: datetime,
    config: ClipScorerConfig,
) -> dict[str, object]:
    return {
        "hotspot_event_id": context.event_id,
        "hotspot_event_key": context.event_key,
        "event_version": context.event_lifecycle_version,
        "clip_scorer_version": EVENT_CLIP_SCORER_VERSION,
        "heat_score": context.heat_score,
        "detector_clip_score": context.detector_clip_score,
        "clip_score": clip_score,
        "semantic_confidence": context.semantic_confidence,
        "evidence_coverage": context.evidence_coverage,
        "signal_scores": dict(context.modality_scores),
        "scoring_components": dict(components),
        "scoring_weights": dict(config.weights),
        "asr_quality": dict(context.asr_quality),
        "representative_danmaku": [dict(item) for item in context.representative_danmaku],
        "event": {
            "title": context.title,
            "summary": context.summary,
            "category": context.category,
            "entities": list(context.entities),
            "start_ts": context.event_start.isoformat(),
            "peak_ts": context.event_peak.isoformat(),
            "end_ts": context.event_end.isoformat(),
        },
        "event_enrichment": {
            "source": context.enrichment_source,
            "evidence_ids": list(context.evidence_ids),
            "bundle_fingerprint": context.bundle_fingerprint,
        },
        "timeline": {
            "dynamic_bounds": True,
            "cross_segment": start_ts < context.segment.start_ts or end_ts > context.segment.end_ts,
            "recording_block_start": context.block_start.isoformat(),
            "recording_block_end": context.block_end.isoformat(),
            "source_segment_id": context.segment.id,
        },
        "decision": decision,
        "threshold": context.threshold,
        "input_fingerprint": context.input_fingerprint,
    }


def _event_modality_scores(
    features: Mapping[str, object],
    evidence_json: str | None,
) -> dict[str, float | None]:
    values: dict[str, list[float]] = {name: [] for name in ("danmaku", "audio", "sensevoice", "asr", "trend")}
    ticks = features.get("ticks")
    if isinstance(ticks, list):
        for tick in ticks:
            if not isinstance(tick, Mapping):
                continue
            scores = tick.get("modality_scores")
            if not isinstance(scores, Mapping):
                continue
            for name in values:
                score = _optional_score(scores.get(name))
                if score is not None:
                    values[name].append(score)
    try:
        evidence = json.loads(evidence_json or "{}")
    except (json.JSONDecodeError, TypeError):
        evidence = {}
    items = evidence.get("items") if isinstance(evidence, Mapping) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            name = item.get("type")
            metrics = item.get("metrics")
            if name in values and isinstance(metrics, Mapping):
                score = _optional_score(metrics.get("score"))
                if score is not None:
                    values[str(name)].append(score)
    return {name: _event_wide_score(scores) for name, scores in values.items()}


def _event_novelty(evidence_json: str | None) -> float:
    try:
        payload = json.loads(evidence_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return 0.0
    items = payload.get("items") if isinstance(payload, Mapping) else None
    values: list[float] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping) or item.get("type") != "asr":
                continue
            metrics = item.get("metrics")
            if not isinstance(metrics, Mapping):
                continue
            for name in ("semantic_novelty", "topic_change"):
                score = _optional_score(metrics.get(name))
                if score is not None:
                    values.append(score)
    return _event_wide_score(values) or 0.0


def _detector_clip_score(event: HotspotEvent, features: Mapping[str, object]) -> float:
    metadata = features.get("event_clip_score")
    if isinstance(metadata, Mapping):
        saved = _optional_score(metadata.get("detector_clip_score"))
        if saved is not None:
            return saved
    return _clamp(float(event.clip_score))


def _asr_quality(text: str | None) -> tuple[dict[str, object], float]:
    if not (text or "").strip():
        return {"state": "unavailable", "usable": False, "reason": "missing_transcript"}, 0.0
    quality = assess_transcript_quality(text or "")
    payload = transcript_quality_payload(quality)
    if quality.usable:
        return payload, 1.0
    return payload, 0.25


def _representative_danmaku(raw: str | None) -> tuple[Mapping[str, object], ...]:
    try:
        values = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(dict(value) for value in values if isinstance(value, Mapping))


def _segment_for_peak(segments: Sequence[RawSegment], peak_ts: datetime) -> RawSegment:
    peak_epoch = datetime_epoch(peak_ts)
    containing = [
        segment
        for segment in segments
        if segment.start_ts is not None
        and segment.end_ts is not None
        and datetime_epoch(segment.start_ts) <= peak_epoch <= datetime_epoch(segment.end_ts)
    ]
    if containing:
        return min(containing, key=lambda item: (abs(datetime_epoch(item.start_ts) - peak_epoch), item.seq))
    available = [segment for segment in segments if segment.start_ts is not None and segment.end_ts is not None]
    if not available:
        raise ValueError("录制场次没有带时间戳的分段")
    return min(
        available,
        key=lambda item: (
            min(abs(datetime_epoch(item.start_ts) - peak_epoch), abs(datetime_epoch(item.end_ts) - peak_epoch)),
            item.seq,
        ),
    )


def _clip_reason(
    context: _EventContext,
    components: Mapping[str, float],
    clip_score: float,
    decision: str,
) -> str:
    summary = (context.summary or context.title or "事件语义证据不足，需结合画面确认").strip()
    outcome = "达到候选阈值" if decision == "candidate" else "保留热点但未达到候选阈值"
    return (
        f"{summary}；完整事件多信号评分 {clip_score:.3f}，{outcome} "
        f"(热度 {components['heat']:.3f}，互动反应 {components['reaction']:.3f}，"
        f"事件完整度 {components['event_completeness']:.3f})"
    )


def _initial_candidate_status(context: _EventContext, clip_score: float) -> str:
    if context.auto_approve and clip_score >= context.auto_approve_threshold:
        return CandidateStatus.APPROVED
    if clip_score >= context.review_threshold:
        return CandidateStatus.PENDING
    return CandidateStatus.REJECTED


def _weighted_score(values: Mapping[str, float], weights: Mapping[str, float]) -> float:
    denominator = sum(weights.values())
    return sum(_clamp(values[name]) * weights[name] for name in weights) / denominator


def _weighted_available(values: Mapping[str, float | None], weights: Mapping[str, float]) -> float:
    available = [(values[name], weights[name]) for name in weights if values.get(name) is not None]
    denominator = sum(weight for _value, weight in available)
    if denominator <= 0.0:
        return 0.0
    return sum(_clamp(float(value)) * weight for value, weight in available if value is not None) / denominator


def _event_wide_score(values: Sequence[float]) -> float | None:
    if not values:
        return None
    bounded = [_clamp(value) for value in values]
    return _clamp(sum(bounded) / len(bounded) * 0.70 + max(bounded) * 0.30)


def _score_or_default(value: object, default: float) -> float:
    score = _optional_score(value)
    return score if score is not None else _clamp(default)


def _optional_score(value: object) -> float | None:
    if not isinstance(value, int | float):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return _clamp(numeric)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _json_mapping(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _epoch_like(epoch: float, reference: datetime) -> datetime:
    reference_epoch = datetime_epoch(reference)
    return reference + timedelta(seconds=epoch - reference_epoch)


def _clamp(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)
