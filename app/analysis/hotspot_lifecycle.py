"""HotspotEvent 的事务内更新、合并、确认与代表弹幕选择。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from sqlmodel import Session, select

from app.analysis.timeline import align_danmaku_window, datetime_epoch, select_representative_danmaku
from app.core.config import settings
from app.db.entities import (
    Danmaku,
    DanmakuType,
    HotspotEvent,
    HotspotStatus,
    RawSegment,
    RecordingSession,
    SessionStatus,
)
from app.db.entities.base import utcnow
from app.db.hotspot_store import get_or_create_hotspot_event

_LIFECYCLE_VERSION = 1
_WORD_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")
_REACTION_TERMS = {"?", "??", "???", "666", "6666", "哈哈", "哈哈哈", "卧槽", "笑死", "草", "绷"}
_ACTIVE_STATUSES = {HotspotStatus.PROVISIONAL, HotspotStatus.ENRICHING, HotspotStatus.CONFIRMED}
_LIVE_SESSION_STATUSES = {
    SessionStatus.STARTING,
    SessionStatus.RECORDING,
    SessionStatus.RECONNECTING,
    SessionStatus.RECONNECTED,
    SessionStatus.STOPPING,
    SessionStatus.FINALIZING,
}


@dataclass(frozen=True, slots=True)
class HotspotLifecycleConfig:
    """事件协调的集中阈值。"""

    merge_gap_s: float = 30.0
    confirm_delay_s: float = 60.0
    semantic_overlap_threshold: float = 0.20
    recording_gap_tolerance_s: float = 1.0

    def __post_init__(self) -> None:
        """校验事件协调阈值。"""
        if not 0.0 <= self.merge_gap_s <= 120.0:
            raise ValueError("merge_gap_s 必须在 0~120 秒之间")
        if not 0.0 <= self.confirm_delay_s <= 600.0:
            raise ValueError("confirm_delay_s 必须在 0~600 秒之间")
        if not 0.0 <= self.semantic_overlap_threshold <= 1.0:
            raise ValueError("semantic_overlap_threshold 必须在 0~1 之间")
        if not 0.0 <= self.recording_gap_tolerance_s <= 10.0:
            raise ValueError("recording_gap_tolerance_s 必须在 0~10 秒之间")

    @classmethod
    def from_settings(cls) -> HotspotLifecycleConfig:
        """从应用设置构建事件协调配置。"""
        return cls(
            merge_gap_s=settings.hotspot_event_merge_gap_s,
            confirm_delay_s=settings.hotspot_event_confirm_delay_s,
            semantic_overlap_threshold=settings.hotspot_event_semantic_overlap_threshold,
            recording_gap_tolerance_s=settings.hotspot_recording_gap_tolerance_s,
        )


def reconcile_hotspot_events(
    db: Session,
    payloads: Sequence[Mapping[str, object]],
    *,
    expected_session_id: int,
    observed_through: datetime | None = None,
    config: HotspotLifecycleConfig | None = None,
) -> list[int]:
    """在调用方事务中更新、合并热点，并确认已经稳定结束的事件。

    每个传入 ``event_key`` 都会保留为真实记录。跨分段合并时，较晚记录变为
    ``merged`` 别名并指向最早主事件，使已排队的局部 ASR 仍可沿原键找到主事件。
    """
    cfg = config or HotspotLifecycleConfig.from_settings()
    canonical_ids: list[int] = []
    touched: dict[int, HotspotEvent] = {}
    for payload in payloads:
        session_id = _required_int(payload, "session_id")
        if session_id != expected_session_id:
            raise ValueError(
                f"hotspot lifecycle source mismatch: expected_session={expected_session_id} actual_session={session_id}"
            )
        incoming, created = get_or_create_hotspot_event(
            db,
            event_key=_required_str(payload, "event_key"),
            session_id=session_id,
            start_ts=_required_datetime(payload, "start_ts"),
            peak_ts=_required_datetime(payload, "peak_ts"),
            end_ts=_required_datetime(payload, "end_ts"),
            status=HotspotStatus.PROVISIONAL,
            heat_score=_required_score(payload, "heat_score"),
            clip_score=_required_score(payload, "clip_score"),
            semantic_confidence=_required_score(payload, "semantic_confidence"),
            evidence_coverage=_required_score(payload, "evidence_coverage"),
            features_json=_optional_str(payload.get("features_json")),
            evidence_json=_optional_str(payload.get("evidence_json")),
            transcript_text=_optional_str(payload.get("transcript_text")),
        )
        root = resolve_merged_hotspot_event(db, incoming)
        if root.status == HotspotStatus.DISMISSED or root.candidate_id is not None:
            if root.id is not None:
                canonical_ids.append(root.id)
            continue
        if not created and _payload_extends_event(root, payload):
            root.status = HotspotStatus.ENRICHING
        _merge_payload_into_event(root, payload, observed_through=observed_through)

        candidates = _merge_candidates(db, root, cfg)
        canonical = min(
            [root, *candidates],
            key=lambda event: (datetime_epoch(event.start_ts), event.id or 0),
        )
        for event in [root, *candidates]:
            if event is canonical:
                continue
            _merge_event_into(db, canonical, event, observed_through=observed_through)
        _refresh_representative_danmaku(db, canonical)
        db.add(canonical)
        if canonical.id is None:
            raise RuntimeError("HotspotEvent 协调后缺少主键")
        touched[canonical.id] = canonical
        canonical_ids.append(canonical.id)

    confirmed = _confirm_stable_events(
        db,
        expected_session_id,
        observed_through=observed_through,
        config=cfg,
    )
    for event in confirmed:
        if event.id is not None:
            touched[event.id] = event
    if touched:
        logger.info(
            "hotspot_lifecycle_reconciled: session_id={} payloads={} active_events={} confirmed={}",
            expected_session_id,
            len(payloads),
            len(touched),
            len(confirmed),
        )
    return canonical_ids


def resolve_merged_hotspot_event(db: Session, event: HotspotEvent) -> HotspotEvent:
    """解析 merged 别名到最终主事件，并在事务内压缩指向链。"""
    path: list[HotspotEvent] = []
    current = event
    seen: set[int] = set()
    while current.status == HotspotStatus.MERGED and current.merged_into_id is not None:
        if current.id is not None:
            if current.id in seen:
                raise RuntimeError(f"HotspotEvent 合并链存在环: hotspot_id={current.id}")
            seen.add(current.id)
        path.append(current)
        target = db.get(HotspotEvent, current.merged_into_id)
        if target is None or target.session_id != event.session_id:
            raise RuntimeError(
                f"HotspotEvent 合并目标无效: hotspot_id={current.id} merged_into_id={current.merged_into_id}"
            )
        current = target
    if current.id is None:
        raise RuntimeError("HotspotEvent 主事件缺少主键")
    for alias in path:
        if alias.merged_into_id != current.id:
            alias.merged_into_id = current.id
            alias.updated_at = utcnow()
            db.add(alias)
    return current


def _merge_candidates(
    db: Session,
    event: HotspotEvent,
    config: HotspotLifecycleConfig,
) -> list[HotspotEvent]:
    statement = select(HotspotEvent).where(
        HotspotEvent.session_id == event.session_id,
        HotspotEvent.status.in_(_ACTIVE_STATUSES),
        HotspotEvent.candidate_id.is_(None),
        HotspotEvent.id != event.id,
    )
    candidates: list[HotspotEvent] = []
    for candidate in db.exec(statement).all():
        if _events_should_merge(db, event, candidate, config):
            candidates.append(candidate)
    return candidates


def _events_should_merge(
    db: Session,
    left: HotspotEvent,
    right: HotspotEvent,
    config: HotspotLifecycleConfig,
) -> bool:
    gap_s = _interval_gap_s(left.start_ts, left.end_ts, right.start_ts, right.end_ts)
    if gap_s > config.merge_gap_s:
        return False
    union_start = left.start_ts if datetime_epoch(left.start_ts) <= datetime_epoch(right.start_ts) else right.start_ts
    union_end = left.end_ts if datetime_epoch(left.end_ts) >= datetime_epoch(right.end_ts) else right.end_ts
    if not _recording_is_contiguous(
        db,
        left.session_id,
        union_start,
        union_end,
        tolerance_s=config.recording_gap_tolerance_s,
    ):
        return False
    if _interval_overlap_s(left.start_ts, left.end_ts, right.start_ts, right.end_ts) > 0.0:
        return True

    left_terms = _event_terms(left)
    right_terms = _event_terms(right)
    if left_terms and right_terms:
        return _jaccard(left_terms, right_terms) >= config.semantic_overlap_threshold

    shared_signals = _evidence_types(left.evidence_json) & _evidence_types(right.evidence_json)
    return bool(shared_signals) and gap_s <= min(config.merge_gap_s, settings.hotspot_detector_tick_s)


def _merge_payload_into_event(
    event: HotspotEvent,
    payload: Mapping[str, object],
    *,
    observed_through: datetime | None,
) -> None:
    incoming_start = _required_datetime(payload, "start_ts")
    incoming_peak = _required_datetime(payload, "peak_ts")
    incoming_end = _required_datetime(payload, "end_ts")
    incoming_heat = _required_score(payload, "heat_score")
    incoming_clip = _required_score(payload, "clip_score")
    detector_clip = max(_detector_clip_value(event), incoming_clip)
    if max(incoming_heat, incoming_clip) > max(event.heat_score, _detector_clip_value(event)):
        event.peak_ts = incoming_peak
    event.start_ts = _earlier(event.start_ts, incoming_start)
    event.end_ts = _later(event.end_ts, incoming_end)
    event.heat_score = max(event.heat_score, incoming_heat)
    event.clip_score = max(event.clip_score, incoming_clip)
    event.semantic_confidence = max(
        event.semantic_confidence,
        _required_score(payload, "semantic_confidence"),
    )
    event.evidence_coverage = max(
        event.evidence_coverage,
        _required_score(payload, "evidence_coverage"),
    )
    event.features_json = _merge_features_json(
        event.features_json,
        _optional_str(payload.get("features_json")),
        observed_through=observed_through,
    )
    merged_features = _json_dict(event.features_json)
    clip_metadata = merged_features.get("event_clip_score")
    if isinstance(clip_metadata, Mapping):
        updated_metadata = dict(clip_metadata)
        updated_metadata["detector_clip_score"] = detector_clip
        merged_features["event_clip_score"] = updated_metadata
        event.features_json = json.dumps(
            merged_features,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    event.evidence_json = _merge_evidence_json(event.evidence_json, _optional_str(payload.get("evidence_json")))
    event.transcript_text = _merge_text(event.transcript_text, _optional_str(payload.get("transcript_text")))
    event.updated_at = utcnow()


def _merge_event_into(
    db: Session,
    canonical: HotspotEvent,
    source: HotspotEvent,
    *,
    observed_through: datetime | None,
) -> None:
    if canonical.id is None or source.id is None:
        raise RuntimeError("HotspotEvent 合并前缺少主键")
    if canonical.session_id != source.session_id:
        raise ValueError("禁止跨录制场次合并 HotspotEvent")
    canonical_status = canonical.status
    source_status = source.status
    canonical_bounds = (canonical.start_ts, canonical.end_ts)
    source_bounds = (source.start_ts, source.end_ts)
    payload: dict[str, object] = {
        "start_ts": source.start_ts,
        "peak_ts": source.peak_ts,
        "end_ts": source.end_ts,
        "heat_score": source.heat_score,
        "clip_score": _detector_clip_value(source),
        "semantic_confidence": source.semantic_confidence,
        "evidence_coverage": source.evidence_coverage,
        "features_json": source.features_json,
        "evidence_json": source.evidence_json,
        "transcript_text": source.transcript_text,
    }
    _merge_payload_into_event(canonical, payload, observed_through=observed_through)
    canonical.status = _merged_active_status(
        canonical_status,
        canonical_bounds,
        source_status,
        source_bounds,
    )
    source.status = HotspotStatus.MERGED
    source.merged_into_id = canonical.id
    source.updated_at = utcnow()
    db.add(source)
    logger.info(
        "hotspot_merged: source_id={} target_id={} session_id={}",
        source.id,
        canonical.id,
        canonical.session_id,
    )


def _detector_clip_value(event: HotspotEvent) -> float:
    """在事件级成片评分覆盖 ``clip_score`` 后仍返回检测器原始先验。"""
    metadata = _json_dict(event.features_json).get("event_clip_score")
    if isinstance(metadata, Mapping):
        value = metadata.get("detector_clip_score")
        if isinstance(value, int | float):
            return max(0.0, min(float(value), 1.0))
    return event.clip_score


def _merged_active_status(
    left_status: str,
    left_bounds: tuple[datetime, datetime],
    right_status: str,
    right_bounds: tuple[datetime, datetime],
) -> str:
    """根据合并前状态与边界决定主事件是否需要重新 enrichment。"""
    if left_status == right_status == HotspotStatus.CONFIRMED:
        return HotspotStatus.CONFIRMED
    if left_status == HotspotStatus.CONFIRMED or right_status == HotspotStatus.CONFIRMED:
        confirmed_bounds = left_bounds if left_status == HotspotStatus.CONFIRMED else right_bounds
        pending_bounds = right_bounds if left_status == HotspotStatus.CONFIRMED else left_bounds
        extends_confirmed = datetime_epoch(pending_bounds[0]) < datetime_epoch(confirmed_bounds[0]) or datetime_epoch(
            pending_bounds[1]
        ) > datetime_epoch(confirmed_bounds[1])
        return HotspotStatus.ENRICHING if extends_confirmed else HotspotStatus.CONFIRMED
    if HotspotStatus.ENRICHING in {left_status, right_status}:
        return HotspotStatus.ENRICHING
    return HotspotStatus.PROVISIONAL


def _confirm_stable_events(
    db: Session,
    session_id: int,
    *,
    observed_through: datetime | None,
    config: HotspotLifecycleConfig,
) -> list[HotspotEvent]:
    if observed_through is None:
        return []
    session = db.get(RecordingSession, session_id)
    force = session is not None and session.status not in _LIVE_SESSION_STATUSES
    cutoff = datetime_epoch(observed_through) - config.confirm_delay_s
    statement = select(HotspotEvent).where(
        HotspotEvent.session_id == session_id,
        HotspotEvent.status.in_({HotspotStatus.PROVISIONAL, HotspotStatus.ENRICHING}),
        HotspotEvent.candidate_id.is_(None),
    )
    confirmed: list[HotspotEvent] = []
    for event in db.exec(statement).all():
        if not force and datetime_epoch(event.end_ts) > cutoff:
            continue
        event.status = HotspotStatus.CONFIRMED
        event.updated_at = utcnow()
        _refresh_representative_danmaku(db, event)
        db.add(event)
        confirmed.append(event)
        logger.info(
            "hotspot_confirmed: hotspot_id={} session_id={} force={}",
            event.id,
            session_id,
            force,
        )
    return confirmed


def _refresh_representative_danmaku(db: Session, event: HotspotEvent) -> None:
    receive_start, receive_end = align_danmaku_window(event.start_ts, event.end_ts)
    rows = db.exec(
        select(Danmaku)
        .where(
            Danmaku.session_id == event.session_id,
            Danmaku.msg_type == DanmakuType.DANMAKU,
            Danmaku.ts >= receive_start,
            Danmaku.ts <= receive_end,
        )
        .order_by(Danmaku.ts.asc(), Danmaku.id.asc())
    ).all()
    representatives = select_representative_danmaku(
        [row.content for row in rows],
        limit=3,
        include_role=True,
    )
    event.representative_danmaku_json = json.dumps(
        representatives,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _recording_is_contiguous(
    db: Session,
    session_id: int,
    start_ts: datetime,
    end_ts: datetime,
    *,
    tolerance_s: float,
) -> bool:
    start_epoch = datetime_epoch(start_ts)
    end_epoch = datetime_epoch(end_ts)
    if end_epoch <= start_epoch:
        return True
    rows = db.exec(
        select(RawSegment)
        .where(
            RawSegment.session_id == session_id,
            RawSegment.start_ts.is_not(None),
            RawSegment.end_ts.is_not(None),
        )
        .order_by(RawSegment.start_ts.asc(), RawSegment.seq.asc())
    ).all()
    cursor = start_epoch
    for segment in rows:
        if segment.start_ts is None or segment.end_ts is None:
            continue
        segment_start = datetime_epoch(segment.start_ts)
        segment_end = datetime_epoch(segment.end_ts)
        if segment_end < cursor - tolerance_s:
            continue
        if segment_start > cursor + tolerance_s:
            return False
        cursor = max(cursor, segment_end)
        if cursor >= end_epoch - tolerance_s:
            return True
    return False


def _event_terms(event: HotspotEvent) -> set[str]:
    texts = [event.transcript_text or ""]
    for item in _evidence_items(event.evidence_json):
        excerpts = item.get("excerpts")
        if isinstance(excerpts, list):
            texts.extend(str(value) for value in excerpts if isinstance(value, str))
    return _semantic_terms(" ".join(texts))


def _payload_extends_event(event: HotspotEvent, payload: Mapping[str, object]) -> bool:
    """判断同键新观测是否扩展了已经持久化的事件边界。"""
    incoming_start = _required_datetime(payload, "start_ts")
    incoming_end = _required_datetime(payload, "end_ts")
    return datetime_epoch(incoming_start) < datetime_epoch(event.start_ts) or datetime_epoch(
        incoming_end
    ) > datetime_epoch(event.end_ts)


def _semantic_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for raw in _WORD_PATTERN.findall(text.casefold()):
        if raw in _REACTION_TERMS:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            terms.update(raw[index : index + 2] for index in range(max(1, len(raw) - 1)))
        else:
            terms.add(raw)
    return terms


def _evidence_types(raw: str | None) -> set[str]:
    return {
        str(item["type"]) for item in _evidence_items(raw) if isinstance(item.get("type"), str) and item.get("type")
    }


def _evidence_items(raw: str | None) -> list[dict[str, Any]]:
    payload = _json_dict(raw)
    items = payload.get("items")
    return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def _merge_evidence_json(existing: str | None, incoming: str | None) -> str | None:
    if not incoming:
        return existing
    items_by_key: dict[str, dict[str, Any]] = {}
    for item in [*_evidence_items(existing), *_evidence_items(incoming)]:
        key_value = item.get("id")
        if isinstance(key_value, str) and key_value:
            key = f"id:{key_value}"
        else:
            identity = {name: item.get(name) for name in ("type", "start_ts", "end_ts", "metrics", "excerpts")}
            key = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        items_by_key[key] = item
    ordered = sorted(
        items_by_key.items(),
        key=lambda pair: (
            str(pair[1].get("start_ts") or ""),
            str(pair[1].get("type") or ""),
            pair[0],
        ),
    )
    return json.dumps(
        {"version": 1, "items": [item for _key, item in ordered]},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _merge_features_json(
    existing: str | None,
    incoming: str | None,
    *,
    observed_through: datetime | None,
) -> str:
    merged = _json_dict(existing)
    update = _json_dict(incoming)
    for key, value in update.items():
        if key == "ticks" and isinstance(value, list):
            previous = merged.get("ticks")
            ticks = [*previous, *value] if isinstance(previous, list) else list(value)
            unique: dict[str, object] = {}
            for tick in ticks:
                identity = json.dumps(tick, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                unique[identity] = tick
            merged["ticks"] = [unique[key] for key in sorted(unique)]
        elif key not in merged:
            merged[key] = value
    lifecycle = merged.get("event_lifecycle")
    lifecycle_payload = dict(lifecycle) if isinstance(lifecycle, Mapping) else {}
    update_count = lifecycle_payload.get("update_count", 0)
    lifecycle_payload.update(
        {
            "version": _LIFECYCLE_VERSION,
            "update_count": int(update_count) + 1 if isinstance(update_count, int) else 1,
            "last_observed_at": observed_through.isoformat() if observed_through is not None else None,
        }
    )
    merged["event_lifecycle"] = lifecycle_payload
    return json.dumps(merged, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _merge_text(existing: str | None, incoming: str | None) -> str | None:
    left = (existing or "").strip()
    right = (incoming or "").strip()
    if not right:
        return left or None
    if not left:
        return right
    if right in left:
        return left
    if left in right:
        return right
    return f"{left} {right}"


def _json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _interval_gap_s(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> float:
    return max(
        0.0,
        max(datetime_epoch(left_start), datetime_epoch(right_start))
        - min(datetime_epoch(left_end), datetime_epoch(right_end)),
    )


def _interval_overlap_s(
    left_start: datetime,
    left_end: datetime,
    right_start: datetime,
    right_end: datetime,
) -> float:
    return max(
        0.0,
        min(datetime_epoch(left_end), datetime_epoch(right_end))
        - max(datetime_epoch(left_start), datetime_epoch(right_start)),
    )


def _earlier(left: datetime, right: datetime) -> datetime:
    return left if datetime_epoch(left) <= datetime_epoch(right) else right


def _later(left: datetime, right: datetime) -> datetime:
    return left if datetime_epoch(left) >= datetime_epoch(right) else right


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _required_int(payload: Mapping[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int):
        raise ValueError(f"hotspot lifecycle payload 缺少整数 {name}")
    return value


def _required_str(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"hotspot lifecycle payload 缺少非空字符串 {name}")
    return value.strip()


def _required_datetime(payload: Mapping[str, object], name: str) -> datetime:
    value = payload.get(name)
    if not isinstance(value, datetime):
        raise ValueError(f"hotspot lifecycle payload 缺少 datetime {name}")
    return value


def _required_score(payload: Mapping[str, object], name: str) -> float:
    value = payload.get(name)
    if not isinstance(value, int | float):
        raise ValueError(f"hotspot lifecycle payload 缺少数值 {name}")
    numeric = float(value)
    if not 0.0 <= numeric <= 1.0:
        raise ValueError(f"hotspot lifecycle payload {name} 必须在 0~1 之间")
    return numeric


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
