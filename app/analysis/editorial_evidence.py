# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from sqlmodel import Session, select

from app.analysis.editorial_contracts import (
    CONTEXT_SECONDS,
    EVIDENCE_MAX_BYTES,
    EVIDENCE_MAX_CHARS,
    EXCERPT_MAX_CHARS,
    EXPANDED_CONTEXT_SECONDS,
    ContextDirection,
    EditorialEvidence,
    EditorialReviewInputV1,
    EditorialTarget,
    EvidenceDetail,
    EvidenceState,
    EvidenceType,
    TimeRange,
)
from app.analysis.timeline import datetime_epoch
from app.analysis.transcript_windows import extract_transcript_window
from app.analysis.transcription.content import refined_transcript_text
from app.analysis.transcription.quality import assess_transcript_quality
from app.db.entities import HotspotEvent, RawSegment, RecordingSession, Transcript

EVIDENCE_POLICY_VERSION = "bounded-evidence-v3"
AUDIENCE_PROFILE = "面向不了解该场次前后经过的普通观众，证据不足时保留人工复核。"
PREFERENCE_VERSION = "general-audience-v1"
_TYPES = {"asr", "context_asr", "danmaku", "audio", "sensevoice", "trend", "visual"}
_STATES = {"available", "degraded", "missing", "disabled", "error", "pending"}


class EvidenceContextUnavailable(ValueError):
    """记录中没有覆盖峰值的连续录制范围，不能生成虚假输入窗口。"""


def _text(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _object(raw: str | None) -> dict[str, object]:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("持久化证据必须是 JSON 对象")
    return value


def _identity(value: object, prefix: str) -> str:
    text = _text(value, 257)
    if text and len(text) <= 256 and not re.search(r"[\s\x00-\x1f\x7f]", text):
        return text
    return f"{prefix}:{hashlib.sha256(str(value).encode()).hexdigest()[:24]}"


def _detail(raw: Mapping[str, object]) -> EvidenceDetail:
    detail = raw.get("detail")
    detail = detail if isinstance(detail, Mapping) else {}
    metrics = raw.get("metrics")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    selected: dict[str, float] = {}
    for key, value in metrics.items():
        if len(selected) >= 24:
            break
        if (
            isinstance(key, str)
            and 0 < len(key) <= 64
            and type(value) in {int, float}
            and -sys.float_info.max <= value <= sys.float_info.max
        ):
            selected[key] = float(value)
    quality = detail.get("quality")
    state = quality.get("state") if isinstance(quality, Mapping) else None
    return EvidenceDetail(
        backend=_text(detail.get("backend"), 128) or None,
        model=_text(detail.get("model_id"), 256) or None,
        quality=_text(state, 240) or None,
        metrics=selected,
    )


def _role(start: float, end: float, bounds: TimeRange) -> str:
    if end <= bounds.start_s:
        return "before"
    if start >= bounds.end_s:
        return "after"
    if start >= bounds.start_s and end <= bounds.end_s:
        return "event"
    return "mixed"


def _timing(raw: Mapping[str, object], origin: float, window: TimeRange, bounds: TimeRange) -> dict[str, object] | None:
    start, end = raw.get("start_ts"), raw.get("end_ts")
    if start is None and end is None:
        return dict(start_s=None, end_s=None, timing_precision="unknown", context_role="unknown")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        lower = datetime_epoch(datetime.fromisoformat(start)) - origin
        upper = datetime_epoch(datetime.fromisoformat(end)) - origin
    except ValueError:
        return None
    # 已有文本没有内部时间定位，不能截取时间却保留范围外正文。
    if lower < window.start_s or upper > window.end_s or upper < lower:
        return None
    return dict(start_s=lower, end_s=upper, timing_precision="event", context_role=_role(lower, upper, bounds))


def _recording_ranges(segments: list[RawSegment], origin: float) -> list[TimeRange]:
    pairs = sorted(
        (datetime_epoch(item.start_ts) - origin, datetime_epoch(item.end_ts) - origin)
        for item in segments
        if item.start_ts is not None and item.end_ts is not None
    )
    merged: list[tuple[float, float]] = []
    for start, end in pairs:
        if start < 0 or end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return [TimeRange(start_s=start, end_s=end) for start, end in merged]


def _context(
    db: Session, segments: list[RawSegment], origin: float, window: TimeRange, bounds: TimeRange, limit: int
) -> tuple[list[EditorialEvidence], bool]:
    result: list[EditorialEvidence] = []
    truncated = False
    for segment in segments:
        if segment.id is None or segment.start_ts is None or segment.end_ts is None:
            continue
        start, end = datetime_epoch(segment.start_ts) - origin, datetime_epoch(segment.end_ts) - origin
        if end <= window.start_s or start >= window.end_s:
            continue
        if len(result) >= 128:
            truncated = True
            break
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment.id)).first()
        if transcript is None:
            continue
        lower, upper = max(start, window.start_s), min(end, window.end_s)
        identity = f"transcript:{transcript.id or segment.id}"
        timing: dict[str, object] = dict(start_s=None, end_s=None, timing_precision="unknown", context_role="unknown")
        problem = None
        try:
            selected = extract_transcript_window(
                transcript.final_text,
                transcript.words_json,
                start_s=lower - start,
                end_s=upper - start,
                duration_s=end - start,
            )
        except ValueError:
            selected = None
            problem = "词级时间数据无效，未采用比例裁剪文本"
        text = ""
        if selected is not None and selected.precise and selected.words:
            word_start = start + min(float(word["start"]) for word in selected.words)
            word_end = start + max(float(word["end"]) for word in selected.words)
            if lower <= word_start <= word_end <= upper:
                text = selected.text
                timing = dict(
                    start_s=word_start,
                    end_s=word_end,
                    timing_precision="word",
                    context_role=_role(word_start, word_end, bounds),
                )
            else:
                problem = "词条跨越输入窗口边界，无法精确定位窗口内全文"
        elif selected is not None and lower == start and upper == end:
            text = refined_transcript_text(transcript) or transcript.final_text or ""
            timing = dict(start_s=start, end_s=end, timing_precision="segment", context_role=_role(start, end, bounds))
        elif problem is None:
            problem = "缺少词级时间，无法定位局部窗口文本"
        truncated = truncated or len(text) > limit
        text = _text(text, limit)
        quality = assess_transcript_quality(text) if text else None
        result.append(
            EditorialEvidence(
                evidence_id=identity,
                type="context_asr",
                state=("available" if quality.usable else "degraded") if quality is not None else "missing",
                excerpts=[text] if text else [],
                detail=EvidenceDetail(
                    segment_id=segment.id,
                    quality=problem or ("可用转写" if quality and quality.usable else "转写质量较低"),
                ),
                **timing,
            )
        )
    return result, truncated


def build_event_input(
    db: Session,
    event: HotspotEvent,
    *,
    context_s: float,
    max_evidence_chars: int,
    max_excerpt_chars: int,
    context_direction: ContextDirection | None = None,
) -> EditorialReviewInputV1:
    """从现有录制元数据和证据构造有界输入，保留缺失时间和录制缺口。"""
    session = db.get(RecordingSession, event.session_id)
    if session is None or event.id is None:
        raise EvidenceContextUnavailable("事件缺少有效录制场次")
    segments = list(
        db.exec(select(RawSegment).where(RawSegment.session_id == event.session_id).order_by(RawSegment.seq)).all()
    )
    # 回放或缓存流的媒体可能早于应用启动；显式保留真实时间原点，不移动业务记录。
    origin = min(
        [
            datetime_epoch(session.started_at),
            *(datetime_epoch(item.start_ts) for item in segments if item.start_ts is not None),
        ]
    )
    raw_start = datetime_epoch(event.start_ts) - origin
    end = datetime_epoch(event.end_ts) - origin
    peak = datetime_epoch(event.peak_ts) - origin
    if peak < 0 or end <= 0:
        raise EvidenceContextUnavailable("事件峰值不在已记录的场次时间内")
    bounds = TimeRange(start_s=max(0.0, raw_start), end_s=end)
    ranges = _recording_ranges(segments, origin)
    block = next((item for item in ranges if item.start_s <= peak < item.end_s), None)
    if block is None:
        raise EvidenceContextUnavailable("峰值不在任何已记录的连续录制范围")
    window = TimeRange(
        start_s=max(block.start_s, bounds.start_s - (CONTEXT_SECONDS if context_direction == "after" else context_s)),
        end_s=min(block.end_s, bounds.end_s + (CONTEXT_SECONDS if context_direction == "before" else context_s)),
    )
    evidence: list[EditorialEvidence] = []
    complete_event = raw_start >= 0 and window.contains(bounds)
    truncated = not complete_event
    raw_items = _object(event.evidence_json).get("items", [])
    if not isinstance(raw_items, list):
        raise ValueError("事件证据 items 必须为数组")
    truncated = truncated or len(raw_items) > 128
    for index, raw in enumerate(raw_items[:128]):
        if not isinstance(raw, dict) or raw.get("type") not in _TYPES:
            continue
        timing = _timing(raw, origin, window, bounds)
        if timing is None:
            truncated = True
            continue
        if not complete_event and timing["timing_precision"] == "unknown":
            # 聚合证据不能定位到连续块内部，保留它会把缺口外内容混入输入。
            continue
        detail = _detail(raw)
        state = detail.quality or raw.get("state") or "available"
        score = raw.get("score", detail.metrics.get("score"))
        if not detail.quality and not raw.get("state") and raw["type"] in {"asr", "context_asr"}:
            if type(score) in {int, float} and score < 0.5:
                state = "missing" if score <= 0 else "degraded"
        state = "missing" if state == "unavailable" else state
        if state not in _STATES:
            state = "degraded"
        values = raw.get("excerpts", [])
        if isinstance(values, list):
            truncated = (
                truncated
                or len(values) > 8
                or any(isinstance(item, str) and len(item) > max_excerpt_chars for item in values[:8])
            )
        texts = (
            [_text(item, min(max_excerpt_chars, EXCERPT_MAX_CHARS)) for item in values[:8]]
            if isinstance(values, list)
            else []
        )
        texts = [item for item in texts if item][:8] if state in {"available", "degraded"} else []
        if not texts and not detail.metrics and state in {"available", "degraded"}:
            state = "missing"
        evidence.append(
            EditorialEvidence(
                evidence_id=_identity(raw.get("id"), f"hotspot:{event.id}:{index}"),
                type=cast("EvidenceType", raw["type"]),
                state=cast("EvidenceState", state),
                excerpts=texts,
                detail=detail,
                **timing,
            )
        )
    existing = {text.casefold() for item in evidence for text in item.excerpts}
    event_text = _text(event.transcript_text, min(max_excerpt_chars, EXCERPT_MAX_CHARS))
    truncated = truncated or bool(event.transcript_text and len(event.transcript_text) > max_excerpt_chars)
    if complete_event and event_text and event_text.casefold() not in existing:
        quality = assess_transcript_quality(event_text)
        evidence.append(
            EditorialEvidence(
                evidence_id=f"hotspot:{event.id}:event_asr",
                type="asr",
                state="available" if quality.usable else "degraded",
                start_s=None,
                end_s=None,
                timing_precision="unknown",
                context_role="unknown",
                excerpts=[event_text],
            )
        )
    context, context_truncated = _context(db, segments, origin, window, bounds, max_excerpt_chars)
    evidence.extend(context)
    truncated = truncated or context_truncated
    representatives = json.loads(event.representative_danmaku_json or "[]")
    if not isinstance(representatives, list):
        raise ValueError("代表弹幕必须为数组")
    truncated = truncated or len(representatives) > 128
    for index, raw in enumerate(representatives[:128] if complete_event else []):
        if not isinstance(raw, dict) or not (text := _text(raw.get("text"), min(max_excerpt_chars, EXCERPT_MAX_CHARS))):
            continue
        count = raw.get("count")
        truncated = truncated or len(raw["text"]) > max_excerpt_chars
        evidence.append(
            EditorialEvidence(
                evidence_id=f"hotspot:{event.id}:danmaku:{index}",
                type="danmaku",
                state="available",
                start_s=None,
                end_s=None,
                timing_precision="unknown",
                context_role="unknown",
                excerpts=[text],
                detail=EvidenceDetail(
                    count=count if type(count) is int and count >= 0 else None, role=_text(raw.get("role"), 64) or None
                ),
            )
        )
    # 优先保留实际文字和事件证据；完整序列化大小包含全部详情与时间字段。
    evidence.sort(key=lambda item: (not bool(item.excerpts), item.context_role not in {"event", "mixed"}))
    selected: list[EditorialEvidence] = []
    used: set[str] = set()
    remaining = max_evidence_chars
    base = dict(
        time_origin_epoch_s=origin,
        target=EditorialTarget(
            kind="hotspot",
            session_id=event.session_id,
            target_id=event.id,
            target_key=_identity(event.event_key, "event"),
            bounds=bounds,
            peak_s=peak,
        ),
        window=window,
        recording_ranges=[block],
        truncated=truncated,
        audience_profile=AUDIENCE_PROFILE,
        preference_version=PREFERENCE_VERSION,
    )
    serialized_base = EditorialReviewInputV1(**base, evidence=[]).model_dump(mode="json")
    for item in evidence:
        if item.evidence_id in used:
            continue
        used.add(item.evidence_id)
        length = sum(map(len, item.excerpts))
        tentative = dict(
            serialized_base, truncated=True, evidence=[part.model_dump(mode="json") for part in [*selected, item]]
        )
        if (
            len(selected) >= 128
            or length > remaining
            or len(json.dumps(tentative, ensure_ascii=False).encode()) > EVIDENCE_MAX_BYTES
        ):
            base["truncated"] = True
            continue
        remaining -= length
        selected.append(item)
    return EditorialReviewInputV1(**base, evidence=selected)


def build_segment_input(
    db: Session,
    segment_id: int,
    *,
    start_ts: datetime,
    end_ts: datetime,
    peak_ts: datetime,
    metrics: Mapping[str, float],
    danmaku: list[dict[str, object]],
    context_direction: ContextDirection | None = None,
) -> EditorialReviewInputV1:
    """旧分段使用真实连续录制和精确文本，复用统一证据契约。"""
    segment = db.get(RawSegment, segment_id)
    session = db.get(RecordingSession, segment.session_id) if segment is not None else None
    if segment is None or session is None:
        raise EvidenceContextUnavailable("分段或场次不存在")
    segments = list(db.exec(select(RawSegment).where(RawSegment.session_id == session.id)).all())
    origin = min(
        [
            datetime_epoch(session.started_at),
            *(datetime_epoch(item.start_ts) for item in segments if item.start_ts is not None),
        ]
    )
    bounds = TimeRange(start_s=datetime_epoch(start_ts) - origin, end_s=datetime_epoch(end_ts) - origin)
    peak = datetime_epoch(peak_ts) - origin
    block = next((item for item in _recording_ranges(segments, origin) if item.start_s <= peak < item.end_s), None)
    if block is None:
        raise EvidenceContextUnavailable("分段峰值没有连续录制范围")
    window = TimeRange(
        start_s=max(
            block.start_s,
            bounds.start_s - (EXPANDED_CONTEXT_SECONDS if context_direction in {"before", "both"} else 0.0),
        ),
        end_s=min(
            block.end_s,
            bounds.end_s + (EXPANDED_CONTEXT_SECONDS if context_direction in {"after", "both"} else 0.0),
        ),
    )
    evidence, truncated = _context(db, segments, origin, window, bounds, EXCERPT_MAX_CHARS)
    for kind, names in (("audio", {"volume"}), ("danmaku", {"danmaku", "danmaku_sentiment"})):
        selected = {name: float(value) for name, value in metrics.items() if name in names}
        if selected:
            evidence.append(
                EditorialEvidence(
                    evidence_id=f"segment:{segment_id}:{kind}",
                    type=kind,
                    state="available",
                    start_s=None,
                    end_s=None,
                    timing_precision="unknown",
                    context_role="unknown",
                    excerpts=[],
                    detail=EvidenceDetail(metrics=selected),
                )
            )
    for index, item in enumerate(danmaku[:32] if window.contains(bounds) else []):
        if not (text := _text(item.get("text"), EXCERPT_MAX_CHARS)):
            continue
        evidence.append(
            EditorialEvidence(
                evidence_id=f"segment:{segment_id}:danmaku:{index}",
                type="danmaku",
                state="available",
                start_s=None,
                end_s=None,
                timing_precision="unknown",
                context_role="unknown",
                excerpts=[text],
            )
        )
    source = EditorialReviewInputV1(
        time_origin_epoch_s=origin,
        target=EditorialTarget(
            kind="segment",
            session_id=session.id,
            target_id=segment_id,
            target_key=f"segment:{segment_id}:{peak:.6f}",
            bounds=bounds,
            peak_s=peak,
        ),
        window=window,
        recording_ranges=[block],
        truncated=truncated or not window.contains(bounds),
        audience_profile=AUDIENCE_PROFILE,
        preference_version=PREFERENCE_VERSION,
        evidence=[],
    )
    selected_evidence: list[EditorialEvidence] = []
    remaining = EVIDENCE_MAX_CHARS
    for item in evidence:
        length = sum(map(len, item.excerpts))
        if len(selected_evidence) >= 128 or length > remaining:
            source = source.model_copy(update={"truncated": True})
            continue
        serialized = dict(
            source.model_dump(mode="json"),
            evidence=[part.model_dump(mode="json") for part in [*selected_evidence, item]],
        )
        if len(json.dumps(serialized, ensure_ascii=False).encode()) > EVIDENCE_MAX_BYTES:
            source = source.model_copy(update={"truncated": True})
            continue
        selected_evidence.append(item)
        remaining -= length
    return EditorialReviewInputV1.model_validate(dict(source.model_dump(), evidence=selected_evidence))
