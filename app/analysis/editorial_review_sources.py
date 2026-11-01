# ruff: noqa: D100
from __future__ import annotations

import hashlib
import json
import math
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import get_ident
from typing import cast

from sqlmodel import Session, select

from app.analysis import llm_providers
from app.analysis.editorial_contracts import (
    CONTEXT_SECONDS,
    EVIDENCE_MAX_CHARS,
    EXCERPT_MAX_CHARS,
    EXPANDED_CONTEXT_SECONDS,
    ContextDirection,
    EditorialExecutionV1,
    EditorialReviewInputV1,
)
from app.analysis.editorial_evidence import EvidenceContextUnavailable, build_event_input, build_segment_input
from app.analysis.editorial_provenance import ReviewProvenance, frozen_review_fingerprint
from app.analysis.editorial_review import (
    EditorialReviewDraft,
    editorial_auto_approvable,
    judge_editorial,
    review_fingerprint,
    review_policy_identity,
)
from app.analysis.event_enricher import EventTargetUnavailableError, build_event_evidence_bundle
from app.analysis.model_execution import ModelCallBudget
from app.analysis.timeline import datetime_epoch
from app.core.media_usage import media_reference_scope
from app.core.paths import resolve_storage_path
from app.db import session as database
from app.db.entities import AppSetting, HighlightCandidate, HighlightEvent, HotspotEvent, RawSegment, SegmentTask
from app.db.session import get_session


@dataclass(frozen=True, slots=True)
class _MediaRow:
    id: int
    session_id: int
    path: str
    start: datetime | None
    end: datetime | None

    @classmethod
    def from_segment(cls, segment: RawSegment) -> _MediaRow:
        if segment.id is None:
            raise ValueError("媒体快照要求已持久化的分段")
        return cls(segment.id, segment.session_id, segment.file_path, segment.start_ts, segment.end_ts)


@dataclass(frozen=True, slots=True)
class _MediaState:
    identity: list[int] | None
    readable: bool


@dataclass(frozen=True, slots=True)
class _MediaSnapshot:
    rows: tuple[_MediaRow, ...]
    states: Mapping[int, _MediaState]
    engine: object
    thread: int

    def matches(
        self, rows: list[RawSegment], session_id: int, start: datetime | None = None, end: datetime | None = None
    ) -> bool:
        def selected(row: _MediaRow) -> bool:
            return row.session_id == session_id and (
                start is None
                or end is None
                or row.start is None
                or row.end is None
                or (datetime_epoch(row.start) < datetime_epoch(end) and datetime_epoch(row.end) > datetime_epoch(start))
            )

        return tuple(row for item in rows if selected(row := _MediaRow.from_segment(item))) == tuple(
            row for row in self.rows if selected(row)
        )


_media_snapshot: ContextVar[_MediaSnapshot | None] = ContextVar("editorial_media_snapshot", default=None)


def _current_media_snapshot() -> _MediaSnapshot | None:
    snapshot = _media_snapshot.get()
    if snapshot is not None and snapshot.engine is database.engine and snapshot.thread == get_ident():
        return snapshot
    return None


def _probe_media(path: str) -> _MediaState:
    identity: list[int] | None = None
    readable = False
    try:
        file = resolve_storage_path(path)
        metadata = file.stat()
        identity = [metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns]
        if stat.S_ISREG(metadata.st_mode):
            with file.open("rb") as media:
                readable = bool(media.read(1))
    except (OSError, ValueError):
        pass
    return _MediaState(identity, readable)


@contextmanager
def media_validation_scope(*, session_ids: Iterable[int] = (), task_id: int | None = None) -> Iterator[None]:
    """先关闭来源读取会话，再探测媒体；提交事务只核对数据库引用快照。"""
    with media_reference_scope():
        ids = set(session_ids)
        with get_session(operation="media_validation_snapshot", task_id=task_id) as db:
            if task_id is not None:
                task = db.get(SegmentTask, task_id)
                if task is not None:
                    ids.add(task.session_id)
            segments = (
                db.exec(select(RawSegment).where(RawSegment.session_id.in_(ids)).order_by(RawSegment.id)).all()
                if ids
                else []
            )
            rows = tuple(_MediaRow.from_segment(segment) for segment in segments)
        by_path = {path: _probe_media(path) for path in {row.path for row in rows}}
        snapshot = _MediaSnapshot(rows, {row.id: by_path[row.path] for row in rows}, database.engine, get_ident())
        token = _media_snapshot.set(snapshot)
        try:
            yield
        finally:
            _media_snapshot.reset(token)


def media_snapshot_is_current(
    db: Session, session_id: int, *, start: datetime | None = None, end: datetime | None = None
) -> bool:
    """事务内拒绝实际使用范围的媒体引用变化，不访问文件系统。"""
    snapshot = _current_media_snapshot()
    if snapshot is None:
        return True
    rows = db.exec(select(RawSegment).where(RawSegment.session_id == session_id).order_by(RawSegment.id)).all()
    return snapshot.matches(rows, session_id, start, end)


def media_file_identity(segment: RawSegment) -> list[int] | None:
    """复用预检文件身份，数据库中的路径或边界变化时拒绝旧快照。"""
    snapshot = _current_media_snapshot()
    if snapshot is None:
        return _probe_media(segment.file_path).identity
    row = _MediaRow.from_segment(segment)
    if row not in snapshot.rows:
        raise ValueError("媒体引用在提交前已发生变化")
    return snapshot.states[row.id].identity


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
            context_s=EXPANDED_CONTEXT_SECONDS if direction is not None else CONTEXT_SECONDS,
            max_evidence_chars=EVIDENCE_MAX_CHARS,
            max_excerpt_chars=EXCERPT_MAX_CHARS,
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


def event_review_is_current(
    db: Session,
    event_id: int,
    draft: EditorialReviewDraft,
    *,
    policy: dict[str, object] | None = None,
    providers: list[llm_providers.LLMProvider] | None = None,
) -> bool:
    """提交前拒绝取消、事件替换、内容和配置变化后的旧判断。"""
    if draft.execution.status == "cancelled":
        return False
    bundle = build_event_evidence_bundle(db, event_id, providers=providers)
    if bundle.event_id != event_id or bundle.fingerprint != draft.revision:
        return False
    initial = event_review_source(db, event_id, None)
    source = event_review_source(db, event_id, draft.expanded_context)
    return draft.execution.input_fingerprint == review_fingerprint(initial, source, bundle.fingerprint, policy=policy)


def segment_review_revision(
    db: Session,
    segment_id: int,
    *,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
) -> str:
    """核验目标及有限扩展范围的完整媒体身份，不把路径发送给模型。"""
    segment = db.get(RawSegment, segment_id)
    if segment is None:
        raise ValueError(f"编辑判断分段不存在: segment_id={segment_id}")
    rows = db.exec(select(RawSegment).where(RawSegment.session_id == segment.session_id).order_by(RawSegment.id)).all()
    start = start_ts or segment.start_ts
    end = end_ts or segment.end_ts
    snapshot = _current_media_snapshot()
    if snapshot is not None and not snapshot.matches(rows, segment.session_id, start, end):
        raise ValueError("媒体引用在提交前已发生变化")
    files: list[dict[str, object]] = []
    for item in rows:
        if (
            start is not None
            and end is not None
            and item.start_ts is not None
            and item.end_ts is not None
            and (
                datetime_epoch(item.end_ts) <= datetime_epoch(start)
                or datetime_epoch(item.start_ts) >= datetime_epoch(end)
            )
        ):
            continue
        full_identity = media_file_identity(item)
        files.append(
            {
                "id": item.id,
                "path": item.file_path,
                "start": str(item.start_ts),
                "end": str(item.end_ts),
                "file": full_identity,
            }
        )
    payload = {
        "segments": files,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _source_media_revision(
    db: Session, segment_id: int, source: EditorialReviewInputV1 | None, start: datetime, end: datetime
) -> str:
    if source is not None:
        start = datetime.fromtimestamp(source.time_origin_epoch_s + source.window.start_s, tz=UTC)
        end = datetime.fromtimestamp(source.time_origin_epoch_s + source.window.end_s, tz=UTC)
    return segment_review_revision(db, segment_id, start_ts=start, end_ts=end)


def _combined_media_revision(
    revisions: Mapping[ContextDirection | None, str], expanded: ContextDirection | None
) -> str:
    if expanded is None:
        return revisions[None]
    return hashlib.sha256(json.dumps([revisions[None], revisions[expanded]]).encode()).hexdigest()


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
    revisions: dict[ContextDirection | None, str] = {}

    def load(direction: ContextDirection | None) -> EditorialReviewInputV1 | None:
        with get_session() as db:
            try:
                source = build_segment_input(
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
                source = None
            revisions[direction] = _source_media_revision(db, segment_id, source, start_ts, end_ts)
            return source

    initial = load(None)
    draft = judge_editorial(
        lambda direction: initial if direction is None else load(direction), revision=revisions[None], budget=budget
    )
    revision = _combined_media_revision(revisions, draft.expanded_context)
    if revision == draft.revision:
        return draft
    execution = draft.execution.model_copy(
        update={
            "input_fingerprint": review_fingerprint(initial, draft.source, revision, policy=draft.provenance.policy)
        }
    )
    return replace(draft, revision=revision, execution=execution)


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

    def is_current(self, db: Session, *, policy: dict[str, object] | None = None) -> bool:
        """验证输入身份、配置、文件和取消状态，迟到结果不能写入。"""
        from app.analysis.timeline import representative_danmaku

        if self.review.execution.status == "cancelled":
            return False
        segment = db.get(RawSegment, self.segment_id)
        if segment is None:
            return False
        current_danmaku = representative_danmaku(segment.session_id, self.start_ts, self.end_ts, db=db)
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

        initial = load(None)
        source = load(self.review.expanded_context) if self.review.expanded_context is not None else initial
        try:
            revisions = {None: _source_media_revision(db, self.segment_id, initial, self.start_ts, self.end_ts)}
            if self.review.expanded_context is not None:
                revisions[self.review.expanded_context] = _source_media_revision(
                    db, self.segment_id, source, self.start_ts, self.end_ts
                )
            revision = _combined_media_revision(revisions, self.review.expanded_context)
        except ValueError:
            return False
        return revision == self.review.revision and self.review.execution.input_fingerprint == review_fingerprint(
            initial, source, revision, policy=policy
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
    segments = db.exec(
        select(RawSegment).where(RawSegment.session_id == source.target.session_id).order_by(RawSegment.id)
    ).all()
    snapshot = _current_media_snapshot()
    if snapshot is not None and not snapshot.matches(segments, source.target.session_id, start, end):
        return False
    covered: list[tuple[float, float]] = []
    lower, upper = datetime_epoch(start), datetime_epoch(end)
    for segment in segments:
        if segment.start_ts is None or segment.end_ts is None:
            continue
        left, right = datetime_epoch(segment.start_ts), datetime_epoch(segment.end_ts)
        if left >= upper or right <= lower:
            continue
        state = snapshot.states[segment.id] if snapshot is not None else _probe_media(segment.file_path)
        if not state.readable:
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


def _current_review_providers(db: Session) -> list[llm_providers.LLMProvider]:
    from app.models.configuration import MODEL_CONFIGURATION_KEY
    from app.models.contracts import ModelConfiguration

    row = db.get(AppSetting, MODEL_CONFIGURATION_KEY)
    if row is not None and row.value:
        config = ModelConfiguration.model_validate_json(row.value)
        connections = {item.id: item for item in config.connections}
        providers = [
            llm_providers.from_route(route, connections[route.connection_id])
            for route in config.routes
            if route.capability == "analysis"
        ]
    else:
        providers = [llm_providers._coerce(value) for value in llm_providers._read_raw(db)]
    return sorted(
        [
            provider
            for provider in providers
            if provider.enabled and (provider.api_key or provider.connection_id) and provider.base_url
        ],
        key=lambda provider: (provider.priority, provider.name),
    )


def automatic_review_is_current(db: Session, event: HighlightEvent, candidate: HighlightCandidate) -> bool:
    """自动操作重新核验冻结来源、当前证据和实际成片边界，不重新请求模型。"""
    try:
        features = json.loads(event.features_json or "{}")
        if not isinstance(features, dict):
            return False
        metadata = features.get("editorial_review")
        if not editorial_auto_approvable(metadata):
            return False
        execution = EditorialExecutionV1.model_validate(metadata.get("execution"))
        source = EditorialReviewInputV1.model_validate(metadata.get("input"))
        provenance = ReviewProvenance.model_validate(metadata.get("provenance"))
        initial = provenance.initial_input
        revision, direction = metadata.get("revision"), metadata.get("expanded_context")
        if (
            initial is None
            or initial.target != source.target
            or initial.time_origin_epoch_s != source.time_origin_epoch_s
            or source.time_origin_epoch_s is None
            or source.target.session_id != event.session_id
            or event.session_id != candidate.session_id
            or event.candidate_id != candidate.id
            or not isinstance(revision, str)
            or direction not in (None, "before", "after", "both")
            or execution.policy_version != provenance.policy["policy"]
            or frozen_review_fingerprint(initial, source, revision, policy=provenance.policy)
            != execution.input_fingerprint
        ):
            return False
        draft = EditorialReviewDraft(execution, source, cast(ContextDirection | None, direction), revision, provenance)
        providers = _current_review_providers(db)
        policy = review_policy_identity(providers=providers)
        target = source.target
        origin = source.time_origin_epoch_s
        if target.kind == "hotspot":
            hotspot = db.get(HotspotEvent, target.target_id)
            if (
                hotspot is None
                or hotspot.candidate_id != candidate.id
                or hotspot.session_id != event.session_id
                or not event_review_is_current(db, hotspot.id, draft, policy=policy, providers=providers)
            ):
                return False
        else:
            metrics, timeline = features.get("features"), features.get("timeline")
            if (
                event.segment_id != target.target_id
                or not isinstance(metrics, dict)
                or any(
                    not isinstance(key, str) or type(value) not in (int, float) or not math.isfinite(value)
                    for key, value in metrics.items()
                )
                or not isinstance(timeline, dict)
            ):
                return False
            danmaku = timeline.get("representative_danmaku")
            if not isinstance(danmaku, list) or any(not isinstance(item, dict) for item in danmaku):
                return False
            snapshot = SegmentReviewSnapshot(
                draft,
                target.target_id,
                datetime.fromtimestamp(origin + target.bounds.start_s, UTC),
                datetime.fromtimestamp(origin + target.bounds.end_s, UTC),
                datetime.fromtimestamp(origin + target.peak_s, UTC),
                metrics,
                danmaku,
            )
            if not snapshot.is_current(db, policy=policy):
                return False
        start, end = datetime_epoch(candidate.start_ts) - origin, datetime_epoch(candidate.end_ts) - origin
        if abs(datetime_epoch(candidate.peak_ts) - origin - target.peak_s) > 1e-6:
            return False
        result = execution.result
        assert result is not None
        suggestion = result.suggested_bounds
        if suggestion is not None:
            if abs(start - suggestion.start_s) > 1e-6 or abs(end - suggestion.end_s) > 1e-6:
                return False
        elif not (
            source.window.start_s <= start <= target.bounds.start_s
            and target.bounds.end_s <= end <= source.window.end_s
        ):
            return False
        return media_bounds_accessible(db, source, candidate.start_ts, candidate.end_ts)
    except ValueError:
        return False
