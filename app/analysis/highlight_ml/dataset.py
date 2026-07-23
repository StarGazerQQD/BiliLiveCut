"""从主程序真实审核数据构建无泄漏高光训练集。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.analysis.highlight_ml.context import AudioLoader, load_feature_context
from app.analysis.highlight_ml.features import extract_feature_record
from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA, FeatureSchema
from app.analysis.highlight_ml.types import BlindReviewItem, DatasetBundle, LabeledSample
from app.db.models import (
    HighlightCandidate,
    HighlightEvent,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    ThresholdFeedback,
)


@dataclass(frozen=True, slots=True)
class _LabelDecision:
    candidate_id: int
    label: int
    source: str
    observed_at: datetime


def _normalized_datetime(value: datetime) -> datetime:
    """把数据库时间统一为可排序的无时区 UTC。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _event_label(status: str) -> int | None:
    """仅把语义明确的审核状态映射为高光二分类标签。"""
    if status in ReviewStatus.POSITIVE:
        return 1
    if status in {ReviewStatus.NOT_EXCITING, ReviewStatus.REJECTED}:
        return 0
    return None


def _collect_labels(db: Session) -> dict[int, _LabelDecision]:
    decisions: dict[int, _LabelDecision] = {}
    for event in db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id.is_not(None))).all():
        if event.candidate_id is None:
            continue
        label = _event_label(event.review_status)
        if label is None or event.review_by != "manual":
            continue
        decisions[event.candidate_id] = _LabelDecision(
            candidate_id=event.candidate_id,
            label=label,
            source=f"highlight_event:{event.review_status}",
            observed_at=_normalized_datetime(event.updated_at),
        )

    feedback_rows = db.exec(
        select(ThresholdFeedback)
        .where(ThresholdFeedback.action.in_(["approved", "rejected"]))
        .order_by(ThresholdFeedback.created_at, ThresholdFeedback.id)
    ).all()
    for feedback in feedback_rows:
        label = 1 if feedback.action == "approved" else 0
        decision = _LabelDecision(
            candidate_id=feedback.candidate_id,
            label=label,
            source=f"threshold_feedback:{feedback.action}",
            observed_at=_normalized_datetime(feedback.created_at),
        )
        previous = decisions.get(feedback.candidate_id)
        if previous is None or decision.observed_at >= previous.observed_at:
            decisions[feedback.candidate_id] = decision
    return decisions


def _candidate_segment_map(
    db: Session,
    candidate_ids: set[int],
) -> tuple[dict[int, int], dict[int, HighlightCandidate]]:
    candidates = {
        candidate.id: candidate
        for candidate in db.exec(select(HighlightCandidate).where(HighlightCandidate.id.in_(candidate_ids))).all()
        if candidate.id is not None
    }
    mapped: dict[int, int] = {}
    events = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id.in_(candidate_ids))).all()
    for event in events:
        if event.candidate_id is not None and event.segment_id is not None:
            mapped[event.candidate_id] = event.segment_id

    unresolved = candidate_ids.difference(mapped)
    if not unresolved:
        return mapped, candidates
    session_ids = {candidates[candidate_id].session_id for candidate_id in unresolved if candidate_id in candidates}
    segments_by_session: dict[int, list[RawSegment]] = {}
    if session_ids:
        for segment in db.exec(
            select(RawSegment)
            .where(RawSegment.session_id.in_(session_ids))
            .order_by(RawSegment.session_id, RawSegment.seq)
        ).all():
            segments_by_session.setdefault(segment.session_id, []).append(segment)
    for candidate_id in sorted(unresolved):
        candidate = candidates.get(candidate_id)
        if candidate is None:
            continue
        peak = _normalized_datetime(candidate.peak_ts)
        matches = [
            segment
            for segment in segments_by_session.get(candidate.session_id, [])
            if segment.id is not None
            and segment.start_ts is not None
            and segment.end_ts is not None
            and _normalized_datetime(segment.start_ts) <= peak <= _normalized_datetime(segment.end_ts)
        ]
        if matches:
            mapped[candidate_id] = min(matches, key=lambda segment: segment.seq).id  # type: ignore[assignment]
    return mapped, candidates


def _blind_review_queue(
    db: Session,
    excluded_segment_ids: set[int],
    *,
    limit: int,
    seed: int,
) -> tuple[BlindReviewItem, ...]:
    if limit <= 0:
        return ()
    eligible = [
        segment
        for segment in db.exec(select(RawSegment).order_by(RawSegment.session_id, RawSegment.seq)).all()
        if segment.id is not None
        and segment.id not in excluded_segment_ids
        and segment.start_ts is not None
        and segment.end_ts is not None
    ]
    by_session: dict[int, list[RawSegment]] = {}
    for segment in eligible:
        by_session.setdefault(segment.session_id, []).append(segment)
    rng = random.Random(seed)
    for segments in by_session.values():
        rng.shuffle(segments)

    selected: list[BlindReviewItem] = []
    session_ids = sorted(by_session)
    while len(selected) < limit and session_ids:
        next_round: list[int] = []
        for session_id in session_ids:
            segments = by_session[session_id]
            if not segments:
                continue
            segment = segments.pop()
            selected.append(
                BlindReviewItem(
                    segment_id=segment.id,  # type: ignore[arg-type]
                    session_id=segment.session_id,
                    start_ts=_normalized_datetime(segment.start_ts),  # type: ignore[arg-type]
                    end_ts=_normalized_datetime(segment.end_ts),  # type: ignore[arg-type]
                )
            )
            if segments:
                next_round.append(session_id)
            if len(selected) >= limit:
                break
        session_ids = next_round
    return tuple(selected)


def build_labeled_dataset(
    db: Session,
    *,
    schema: FeatureSchema = DEFAULT_FEATURE_SCHEMA,
    audio_loader: AudioLoader | None = None,
    blind_review_limit: int = 0,
    blind_review_seed: int = 0,
) -> DatasetBundle:
    """构建只含明确人工标签的监督集，并附带未标注盲审队列。"""
    labels = _collect_labels(db)
    segment_map, candidates = _candidate_segment_map(db, set(labels))
    session_ids = {candidate.session_id for candidate in candidates.values()}
    sessions = {
        recording.id: recording
        for recording in db.exec(select(RecordingSession).where(RecordingSession.id.in_(session_ids))).all()
        if recording.id is not None
    }

    selected_by_segment: dict[int, tuple[int, _LabelDecision, HighlightCandidate]] = {}
    for candidate_id, decision in labels.items():
        segment_id = segment_map.get(candidate_id)
        candidate = candidates.get(candidate_id)
        if segment_id is None or candidate is None or candidate.session_id not in sessions:
            continue
        previous = selected_by_segment.get(segment_id)
        if previous is None or decision.observed_at >= previous[1].observed_at:
            selected_by_segment[segment_id] = (candidate_id, decision, candidate)

    samples: list[LabeledSample] = []
    used_segment_ids: set[int] = set()
    ordered = sorted(selected_by_segment.items(), key=lambda item: (item[1][1].observed_at, item[1][0]))
    for segment_id, (candidate_id, decision, candidate) in ordered:
        context = load_feature_context(db, segment_id, audio_loader=audio_loader)
        if context.session_id != candidate.session_id:
            continue
        record = extract_feature_record(context, schema)
        used_segment_ids.add(segment_id)
        samples.append(
            LabeledSample(
                sample_id=f"candidate:{candidate_id}",
                segment_id=segment_id,
                session_id=candidate.session_id,
                room_id=sessions[candidate.session_id].room_id,
                segment_start_ts=context.start_ts,
                label=decision.label,
                label_source=decision.source,
                observed_at=decision.observed_at,
                features=record,
            )
        )

    blind_queue = _blind_review_queue(
        db,
        used_segment_ids,
        limit=blind_review_limit,
        seed=blind_review_seed,
    )
    return DatasetBundle(
        schema_version=schema.version,
        schema_fingerprint=schema.fingerprint,
        feature_names=schema.feature_names,
        samples=tuple(samples),
        blind_review_queue=blind_queue,
    )
