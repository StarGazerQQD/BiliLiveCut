# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.analysis.editorial_review import review_policy_identity
from app.analysis.editorial_review_sources import segment_review_revision
from app.analysis.reanalysis_policy import event_protection_reason, segment_protection_reason
from app.analysis.scoring_config import get_scoring_config
from app.analysis.timeline import datetime_epoch
from app.db.entities import (
    AppSetting,
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    HotspotStatus,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentTask,
    Transcript,
)
from app.web.services.review_workflow import decode_features


class ReanalysisSelection(BaseModel):
    """只选择已有分析对象；分页预览不会调用模型或修改数据库。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    session_id: int = Field(gt=0)
    keys: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(default_factory=list, max_length=100)
    verdicts: list[Literal["recommend", "reject", "needs_review", "unknown"]] = Field(
        default_factory=list, max_length=4
    )
    failed_only: bool = False
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class ReanalysisTarget(BaseModel):
    """服务端解析的稳定业务身份和预览快照，提交时必须重新核验。"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    key: str = Field(min_length=1, max_length=256)
    kind: Literal["hotspot", "segment"]
    source_id: int = Field(gt=0)
    session_id: int = Field(gt=0)
    segment_id: int = Field(gt=0)
    peak_ts: datetime
    candidate_id: int | None = Field(default=None, gt=0)
    event_id: int | None = Field(default=None, gt=0)
    fingerprint: str = ""
    protection: str | None = None
    verdict: Literal["recommend", "reject", "needs_review", "unknown"] = "unknown"
    failed: bool = False


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def target_protection(db: Session, target: ReanalysisTarget, own_task_id: int | None = None) -> str | None:
    """检查目标和跨段录制范围内的人工及执行状态。"""
    if target.candidate_id is not None and target.event_id is None:
        return "missing_review_event"
    if target.event_id is not None:
        event = db.get(HighlightEvent, target.event_id)
        if event is None or event.candidate_id != target.candidate_id:
            return "identity_changed"
        reason = event_protection_reason(db, event, own_task_id)
        if reason:
            return reason
    if target.kind == "hotspot":
        hotspot = db.get(HotspotEvent, target.source_id)
        if hotspot is None or hotspot.status != HotspotStatus.CONFIRMED or hotspot.candidate_id != target.candidate_id:
            return "identity_changed"
        segments = db.exec(select(RawSegment).where(RawSegment.session_id == target.session_id)).all()
        related = [
            segment
            for segment in segments
            if segment.start_ts is not None
            and segment.end_ts is not None
            and datetime_epoch(segment.start_ts) < datetime_epoch(hotspot.end_ts)
            and datetime_epoch(segment.end_ts) > datetime_epoch(hotspot.start_ts)
        ]
    else:
        segment = db.get(RawSegment, target.segment_id)
        related = [segment] if segment is not None else []
    if not related:
        return "missing_source"
    for segment in related:
        reason = segment_protection_reason(db, segment.id, own_task_id=own_task_id)
        if reason:
            return reason
    return None


def target_fingerprint(db: Session, target: ReanalysisTarget, own_task_id: int | None = None) -> str:
    """散列真实来源、人工状态和配置；公开结果只有指纹，不包含路径或凭据。"""
    recording = db.get(RecordingSession, target.session_id)
    if recording is None:
        raise ValueError("重分析场次不存在")
    room = db.get(LiveRoom, recording.room_id)
    segments = db.exec(
        select(RawSegment).where(RawSegment.session_id == target.session_id).order_by(RawSegment.id)
    ).all()
    transcripts = db.exec(
        select(Transcript).where(Transcript.segment_id.in_([item.id for item in segments])).order_by(Transcript.id)
    ).all()
    candidate = db.get(HighlightCandidate, target.candidate_id) if target.candidate_id is not None else None
    event = db.get(HighlightEvent, target.event_id) if target.event_id is not None else None
    hotspot = db.get(HotspotEvent, target.source_id) if target.kind == "hotspot" else None
    tasks = db.exec(
        select(SegmentTask).where(SegmentTask.segment_id == target.segment_id).order_by(SegmentTask.id)
    ).all()
    record = db.get(AppSetting, f"editorial_segment:{target.segment_id}") if target.kind == "segment" else None
    return _hash(
        {
            "target": [
                target.key,
                target.kind,
                target.source_id,
                target.segment_id,
                target.peak_ts.isoformat(),
                target.candidate_id,
                target.event_id,
            ],
            "source_revision": segment_review_revision(db, target.segment_id),
            "transcripts": [item.model_dump(mode="json") for item in transcripts],
            "candidate": candidate.model_dump(mode="json") if candidate is not None else None,
            "event": event.model_dump(mode="json") if event is not None else None,
            "hotspot": hotspot.model_dump(mode="json") if hotspot is not None else None,
            "windows": record.value if record is not None else None,
            "tasks": [
                {
                    "id": task.id,
                    "stage": task.stage,
                    "lease": task.lease_token,
                    "candidate": task.candidate_id,
                    "event": task.event_id,
                    "clip": task.clip_id,
                }
                for task in tasks
                if task.id != own_task_id
            ],
            "room": {
                name: getattr(room, name)
                for name in (
                    "highlight_threshold",
                    "review_threshold",
                    "auto_approve",
                    "auto_approve_threshold",
                    "danmaku_sentiment_enabled",
                )
            }
            if room is not None
            else None,
            "scoring": asdict(get_scoring_config()),
            "policy": review_policy_identity(),
        }
    )


def _decorate(db: Session, target: ReanalysisTarget, metadata: object, own_task_id: int | None) -> ReanalysisTarget:
    execution = metadata.get("execution") if isinstance(metadata, dict) else None
    result = execution.get("result") if isinstance(execution, dict) else None
    verdict = result.get("verdict", "unknown") if isinstance(result, dict) else "unknown"
    if verdict not in {"recommend", "reject", "needs_review", "unknown"}:
        raise ValueError("已有编辑判断包含无效结论")
    return target.model_copy(
        update={
            "fingerprint": target_fingerprint(db, target, own_task_id),
            "protection": target_protection(db, target, own_task_id),
            "verdict": verdict,
            "failed": isinstance(execution, dict) and execution.get("status") == "failed",
        }
    )


def list_reanalysis_targets(db: Session, session_id: int, *, own_task_id: int | None = None) -> list[ReanalysisTarget]:
    """列出热点、历史候选及未生成候选的旧链窗口，每个业务对象仅出现一次。"""
    if db.get(RecordingSession, session_id) is None:
        raise ValueError("录制场次不存在")
    segments = db.exec(select(RawSegment).where(RawSegment.session_id == session_id).order_by(RawSegment.id)).all()
    events = db.exec(
        select(HighlightEvent).where(HighlightEvent.session_id == session_id).order_by(HighlightEvent.id)
    ).all()
    events_by_candidate = {event.candidate_id: event for event in events}
    candidates = db.exec(
        select(HighlightCandidate).where(HighlightCandidate.session_id == session_id).order_by(HighlightCandidate.id)
    ).all()
    used: set[int] = set()
    targets: list[ReanalysisTarget] = []
    for hotspot in db.exec(
        select(HotspotEvent)
        .where(HotspotEvent.session_id == session_id, HotspotEvent.status == HotspotStatus.CONFIRMED)
        .order_by(HotspotEvent.id)
    ).all():
        segment = next(
            (
                item
                for item in segments
                if item.start_ts is not None
                and item.end_ts is not None
                and datetime_epoch(item.start_ts) <= datetime_epoch(hotspot.peak_ts) < datetime_epoch(item.end_ts)
            ),
            None,
        )
        if segment is None:
            continue
        event = events_by_candidate.get(hotspot.candidate_id)
        target = ReanalysisTarget(
            key=f"hotspot:{hotspot.id}",
            kind="hotspot",
            source_id=hotspot.id,
            session_id=session_id,
            segment_id=segment.id,
            peak_ts=hotspot.peak_ts,
            candidate_id=hotspot.candidate_id,
            event_id=event.id if event else None,
        )
        targets.append(
            _decorate(db, target, decode_features(hotspot.features_json).get("editorial_review"), own_task_id)
        )
        if hotspot.candidate_id is not None:
            used.add(hotspot.candidate_id)
    for candidate in candidates:
        if candidate.id in used:
            continue
        event = events_by_candidate.get(candidate.id)
        segment = next((item for item in segments if event is not None and item.id == event.segment_id), None)
        if segment is None:
            segment = next(
                (
                    item
                    for item in segments
                    if item.start_ts is not None
                    and item.end_ts is not None
                    and datetime_epoch(item.start_ts) <= datetime_epoch(candidate.peak_ts) < datetime_epoch(item.end_ts)
                ),
                None,
            )
        if segment is None:
            continue
        target = ReanalysisTarget(
            key=f"candidate:{candidate.id}",
            kind="segment",
            source_id=segment.id,
            session_id=session_id,
            segment_id=segment.id,
            peak_ts=candidate.peak_ts,
            candidate_id=candidate.id,
            event_id=event.id if event else None,
        )
        targets.append(
            _decorate(db, target, decode_features(candidate.features_json).get("editorial_review"), own_task_id)
        )
    for segment in segments:
        record = db.get(AppSetting, f"editorial_segment:{segment.id}")
        if record is None:
            continue
        payload = json.loads(record.value)
        if payload.get("schema_version") != "segment-editorial-v1" or payload.get("segment_id") != segment.id:
            raise ValueError("旧链编辑窗口记录格式无效")
        for window in payload["windows"]:
            peak = datetime.fromisoformat(window["peak_ts"])
            if any(
                item.segment_id == segment.id and abs(datetime_epoch(item.peak_ts) - datetime_epoch(peak)) < 1e-6
                for item in targets
            ):
                continue
            target = ReanalysisTarget(
                key=f"segment:{segment.id}:{datetime_epoch(peak):.6f}",
                kind="segment",
                source_id=segment.id,
                session_id=session_id,
                segment_id=segment.id,
                peak_ts=peak,
            )
            targets.append(_decorate(db, target, window["editorial_review"], own_task_id))
    return sorted(targets, key=lambda item: (datetime_epoch(item.peak_ts), item.key))


def preview_reanalysis(db: Session, selection: ReanalysisSelection) -> dict[str, object]:
    """返回筛选总数、保护原因和执行指纹；预览不触发付费调用。"""
    targets = list_reanalysis_targets(db, selection.session_id)
    if selection.keys:
        if len(set(selection.keys)) != len(selection.keys) or set(selection.keys) - {item.key for item in targets}:
            raise ValueError("选择包含重复或不存在的分析对象")
        targets = [item for item in targets if item.key in selection.keys]
    targets = [
        item
        for item in targets
        if (not selection.verdicts or item.verdict in selection.verdicts) and (not selection.failed_only or item.failed)
    ]
    page = targets[selection.offset : selection.offset + selection.limit]
    return {
        "session_id": selection.session_id,
        "total": len(targets),
        "targets": [item.model_dump(mode="json") for item in page],
        "fingerprint": _hash([item.model_dump(mode="json") for item in page]),
        "next_offset": selection.offset + len(page) if selection.offset + len(page) < len(targets) else None,
    }
