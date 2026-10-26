# ruff: noqa: D100
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel
from sqlmodel import Session, create_engine, select

from app import __version__
from app.analysis.editorial_contracts import EditorialExecutionV1, EditorialReviewInputV1, TimeRange
from app.analysis.editorial_evaluation import EvaluationRecord, EvaluationSession, EvaluationSnapshot
from app.analysis.editorial_provenance import ReviewProvenance
from app.db.entities import (
    AppSetting,
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    HotspotStatus,
    RawSegment,
    RecordingSession,
    SegmentStatus,
    SegmentTask,
    TaskStatus,
)

MAX_FILE_BYTES = 64 * 1024 * 1024
T = TypeVar("T", bound=BaseModel)


def datetime_epoch(value: datetime) -> float:
    """使用数据库 UTC 时间约定，离线工具不加载带运行时初始化的模块。"""
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp()


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复")
        result[key] = value
    return result


def _load(text: str) -> dict[str, object]:
    value = json.loads(text, object_pairs_hook=_object)
    if not isinstance(value, dict):
        raise ValueError("JSON 顶层必须是对象")
    return value


def read_contract(path: Path, contract: type[T]) -> T:
    """有界读取严格 JSON 文件，不执行其中的路径或指令。"""
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("评估文件超过 64 MiB，请减少本次场次数")
    try:
        return contract.model_validate(_load(data.decode("utf-8")))
    except RecursionError as exc:
        raise ValueError("评估 JSON 嵌套过深") from exc


def write_result(path: Path, value: object) -> None:
    """只新建明确指定的评估产物，禁止覆盖快照、数据库或已有报告。"""
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("评估输出超过 64 MiB，请减少本次场次数")
    with path.open("xb") as stream:
        stream.write(data)


def _merged_ranges(segments: list[RawSegment], origin: float) -> list[TimeRange]:
    intervals: list[tuple[float, float]] = []
    for segment in segments:
        if segment.start_ts is None or segment.end_ts is None:
            raise ValueError("分段缺少真实录制时间，无法导出评估范围")
        intervals.append((datetime_epoch(segment.start_ts) - origin, datetime_epoch(segment.end_ts) - origin))
    merged: list[TimeRange] = []
    for start, end in sorted(intervals):
        interval = TimeRange(start_s=start, end_s=end)
        if merged and start <= merged[-1].end_s:
            previous = merged.pop()
            interval = TimeRange(start_s=previous.start_s, end_s=max(end, previous.end_s))
        merged.append(interval)
    return merged


def _record(
    key: str,
    session_id: int,
    start: datetime,
    end: datetime,
    origin: float,
    metadata: object,
    *,
    title: str | None = None,
    summary: str | None = None,
    verified: bool = False,
) -> EvaluationRecord:
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("编辑记录格式无效")
    metadata = metadata or {}
    source = EditorialReviewInputV1.model_validate(metadata["input"]) if metadata.get("input") is not None else None
    execution = (
        EditorialExecutionV1.model_validate(metadata["execution"]) if metadata.get("execution") is not None else None
    )
    if source is not None:
        if source.time_origin_epoch_s is None or abs(source.time_origin_epoch_s - origin) > 1e-6:
            raise ValueError("历史判断的时间原点与场次不一致，需要重新分析后导出")
        kind, identity = key.split(":", 2)[:2]
        if kind in {"hotspot", "segment"} and (source.target.kind != kind or source.target.target_id != int(identity)):
            raise ValueError("评估输入与业务对象身份不一致")
    return EvaluationRecord(
        key=key,
        session_id=session_id,
        bounds=TimeRange(start_s=datetime_epoch(start) - origin, end_s=datetime_epoch(end) - origin),
        input=source,
        execution=execution,
        provenance=ReviewProvenance.model_validate(metadata["provenance"])
        if metadata.get("provenance") is not None
        else None,
        revision=metadata.get("revision"),
        title=title,
        summary=summary,
        explanation_verified=verified,
    )


def _session_records(db: Session, session_id: int) -> tuple[EvaluationSession, list[EvaluationRecord]]:
    recording = db.get(RecordingSession, session_id)
    segments = list(
        db.exec(select(RawSegment).where(RawSegment.session_id == session_id).order_by(RawSegment.id)).all()
    )
    if recording is None or not segments:
        raise ValueError("场次不存在或没有录制分段")
    origin = min([datetime_epoch(recording.started_at)] + [datetime_epoch(s.start_ts) for s in segments if s.start_ts])
    ranges = _merged_ranges(segments, origin)
    candidates = list(db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)).all())
    by_candidate = {c.id: c for c in candidates}
    events = list(db.exec(select(HighlightEvent).where(HighlightEvent.session_id == session_id)).all())
    event_map = {e.candidate_id: e for e in events}
    hotspots = list(
        db.exec(select(HotspotEvent).where(HotspotEvent.session_id == session_id).order_by(HotspotEvent.id)).all()
    )
    records: list[EvaluationRecord] = []
    used: set[int] = set()
    known_windows: set[tuple[int, float]] = set()
    for hotspot in hotspots:
        if hotspot.status != HotspotStatus.CONFIRMED:
            continue
        features = _load(hotspot.features_json) if hotspot.features_json else {}
        enrichment = features.get("event_enrichment")
        verified = (
            isinstance(enrichment, dict) and enrichment.get("source") == "llm" and enrichment.get("error") is None
        )
        candidate = by_candidate.get(hotspot.candidate_id)
        records.append(
            _record(
                f"hotspot:{hotspot.id}",
                session_id,
                candidate.start_ts if candidate else hotspot.start_ts,
                candidate.end_ts if candidate else hotspot.end_ts,
                origin,
                features.get("editorial_review"),
                title=hotspot.title,
                summary=hotspot.summary,
                verified=verified,
            )
        )
        if candidate is not None:
            used.add(candidate.id)
            event = event_map.get(candidate.id)
            if event is not None:
                known_windows.add((event.segment_id, datetime_epoch(candidate.peak_ts)))
    for candidate in sorted(candidates, key=lambda c: c.id):
        if candidate.id in used:
            continue
        features = _load(candidate.features_json) if candidate.features_json else {}
        records.append(
            _record(
                f"candidate:{candidate.id}",
                session_id,
                candidate.start_ts,
                candidate.end_ts,
                origin,
                features.get("editorial_review"),
            )
        )
        event = event_map.get(candidate.id)
        if event is not None:
            known_windows.add((event.segment_id, datetime_epoch(candidate.peak_ts)))
    for segment in segments:
        saved = db.get(AppSetting, f"editorial_segment:{segment.id}")
        if saved is None:
            continue
        payload = _load(saved.value)
        if payload.get("schema_version") != "segment-editorial-v1" or payload.get("segment_id") != segment.id:
            raise ValueError("旧链窗口记录身份无效")
        for window in payload["windows"]:
            peak = datetime_epoch(datetime.fromisoformat(window["peak_ts"]))
            if (segment.id, peak) in known_windows:
                continue
            records.append(
                _record(
                    f"segment:{segment.id}:{peak:.6f}",
                    session_id,
                    datetime.fromisoformat(window["start_ts"]),
                    datetime.fromisoformat(window["end_ts"]),
                    origin,
                    window["editorial_review"],
                )
            )
    tasks = list(db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).all())
    analyzed = {
        TaskStatus.CANDIDATE_CREATED,
        TaskStatus.AWAITING_REVIEW,
        TaskStatus.REVIEWED_WAITING_ACTION,
        TaskStatus.APPROVED,
        TaskStatus.APPROVED_WAITING_RENDER,
        TaskStatus.QUEUED_FOR_RENDER,
        TaskStatus.RENDERING,
        TaskStatus.RENDERED,
        TaskStatus.AWAITING_PUBLISH_CONFIRMATION,
        TaskStatus.QUEUED_FOR_PUBLISH,
        TaskStatus.PUBLISHING,
        TaskStatus.COMPLETED,
    }
    complete = (
        recording.ended_at is not None
        and recording.status == "stopped"
        and all(s.status in {SegmentStatus.SCORED, SegmentStatus.ARCHIVED} for s in segments)
        and {t.segment_id for t in tasks if t.stage in analyzed and not t.lease_token} == {s.id for s in segments}
        and not any(h.status in {HotspotStatus.PROVISIONAL, HotspotStatus.ENRICHING} for h in hotspots)
    )
    return EvaluationSession(session_id=session_id, recording_ranges=ranges, analysis_complete=complete), records


def export_snapshot(database: Path, *, dataset_id: str, session_ids: list[int]) -> EvaluationSnapshot:
    """以 SQLite 只读模式导出一致快照，不初始化、迁移或修改业务数据库。"""
    if not database.is_file() or not session_ids or len(session_ids) != len(set(session_ids)):
        raise ValueError("需要现有数据库和互不重复的场次 ID")
    uri = database.resolve().as_uri() + "?mode=ro"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        return connection

    engine = create_engine("sqlite://", creator=connect)
    try:
        with Session(engine) as db:
            db.connection().exec_driver_sql("BEGIN")
            sessions: list[EvaluationSession] = []
            records: list[EvaluationRecord] = []
            for session_id in sorted(session_ids):
                session, rows = _session_records(db, session_id)
                sessions.append(session)
                records.extend(rows)
            return EvaluationSnapshot(
                dataset_id=dataset_id,
                exporter_version=__version__,
                sessions=sessions,
                records=sorted(records, key=lambda r: (r.session_id, r.bounds.start_s, r.key)),
            )
    finally:
        engine.dispose()
