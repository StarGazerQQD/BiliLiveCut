# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app.analysis.editorial_contracts import ContextDirection, EditorialReviewInputV1
from app.analysis.editorial_evidence import EvidenceContextUnavailable, build_event_input, build_segment_input
from app.analysis.editorial_review import EditorialReviewDraft, judge_editorial, review_fingerprint
from app.analysis.event_enricher import EventTargetUnavailableError, build_event_evidence_bundle
from app.analysis.model_execution import ModelCallBudget
from app.analysis.timeline import datetime_epoch
from app.db.entities import AppSetting, HotspotEvent, RawSegment
from app.db.session import get_session


def event_review_source(
    db: Session, event_id: int, direction: ContextDirection | None
) -> EditorialReviewInputV1 | None:
    """按目标和扩展方向读取真实事件输入，不写数据库。"""
    event = db.get(HotspotEvent, event_id)
    if event is None:
        raise EventTargetUnavailableError(f"编辑判断事件不存在: event_id={event_id}")
    try:
        return build_event_input(
            db,
            event,
            context_s=90.0 if direction is not None else 30.0,
            max_evidence_chars=12000,
            max_excerpt_chars=2000,
            context_direction=direction,
        )
    except EvidenceContextUnavailable:
        return None


def compute_event_review(event_id: int, budget: ModelCallBudget) -> EditorialReviewDraft:
    """在事务外执行热点编辑判断，保存提交时所需的来源身份。"""
    with get_session() as db:
        bundle = build_event_evidence_bundle(db, event_id)
    canonical_id = bundle.event_id

    def load(direction: ContextDirection | None) -> EditorialReviewInputV1 | None:
        with get_session() as db:
            return event_review_source(db, canonical_id, direction)

    return judge_editorial(load, revision=bundle.fingerprint, budget=budget)


def event_review_is_current(db: Session, event_id: int, draft: EditorialReviewDraft) -> bool:
    """提交前拒绝取消、事件替换、内容和配置变化后的旧判断。"""
    if draft.execution.status == "cancelled":
        return False
    bundle = build_event_evidence_bundle(db, event_id)
    if bundle.event_id != event_id or bundle.fingerprint != draft.revision:
        return False
    initial = event_review_source(db, event_id, None)
    source = event_review_source(db, event_id, draft.expanded_context)
    return draft.execution.input_fingerprint == review_fingerprint(initial, source, bundle.fingerprint)


def segment_review_revision(db: Session, segment_id: int) -> str:
    """纳入录制身份、配置代次与文件元数据，不把路径发送给模型。"""
    segment = db.get(RawSegment, segment_id)
    if segment is None:
        raise ValueError(f"编辑判断分段不存在: segment_id={segment_id}")
    rows = db.exec(select(RawSegment).where(RawSegment.session_id == segment.session_id).order_by(RawSegment.id)).all()
    revision = db.get(AppSetting, "configuration_revision")
    files: list[dict[str, object]] = []
    for item in rows:
        try:
            stat = Path(item.file_path).stat()
            identity = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            identity = None
        files.append(
            {
                "id": item.id,
                "path": item.file_path,
                "start": str(item.start_ts),
                "end": str(item.end_ts),
                "file": identity,
            }
        )
    payload = {
        "configuration_revision": revision.value if revision is not None else "0",
        "segments": files,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def compute_segment_review(
    segment_id: int,
    *,
    start_ts: datetime,
    end_ts: datetime,
    peak_ts: datetime,
    metrics: Mapping[str, float],
    danmaku: list[dict[str, object]],
    budget: ModelCallBudget,
) -> EditorialReviewDraft:
    """为旧分段链提供同一编辑判断和预算，不使用比例文本作为精确证据。"""
    with get_session() as db:
        revision = segment_review_revision(db, segment_id)

    def load(direction: ContextDirection | None) -> EditorialReviewInputV1 | None:
        with get_session() as db:
            try:
                return build_segment_input(
                    db,
                    segment_id,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    peak_ts=peak_ts,
                    metrics=metrics,
                    danmaku=danmaku,
                    context_direction=direction,
                )
            except EvidenceContextUnavailable:
                return None

    return judge_editorial(load, revision=revision, budget=budget)


@dataclass(frozen=True, slots=True)
class SegmentReviewSnapshot:
    """保存旧分段计算的来源，提交时重新读取文本和弹幕进行核验。"""

    review: EditorialReviewDraft
    segment_id: int
    start_ts: datetime
    end_ts: datetime
    peak_ts: datetime
    metrics: Mapping[str, float]
    danmaku: list[dict[str, object]]

    def is_current(self, db: Session) -> bool:
        """验证输入身份、配置、文件和取消状态，迟到结果不能写入。"""
        from app.analysis.timeline import representative_danmaku

        if self.review.execution.status == "cancelled":
            return False
        segment = db.get(RawSegment, self.segment_id)
        if segment is None or segment_review_revision(db, self.segment_id) != self.review.revision:
            return False
        current_danmaku = representative_danmaku(segment.session_id, self.start_ts, self.end_ts)
        if current_danmaku != self.danmaku:
            return False

        def load(direction: ContextDirection | None) -> EditorialReviewInputV1 | None:
            try:
                return build_segment_input(
                    db,
                    self.segment_id,
                    start_ts=self.start_ts,
                    end_ts=self.end_ts,
                    peak_ts=self.peak_ts,
                    metrics=self.metrics,
                    danmaku=current_danmaku,
                    context_direction=direction,
                )
            except EvidenceContextUnavailable:
                return None

        return self.review.execution.input_fingerprint == review_fingerprint(
            load(None),
            load(self.review.expanded_context),
            self.review.revision,
        )


def store_segment_reviews(db: Session, segment_id: int, snapshots: list[SegmentReviewSnapshot]) -> bool:
    """原子保留旧链各窗口的实际判断，包括未生成候选的拒绝与失败。"""
    if not snapshots:
        return True
    if any(item.segment_id != segment_id or not item.is_current(db) for item in snapshots):
        return False
    payload = {
        "schema_version": "segment-editorial-v1",
        "segment_id": segment_id,
        "windows": [
            {
                "start_ts": item.start_ts.isoformat(),
                "end_ts": item.end_ts.isoformat(),
                "peak_ts": item.peak_ts.isoformat(),
                "editorial_review": item.review.metadata(),
            }
            for item in snapshots
        ],
    }
    key = f"editorial_segment:{segment_id}"
    record = db.get(AppSetting, key) or AppSetting(key=key, value="{}")
    record.value = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    db.add(record)
    return True


def media_bounds_accessible(db: Session, source: EditorialReviewInputV1 | None, start: datetime, end: datetime) -> bool:
    """实际打开媒体并核对连续覆盖；缺失文件不能支持自动批准或模型边界。"""
    if source is None or end <= start:
        return False
    segments = db.exec(select(RawSegment).where(RawSegment.session_id == source.target.session_id)).all()
    covered: list[tuple[float, float]] = []
    lower, upper = datetime_epoch(start), datetime_epoch(end)
    for segment in segments:
        if segment.start_ts is None or segment.end_ts is None:
            continue
        left, right = datetime_epoch(segment.start_ts), datetime_epoch(segment.end_ts)
        if left >= upper or right <= lower:
            continue
        try:
            path = Path(segment.file_path)
            if not stat.S_ISREG(path.stat().st_mode):
                continue
            with path.open("rb") as media:
                if not media.read(1):
                    continue
        except OSError:
            continue
        covered.append((left, right))
    cursor = lower
    for left, right in sorted(covered):
        if left > cursor:
            break
        cursor = max(cursor, right)
        if cursor >= upper:
            return True
    return False
