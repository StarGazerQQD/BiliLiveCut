# ruff: noqa: D100
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlmodel import Session

from app.analysis.editorial_contracts import EditorialReviewInputV1
from app.analysis.editorial_provenance import ReviewProvenance
from app.analysis.editorial_review_sources import MediaWindow, SegmentReviewSnapshot
from app.db.entities import HighlightCandidate, HighlightEvent, HotspotEvent, SegmentTask

if TYPE_CHECKING:
    from app.analysis.editorial_reanalysis import ReanalysisDraft
    from app.analysis.editorial_review import EditorialReviewDraft
    from app.analysis.reanalysis_targets import ReanalysisTarget


def _input_window(source: EditorialReviewInputV1 | None, fallback: MediaWindow | None = None) -> MediaWindow:
    if source is None:
        if fallback is None:
            raise ValueError("编辑判断缺少媒体范围")
        return fallback
    if source.time_origin_epoch_s is None:
        raise ValueError("编辑判断输入缺少媒体时间原点")
    return MediaWindow(
        source.target.session_id,
        datetime.fromtimestamp(source.time_origin_epoch_s + source.window.start_s, UTC),
        datetime.fromtimestamp(source.time_origin_epoch_s + source.window.end_s, UTC),
    )


def _review_windows(review: EditorialReviewDraft, fallback: MediaWindow) -> list[MediaWindow]:
    return [_input_window(review.provenance.initial_input, fallback), _input_window(review.source, fallback)]


def segment_result_windows(db: Session, result: Mapping[str, object]) -> list[MediaWindow]:
    """保留全部实际编辑结果的原始与扩展范围，包括拒绝和失败窗口。"""
    from app.db.entities import RawSegment

    snapshots = result.get("editorial_outcomes")
    if snapshots is None:
        single = result.get("editorial_snapshot")
        snapshots = [] if single is None else [single]
    if not isinstance(snapshots, list) or any(not isinstance(item, SegmentReviewSnapshot) for item in snapshots):
        raise ValueError("分段编辑判断快照格式无效")
    windows: list[MediaWindow] = []
    for snapshot in snapshots:
        segment = db.get(RawSegment, snapshot.segment_id)
        if segment is not None:
            windows.extend(
                _review_windows(snapshot.review, MediaWindow(segment.session_id, snapshot.start_ts, snapshot.end_ts))
            )
    return windows


def target_media_windows(db: Session, target: ReanalysisTarget) -> list[MediaWindow]:
    """复用重分析目标指纹已经定义的媒体范围。"""
    from app.analysis.reanalysis_targets import target_media_bounds

    start, end = target_media_bounds(db, target)
    return [MediaWindow(target.session_id, start, end)]


def hotspot_media_windows(db: Session, event_ids: Iterable[int], *, checkpoints: bool = False) -> list[MediaWindow]:
    """评分文件范围复用正式连续块；检查点额外包含已有目标指纹的范围。"""
    from app.analysis.clip_scorer import event_clip_media_bounds
    from app.analysis.reanalysis_targets import ReanalysisTarget

    windows: list[MediaWindow] = []
    for event_id in set(event_ids):
        event = db.get(HotspotEvent, event_id)
        if event is None:
            continue
        try:
            segment_id, start, end = event_clip_media_bounds(db, event)
        except ValueError:
            # 正式提交仍会拒绝没有峰值分段的结果，此处没有可供探测的媒体范围。
            continue
        windows.append(MediaWindow(event.session_id, start, end))
        if checkpoints:
            windows.extend(
                target_media_windows(
                    db,
                    ReanalysisTarget(
                        key=f"hotspot:{event_id}",
                        kind="hotspot",
                        source_id=event_id,
                        session_id=event.session_id,
                        segment_id=segment_id,
                        peak_ts=event.peak_ts,
                    ),
                )
            )
    return windows


def event_result_windows(db: Session, results: Iterable[object], *, checkpoints: bool = False) -> list[MediaWindow]:
    """合并所有热点结果的评分与实际成片范围。"""
    from app.analysis.clip_scorer import EventClipDraft

    items = [item for item in results if isinstance(item, Mapping)]
    windows = hotspot_media_windows(
        db, (item["event_id"] for item in items if type(item.get("event_id")) is int), checkpoints=checkpoints
    )
    for item in items:
        draft = item.get("clip_draft")
        if isinstance(draft, EventClipDraft):
            windows.append(MediaWindow(draft.session_id, draft.start_ts, draft.end_ts))
    return windows


def reanalysis_media_windows(db: Session, drafts: Iterable[ReanalysisDraft]) -> list[MediaWindow]:
    """重分析同时核对原目标身份与本次实际使用的编辑范围。"""
    windows: list[MediaWindow] = []
    for draft in drafts:
        windows.extend(target_media_windows(db, draft.target))
        if draft.payload is not None:
            if draft.target.kind == "segment":
                windows.extend(segment_result_windows(db, draft.payload))
            else:
                windows.extend(event_result_windows(db, [draft.payload]))
    return windows


def task_media_windows(
    db: Session,
    *,
    task_ids: Iterable[int] = (),
    event_ids: Iterable[int] = (),
) -> list[MediaWindow]:
    """只装载已选定任务的候选与冻结编辑输入，不重复实现批准资格判定。"""
    candidates: set[int] = set()
    events = set(event_ids)
    for task_id in set(task_ids):
        task = db.get(SegmentTask, task_id)
        if task is not None:
            if task.candidate_id is not None:
                candidates.add(task.candidate_id)
            if task.event_id is not None:
                events.add(task.event_id)
    windows: list[MediaWindow] = []
    for event_id in events:
        event = db.get(HighlightEvent, event_id)
        if event is None:
            continue
        candidates.add(event.candidate_id)
        try:
            features = json.loads(event.features_json or "{}")
            metadata = features.get("editorial_review") if isinstance(features, dict) else None
            if not isinstance(metadata, dict):
                continue
            source = EditorialReviewInputV1.model_validate(metadata.get("input"))
            provenance = ReviewProvenance.model_validate(metadata.get("provenance"))
        except (json.JSONDecodeError, ValidationError):
            # 损坏的审核元数据仍由 automatic_review_is_current 明确拒绝。
            continue
        if source.target.kind == "segment" and source.time_origin_epoch_s is not None:
            try:
                fallback = _input_window(source)
                initial = _input_window(provenance.initial_input, fallback)
            except ValueError:
                # 原点不完整的记录由原审批函数拒绝，不中断同批其他任务。
                continue
            windows.extend([fallback, initial])
    for candidate_id in candidates:
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is not None:
            windows.append(MediaWindow(candidate.session_id, candidate.start_ts, candidate.end_ts))
    return windows
