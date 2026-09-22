"""分析阶段 Worker — compute/commit 真正分离。

analyze_compute 只做热点检测与候选评分计算, 不创建 ORM 对象, 不写 DB。
commit_highlight 在租约保护下幂等写入 provisional Hotspot 与 Candidate/Event。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError as _IntegrityError
from sqlmodel import Session, select

from app.analysis import audio as audio_mod
from app.analysis.keywords import match_keywords
from app.analysis.source_policy import session_danmaku_lag_s
from app.core.config import settings
from app.db.entities import (
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    HotspotEvent,
    HotspotStatus,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    SegmentStatus,
    SegmentTask,
    SessionStatus,
    SystemLog,
    TaskStatus,
    Transcript,
)
from app.db.session import get_session
from app.pipeline.highlight_plugins import build_highlight_scoring_request
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_failed
from app.pipeline.task_context import event_first_context, update_event_first_context
from app.plugins.highlight import HighlightDispatch
from app.plugins.manager import plugin_manager

_logger = logging.getLogger(__name__)


class HighlightDecision(StrEnum):
    """分析结果决策类型。"""

    CANDIDATE = "candidate"
    BELOW_THRESHOLD = "below_threshold"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"
    SIGNAL_PASS = "signal_pass"


@dataclass(frozen=True)
class HighlightDraft:
    """纯计算产物 — 不包含任何 ORM 对象, 不可变。"""

    segment_id: int
    session_id: int
    room_id: int | None
    decision: HighlightDecision
    score: float | None
    rule_score: float
    llm_score: float
    highlight_score: float
    start_ts: str | None
    end_ts: str | None
    peak_ts: str | None
    reason: str | None
    dedup_hash: str | None
    features_json: str
    initial_status: str
    config_hash: str
    highlight_plugin: dict[str, object] | None


def _dispatch_payload(dispatch: HighlightDispatch | None) -> dict[str, object] | None:
    """把插件调度结果转换为稳定 JSON 元数据。"""
    if dispatch is None:
        return None
    payload: dict[str, object] = {"plugin_id": dispatch.plugin_id}
    if dispatch.prediction is not None:
        payload["prediction"] = dispatch.prediction.to_dict()
    if dispatch.error is not None:
        payload["error"] = dispatch.error
    return payload


def _effective_primary_score(rule_score: float, dispatch: HighlightDispatch | None) -> float:
    """仅让成功的 Champion 结果替换规则主评分。"""
    prediction = dispatch.prediction if dispatch is not None else None
    if prediction is not None and prediction.uses_champion:
        assert prediction.champion_probability is not None
        return prediction.champion_probability
    return rule_score


def _merge_plugin_metadata(features_json: str, plugin_payload: dict[str, object] | None) -> str:
    """把插件版本、概率和回退原因写入候选特征。"""
    if plugin_payload is None:
        return features_json
    try:
        payload = json.loads(features_json)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["highlight_plugin"] = plugin_payload
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _record_plugin_dispatch(db: Session, compute_result: dict[str, Any]) -> None:
    """在 commit 事务中记录一次插件预测或规则回退。"""
    raw = compute_result.get("highlight_plugin")
    if not isinstance(raw, dict):
        return
    prediction = raw.get("prediction")
    error = raw.get("error")
    if isinstance(prediction, dict) and prediction.get("requested_mode") == "off" and error is None:
        return
    context = dict(raw)
    context.update(
        {
            "segment_id": compute_result.get("segment_id"),
            "session_id": compute_result.get("session_id"),
            "rule_score": compute_result.get("rule_score"),
            "final_score": compute_result.get("highlight_score"),
        }
    )
    db.add(
        SystemLog(
            level="WARNING" if error else "INFO",
            module="plugins",
            room_id=compute_result.get("room_id"),
            event="highlight_scoring_fallback" if error else "highlight_scoring_prediction",
            message="高光评分插件不可用，已回退规则评分" if error else "高光评分插件预测完成",
            context_json=json.dumps(context, ensure_ascii=False, allow_nan=False),
        )
    )


def analyze_compute(task_id: int) -> dict[str, Any]:
    """仅执行分析计算, 不写 DB, 不创建 Candidate/Event。

    返回结构化决策: HighlightDecision + 必要上下文数据。

    :param task_id: SegmentTask ID。
    :returns: 纯计算产物 dict, decision 字段明确表示决策类型。
    """
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return {"error": "task not found", "decision": HighlightDecision.SKIPPED}
        segment_id = task.segment_id
        analysis_pass = event_first_context(task.context_json).get("analysis_pass")
        segment = db.get(RawSegment, segment_id)
        if segment is not None and segment.session_id != task.session_id:
            return {
                "error": (
                    f"任务来源不一致: task={task_id} session={task.session_id},"
                    f" segment={segment_id} session={segment.session_id}"
                ),
                "decision": HighlightDecision.SKIPPED,
                "segment_id": segment_id,
            }
        if segment is None:
            return {"error": "segment not found", "decision": HighlightDecision.SKIPPED, "segment_id": segment_id}
        session_id = segment.session_id
        file_path = segment.file_path

    audio_features: audio_mod.AudioFeatures | None = None
    try:
        audio_features = audio_mod.analyze_audio(file_path)
    except (OSError, RuntimeError) as exc:
        _logger.warning("hotspot_audio_unavailable: segment=%s error=%s", segment_id, exc)

    hotspot_payloads: list[dict[str, object]] = []
    try:
        from app.analysis.hotspot_detector import detect_segment_hotspots, log_hotspot_detection

        hotspot_drafts = detect_segment_hotspots(segment_id, audio_features=audio_features)
        log_hotspot_detection(segment_id, hotspot_drafts)
        hotspot_payloads = [draft.to_payload() for draft in hotspot_drafts]
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _logger.exception("hotspot_detection_failed: segment=%s error=%s", segment_id, exc)

    if analysis_pass == "detect":
        return {
            "decision": HighlightDecision.SIGNAL_PASS,
            "segment_id": segment_id,
            "session_id": session_id,
            "hotspot_drafts": hotspot_payloads,
        }

    if analysis_pass == "candidate":
        event_results = _score_pending_hotspot_events(
            session_id,
            audio_features=audio_features,
            audio_segment_id=segment_id,
        )
        has_candidate = any(result["clip_draft"].decision == HighlightDecision.CANDIDATE for result in event_results)
        return {
            "decision": HighlightDecision.CANDIDATE if has_candidate else HighlightDecision.BELOW_THRESHOLD,
            "segment_id": segment_id,
            "session_id": session_id,
            "hotspot_drafts": hotspot_payloads,
            "event_clip_results": event_results,
        }

    try:
        draft = _score_segment_drafts(segment_id, audio_features=audio_features)
    except ValueError as exc:
        return {
            "error": str(exc),
            "decision": HighlightDecision.SKIPPED,
            "segment_id": segment_id,
            "session_id": session_id,
            "hotspot_drafts": hotspot_payloads,
        }

    if draft is None:
        return {
            "decision": HighlightDecision.BELOW_THRESHOLD,
            "segment_id": segment_id,
            "session_id": session_id,
            "hotspot_drafts": hotspot_payloads,
        }

    # draft 包含 decision 字段 (CANDIDATE / DUPLICATE)
    draft["hotspot_drafts"] = hotspot_payloads
    return draft


def commit_highlight(lease: TaskLease, compute_result: dict[str, Any], ms: int) -> None:
    """单事务提交热点与候选分析结果: 先校验租约再执行 DB 写操作。

    决策分支:
    - BELOW_THRESHOLD: _mark_scored + 推进 Task 到 COMPLETED
    - DUPLICATE: _mark_scored + 推进 Task 到 COMPLETED
    - CANDIDATE: 幂等创建 Candidate + Event + 推进 Task
    - SKIPPED: 推进 Task 到 COMPLETED (记录原因)

    :param lease: 任务租约。
    :param compute_result: analyze_compute 的输出。
    :param ms: 处理耗时 (毫秒)。
    """
    segment_id = compute_result.get("segment_id", 0)
    decision = compute_result.get("decision", HighlightDecision.SKIPPED)

    try:
        with get_session() as db:
            if not still_owns_lease(db, lease):
                raise LeaseLostError()

            task = db.get(SegmentTask, lease.task_id)
            if task is None:
                return
            result_session_id = compute_result.get("session_id", task.session_id)
            segment = db.get(RawSegment, task.segment_id)
            if (
                segment_id != task.segment_id
                or result_session_id != task.session_id
                or segment is None
                or segment.session_id != task.session_id
            ):
                mark_failed(task, "highlight compute result source mismatch", permanent=True)
                db.add(task)
                db.commit()
                return
            raw_hotspots = compute_result.get("hotspot_drafts", [])
            if not isinstance(raw_hotspots, list) or not all(isinstance(item, Mapping) for item in raw_hotspots):
                mark_failed(task, "invalid hotspot compute result", permanent=True)
                db.add(task)
                db.commit()
                return
            from app.analysis.hotspot_detector import persist_provisional_hotspots

            persist_provisional_hotspots(
                db,
                raw_hotspots,
                expected_session_id=task.session_id,
                observed_through=segment.end_ts,
            )

            if decision == HighlightDecision.SIGNAL_PASS:
                attention_windows = _build_hotspot_attention_windows(segment, raw_hotspots)
                has_attention = settings.hotspot_asr_enabled and bool(attention_windows)
                task.context_json = update_event_first_context(
                    task.context_json,
                    analysis_pass="candidate",
                    asr_mode="hotspot" if has_attention else "background",
                    asr_evidence_state="pending",
                    attention_windows=attention_windows if has_attention else [],
                    detector_completed_at=time.time(),
                )
                task.priority = _next_asr_priority(db, task.session_id, hotspot=has_attention)
                task.processing_time_ms = ms
                enqueue_next(task, TaskStatus.QUEUED_FOR_TRANS)
                db.add(task)
                db.commit()
                _logger.info(
                    "signal_pass_committed segment=%s hotspots=%s attention=%s next_priority=%s",
                    segment_id,
                    len(raw_hotspots),
                    len(attention_windows),
                    task.priority,
                )
                return

            raw_event_results = compute_result.get("event_clip_results")
            if isinstance(raw_event_results, list):
                # 先提交本轮热点事实，仍持有 ANALYZING 租约。证据可能因此发生变化，
                # 重新评分与候选/任务的原子提交由同一租约继续完成。
                db.commit()
                _commit_event_analysis(lease, raw_event_results, ms)
                return
            _record_plugin_dispatch(db, compute_result)

            # ── BELOW_THRESHOLD ──────────────────────────
            if decision == HighlightDecision.BELOW_THRESHOLD:
                _logger.info("analyze_below_threshold: segment=%s", segment_id)
                _mark_scored_in_db(db, segment_id)
                mark_completed(task, ms)
                enqueue_next(task, TaskStatus.COMPLETED)
                db.add(task)
                db.commit()
                return

            # ── DUPLICATE ────────────────────────────────
            if decision == HighlightDecision.DUPLICATE:
                _logger.info(
                    "analyze_duplicate: segment=%s dedup_hash=%s",
                    segment_id,
                    compute_result.get("dedup_hash"),
                )
                _mark_scored_in_db(db, segment_id)
                mark_completed(task, ms)
                enqueue_next(task, TaskStatus.COMPLETED)
                db.add(task)
                db.commit()
                return

            # ── SKIPPED ──────────────────────────────────
            if decision == HighlightDecision.SKIPPED:
                reason = compute_result.get("error", compute_result.get("reason", "skipped"))
                _logger.info("analyze_skipped: segment=%s reason=%s", segment_id, reason)
                _mark_scored_in_db(db, segment_id)
                mark_completed(task, ms)
                enqueue_next(task, TaskStatus.COMPLETED)
                db.add(task)
                db.commit()
                return

            # ── CANDIDATE: 同一租约内幂等创建一个或多个 Candidate ─────
            drafts = [compute_result]
            additional = compute_result.get("additional_candidates", [])
            if isinstance(additional, list):
                drafts.extend(item for item in additional if isinstance(item, dict))

            created: list[tuple[int, int]] = []
            from app.analysis.scoring_config import get_scoring_config

            scoring_config = get_scoring_config()
            for draft in drafts:
                if (
                    draft.get("decision") != HighlightDecision.CANDIDATE
                    or draft.get("segment_id", segment_id) != segment_id
                    or draft.get("session_id", task.session_id) != task.session_id
                ):
                    continue
                dedup_hash = draft.get("dedup_hash") or _draft_dedup_hash(draft)
                if _draft_clusters_existing(
                    db,
                    draft,
                    dedup_hash=dedup_hash,
                    cooldown_s=scoring_config.cooldown_s,
                    iou_threshold=scoring_config.iou_threshold,
                ):
                    _logger.info(
                        "analyze_candidate_cluster_suppressed: segment=%s dedup_hash=%s",
                        segment_id,
                        dedup_hash[:16],
                    )
                    continue
                candidate = _get_or_create_candidate(
                    db,
                    dedup_hash,
                    draft["session_id"],
                    draft["peak_ts"],
                    draft["start_ts"],
                    draft["end_ts"],
                    draft["rule_score"],
                    draft.get("llm_score", 0.0),
                    draft["highlight_score"],
                    draft.get("features_json", "{}"),
                    draft.get("reason", ""),
                    draft.get("initial_status", CandidateStatus.PENDING),
                )
                cid = candidate.id
                if cid is None:
                    raise RuntimeError("候选创建后缺少主键")
                event_id = _get_or_create_event(
                    db,
                    cid,
                    draft["session_id"],
                    draft["start_ts"],
                    draft["end_ts"],
                    draft["rule_score"],
                    draft.get("llm_score", 0.0),
                    draft["highlight_score"],
                    draft.get("features_json", "{}"),
                    draft.get("reason", ""),
                    segment_id=segment_id,
                    asr_text=draft.get("asr_text"),
                )
                created.append((cid, event_id))

            if not created:
                _mark_scored_in_db(db, segment_id)
                mark_completed(task, ms)
                enqueue_next(task, TaskStatus.COMPLETED)
                db.add(task)
                db.commit()
                return

            cid, event_id = created[0]
            _logger.info("analyze_candidates_committed: segment=%s count=%s primary=%s", segment_id, len(created), cid)

            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.CANDIDATE_CREATED, candidate_id=cid, event_id=event_id)
            db.add(task)
            db.commit()

    except LeaseLostError:
        _logger.warning("stale_result_discarded: highlight task=%s 已失去租约", lease.task_id)


def _commit_event_analysis(lease: TaskLease, raw_results: list[object], ms: int) -> None:
    """在同一租约下重算过期证据，候选与任务承接原子提交。"""
    from app.analysis.clip_scorer import compute_hotspot_clip_draft, pending_hotspot_event_ids
    from app.analysis.event_enricher import compute_event_enrichment

    results = raw_results
    for attempt in range(2):
        with get_session() as db:
            connection = db.connection()
            if connection.dialect.name == "sqlite":
                # legacy SQLite SELECT 不开启事务；必须先 BEGIN，避免首个 SAVEPOINT
                # 释放时独立提交，导致整批 rollback 仍残留候选。
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            if not still_owns_lease(db, lease):
                raise LeaseLostError()
            task = db.get(SegmentTask, lease.task_id)
            if task is None:
                raise LeaseLostError()
            session_id = task.session_id
            created, processed = _commit_event_clip_results(db, task, results)
            remaining = _unscored_confirmed_event_ids(db, session_id, exclude=processed)
            if not remaining:
                _mark_scored_in_db(db, task.segment_id)
                mark_completed(task, ms)
                if created:
                    primary = max(created, key=lambda item: item[2])
                    enqueue_next(task, TaskStatus.CANDIDATE_CREATED, candidate_id=primary[0], event_id=primary[1])
                else:
                    enqueue_next(task, TaskStatus.COMPLETED)
                db.add(task)
                return
            # 包括本轮已经创建的候选一起撤销，不留下无任务承接的中间产物。
            db.rollback()
            if attempt == 1:
                connection = db.connection()
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                if not still_owns_lease(db, lease):
                    raise LeaseLostError()
                task = db.get(SegmentTask, lease.task_id)
                if task is None:
                    raise LeaseLostError()
                mark_failed(task, "热点证据在提交期间变化，等待重新分析", permanent=False)
                db.add(task)
                return
        # 计算必须在事务之外；最多立即重算一次，持续变化交给原有有界重试。
        results = []
        for event_id in pending_hotspot_event_ids(session_id):
            enrichment = compute_event_enrichment(event_id)
            draft = compute_hotspot_clip_draft(event_id, enrichment=enrichment)
            results.append({"event_id": event_id, "enrichment": enrichment, "clip_draft": draft})


def _score_pending_hotspot_events(
    session_id: int,
    *,
    audio_features: audio_mod.AudioFeatures | None,
    audio_segment_id: int,
) -> list[dict[str, object]]:
    """在事务外依次补全并评分当前场次全部待处理的 confirmed 事件。"""
    from app.analysis.clip_scorer import compute_hotspot_clip_draft, pending_hotspot_event_ids
    from app.analysis.event_enricher import compute_event_enrichment

    results: list[dict[str, object]] = []
    for event_id in pending_hotspot_event_ids(session_id):
        enrichment = compute_event_enrichment(event_id)
        clip_draft = compute_hotspot_clip_draft(
            event_id,
            enrichment=enrichment,
            audio_features=audio_features,
            audio_segment_id=audio_segment_id,
        )
        results.append(
            {
                "event_id": event_id,
                "enrichment": enrichment,
                "clip_draft": clip_draft,
            }
        )
    return results


def _commit_event_clip_results(
    db: Session,
    task: SegmentTask,
    raw_results: list[object],
) -> tuple[list[tuple[int, int, float]], set[int]]:
    """校验事件快照并复用既有 Candidate/Event 幂等提交链。"""
    from app.analysis.clip_scorer import (
        EventClipDraft,
        mark_event_clip_evaluated,
        validate_event_clip_draft,
    )
    from app.analysis.event_enricher import (
        EventEnrichmentDraft,
        commit_event_enrichment,
    )
    from app.analysis.scoring_config import get_scoring_config

    scoring_config = get_scoring_config()
    created: list[tuple[int, int, float]] = []
    processed_event_ids: set[int] = set()
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            raise ValueError("event_clip_results 只能包含对象")
        clip_draft = raw.get("clip_draft")
        enrichment = raw.get("enrichment")
        if not isinstance(clip_draft, EventClipDraft):
            raise ValueError("event_clip_results 缺少 EventClipDraft")
        if enrichment is not None and not isinstance(enrichment, EventEnrichmentDraft):
            raise ValueError("event_clip_results 包含无效 EventEnrichmentDraft")
        if clip_draft.session_id != task.session_id:
            raise ValueError("event_clip_results 与任务场次不一致")
        if enrichment is not None and not commit_event_enrichment(db, enrichment):
            continue
        event = validate_event_clip_draft(db, clip_draft)
        if event is None:
            continue
        processed_event_ids.add(clip_draft.hotspot_event_id)
        committed_draft = clip_draft
        if clip_draft.decision == HighlightDecision.CANDIDATE:
            payload = clip_draft.to_payload()
            if _draft_clusters_existing(
                db,
                payload,
                dedup_hash=clip_draft.dedup_hash,
                cooldown_s=scoring_config.cooldown_s,
                iou_threshold=scoring_config.iou_threshold,
            ):
                committed_draft = replace(
                    clip_draft,
                    decision=HighlightDecision.DUPLICATE,
                    reason=f"{clip_draft.reason}；与既有候选重叠或处于同事件冷却期",
                )
            else:
                candidate = _get_or_create_candidate(
                    db,
                    clip_draft.dedup_hash,
                    clip_draft.session_id,
                    clip_draft.peak_ts,
                    clip_draft.start_ts,
                    clip_draft.end_ts,
                    clip_draft.clip_score,
                    0.0,
                    clip_draft.clip_score,
                    clip_draft.features_json,
                    clip_draft.reason,
                    clip_draft.initial_status,
                )
                if candidate.id is None:
                    raise RuntimeError("事件级候选创建后缺少主键")
                highlight_event_id = _get_or_create_event(
                    db,
                    candidate.id,
                    clip_draft.session_id,
                    clip_draft.start_ts,
                    clip_draft.end_ts,
                    clip_draft.clip_score,
                    0.0,
                    clip_draft.clip_score,
                    clip_draft.features_json,
                    clip_draft.reason,
                    segment_id=clip_draft.segment_id,
                    asr_text=clip_draft.asr_text,
                )
                event.candidate_id = candidate.id
                db.add(event)
                created.append((candidate.id, highlight_event_id, clip_draft.clip_score))
        mark_event_clip_evaluated(db, event, committed_draft)
    return created, processed_event_ids


def _unscored_confirmed_event_ids(
    db: Session,
    session_id: int,
    *,
    exclude: set[int],
) -> list[int]:
    """找出本次持久化后才进入 confirmed 或证据刚变化的事件。"""
    from app.analysis.clip_scorer import event_clip_score_is_current

    rows = db.exec(
        select(HotspotEvent)
        .where(
            HotspotEvent.session_id == session_id,
            HotspotEvent.status == HotspotStatus.CONFIRMED,
            HotspotEvent.candidate_id.is_(None),
        )
        .order_by(HotspotEvent.peak_ts.asc(), HotspotEvent.id.asc())
    ).all()
    return [
        event.id
        for event in rows
        if event.id is not None and event.id not in exclude and not event_clip_score_is_current(db, event)
    ]


def score_hotspot_event(event_id: int) -> HighlightCandidate | None:
    """同步补全并评分一个 confirmed 热点，达到房间阈值时创建候选。"""
    from app.analysis.clip_scorer import compute_hotspot_clip_draft, event_clip_score_is_current
    from app.analysis.event_enricher import compute_event_enrichment

    with get_session() as db:
        current = db.get(HotspotEvent, event_id)
        if current is None:
            raise ValueError(f"HotspotEvent 不存在: event_id={event_id}")
        if current.candidate_id is not None:
            return db.get(HighlightCandidate, current.candidate_id)
        if event_clip_score_is_current(db, current):
            return None
    enrichment = compute_event_enrichment(event_id)
    clip_draft = compute_hotspot_clip_draft(event_id, enrichment=enrichment)
    with get_session() as db:
        event = db.get(HotspotEvent, event_id)
        if event is None:
            raise ValueError(f"HotspotEvent 不存在: event_id={event_id}")
        synthetic_task = SegmentTask(
            segment_id=clip_draft.segment_id,
            session_id=clip_draft.session_id,
            stage=TaskStatus.ANALYZING,
        )
        created, _processed = _commit_event_clip_results(
            db,
            synthetic_task,
            [{"event_id": event_id, "enrichment": enrichment, "clip_draft": clip_draft}],
        )
        if not created:
            return None
        candidate = db.get(HighlightCandidate, created[0][0])
        if candidate is None:
            raise RuntimeError("事件级候选提交后无法读取")
        return candidate


def score_segment_direct(segment_id: int) -> HighlightCandidate | None:
    """供 CLI/同步编排器复用正式多峰评分链，并提交全部去簇候选。

    在旧候选评分前先运行无 LLM HotspotDetector，并独立持久化 provisional
    热点；因此候选路径仍要求 ASR 时，热点事实也不会丢失。

    返回最高分候选以维持既有同步 API；同一分段的其他有效爆点也会创建为
    独立 Candidate/Event，随后统一出现在场次时间线与审核工作台中。
    """
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        file_path = segment.file_path
        session_id = segment.session_id

    audio_features: audio_mod.AudioFeatures | None = None
    try:
        audio_features = audio_mod.analyze_audio(file_path)
    except (OSError, RuntimeError) as exc:
        _logger.warning("hotspot_audio_unavailable: segment=%s error=%s", segment_id, exc)
    try:
        from app.analysis.hotspot_detector import (
            detect_segment_hotspots,
            log_hotspot_detection,
            persist_provisional_hotspots,
        )

        hotspot_drafts = detect_segment_hotspots(segment_id, audio_features=audio_features)
        log_hotspot_detection(segment_id, hotspot_drafts)
        with get_session() as db:
            segment = db.get(RawSegment, segment_id)
            persist_provisional_hotspots(
                db,
                hotspot_drafts,
                expected_session_id=session_id,
                observed_through=segment.end_ts if segment is not None else None,
            )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        _logger.exception("hotspot_detection_failed: segment=%s error=%s", segment_id, exc)

    compute_result = _score_segment_drafts(segment_id, audio_features=audio_features)
    if compute_result is None:
        _mark_scored_direct(segment_id)
        return None
    if compute_result.get("decision") != HighlightDecision.CANDIDATE:
        _mark_scored_direct(segment_id)
        return None

    drafts = [compute_result]
    additional = compute_result.get("additional_candidates", [])
    if isinstance(additional, list):
        drafts.extend(item for item in additional if isinstance(item, dict))

    primary: HighlightCandidate | None = None
    with get_session() as db:
        from app.analysis.scoring_config import get_scoring_config

        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        scoring_config = get_scoring_config()
        for draft in drafts:
            if (
                draft.get("decision") != HighlightDecision.CANDIDATE
                or draft.get("segment_id") != segment_id
                or draft.get("session_id") != segment.session_id
            ):
                continue
            _record_plugin_dispatch(db, draft)
            dedup_hash = draft.get("dedup_hash") or _draft_dedup_hash(draft)
            if _draft_clusters_existing(
                db,
                draft,
                dedup_hash=dedup_hash,
                cooldown_s=scoring_config.cooldown_s,
                iou_threshold=scoring_config.iou_threshold,
            ):
                continue
            candidate = _get_or_create_candidate(
                db,
                dedup_hash,
                draft["session_id"],
                draft["peak_ts"],
                draft["start_ts"],
                draft["end_ts"],
                draft["rule_score"],
                draft.get("llm_score", 0.0),
                draft["highlight_score"],
                draft.get("features_json", "{}"),
                draft.get("reason", ""),
                draft.get("initial_status", CandidateStatus.PENDING),
            )
            if candidate.id is None:
                raise RuntimeError("候选创建后缺少主键")
            _get_or_create_event(
                db,
                candidate.id,
                draft["session_id"],
                draft["start_ts"],
                draft["end_ts"],
                draft["rule_score"],
                draft.get("llm_score", 0.0),
                draft["highlight_score"],
                draft.get("features_json", "{}"),
                draft.get("reason", ""),
                segment_id=segment_id,
                asr_text=draft.get("asr_text"),
            )
            if primary is None:
                primary = candidate
        _mark_scored_in_db(db, segment_id)
    return primary


def _mark_scored_direct(segment_id: int) -> None:
    """在同步评分入口没有候选时持久化分段完成状态。"""
    with get_session() as db:
        _mark_scored_in_db(db, segment_id)


def _build_hotspot_attention_windows(
    segment: RawSegment,
    hotspots: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    """把本段 provisional 热点转换为可恢复的局部 ASR 窗口。"""
    if segment.start_ts is None:
        return []
    if segment.end_ts is not None:
        duration_s = max(0.0, (segment.end_ts - segment.start_ts).total_seconds())
    else:
        duration_s = float(segment.duration_s or settings.segment_duration_s)
    windows: list[dict[str, object]] = []
    seen: set[str] = set()
    for hotspot in hotspots:
        event_key = hotspot.get("event_key")
        peak_ts = hotspot.get("peak_ts")
        if not isinstance(event_key, str) or not event_key or event_key in seen:
            continue
        if not isinstance(peak_ts, datetime):
            continue
        try:
            peak_offset_s = (peak_ts - segment.start_ts).total_seconds()
        except TypeError:
            peak_value = peak_ts.replace(tzinfo=None)
            start_value = segment.start_ts.replace(tzinfo=None)
            peak_offset_s = (peak_value - start_value).total_seconds()
        start_offset_s = max(0.0, peak_offset_s - settings.hotspot_asr_pre_roll_s)
        end_offset_s = min(duration_s, peak_offset_s + settings.hotspot_asr_post_roll_s)
        if end_offset_s - start_offset_s < 1.0:
            continue
        seen.add(event_key)
        windows.append(
            {
                "event_key": event_key,
                "start_offset_s": round(start_offset_s, 3),
                "end_offset_s": round(end_offset_s, 3),
                "status": "pending",
            }
        )
    return windows


def _next_asr_priority(db: Session, session_id: int, *, hotspot: bool) -> int:
    """按热点、近直播、历史后台三档返回持久化任务优先级。"""
    if hotspot:
        return settings.hotspot_asr_priority
    session = db.get(RecordingSession, session_id)
    if session is not None and session.status in {
        SessionStatus.STARTING,
        SessionStatus.RECORDING,
        SessionStatus.RECONNECTING,
        SessionStatus.RECONNECTED,
    }:
        return settings.near_live_asr_priority
    return settings.background_asr_priority


def _draft_dedup_hash(draft: dict[str, Any]) -> str:
    """为缺少业务键的异常计算结果生成稳定候选指纹。"""
    start = draft.get("start_ts")
    end = draft.get("end_ts")
    start_value = start.timestamp() if hasattr(start, "timestamp") else str(start)
    end_value = end.timestamp() if hasattr(end, "timestamp") else str(end)
    return hashlib.sha256(f"{draft.get('session_id', '')}:{start_value}:{end_value}".encode()).hexdigest()


def _draft_clusters_existing(
    db: Session,
    draft: dict[str, Any],
    *,
    dedup_hash: str,
    cooldown_s: float,
    iou_threshold: float,
) -> bool:
    """在提交事务内复核跨分段冷却与 IoU，封住并发计算竞态。"""
    from app.analysis.timeline import datetime_distance_s, interval_iou

    start = draft.get("start_ts")
    end = draft.get("end_ts")
    peak = draft.get("peak_ts")
    session_id = draft.get("session_id")
    if not all(hasattr(value, "timestamp") for value in (start, end, peak)) or not isinstance(session_id, int):
        return False
    existing = db.exec(select(HighlightCandidate).where(HighlightCandidate.session_id == session_id)).all()
    for candidate in existing:
        if candidate.dedup_hash == dedup_hash:
            return False
        if interval_iou(start, end, candidate.start_ts, candidate.end_ts) >= iou_threshold:
            return True
        if (
            cooldown_s > 0
            and datetime_distance_s(peak, candidate.peak_ts) < cooldown_s
            and _draft_shares_cooldown_identity(draft, candidate)
        ):
            return True
    return False


def _draft_shares_cooldown_identity(
    draft: Mapping[str, Any],
    candidate: HighlightCandidate,
) -> bool:
    """旧分段候选保持时间冷却；事件候选仅对同一语义事件应用冷却。"""
    hotspot_event_id = draft.get("hotspot_event_id")
    if not isinstance(hotspot_event_id, int):
        return True
    draft_features = _json_object(draft.get("features_json"))
    candidate_features = _json_object(candidate.features_json)
    if candidate_features.get("hotspot_event_id") == hotspot_event_id:
        return True
    draft_event = draft_features.get("event")
    candidate_event = candidate_features.get("event")
    if not isinstance(draft_event, Mapping) or not isinstance(candidate_event, Mapping):
        return False
    draft_terms = _cooldown_terms(draft_event)
    candidate_terms = _cooldown_terms(candidate_event)
    if not draft_terms or not candidate_terms:
        return False
    return len(draft_terms & candidate_terms) / len(draft_terms | candidate_terms) >= 0.20


def _cooldown_terms(event_payload: Mapping[str, object]) -> set[str]:
    """提取事件标题、类别与实体的稳定冷却身份词。"""
    values: list[str] = []
    for name in ("title", "category"):
        value = event_payload.get(name)
        if isinstance(value, str):
            values.append(value)
    entities = event_payload.get("entities")
    if isinstance(entities, list):
        values.extend(item for item in entities if isinstance(item, str))
    terms: set[str] = set()
    for value in values:
        normalized = "".join(value.casefold().split())
        if not normalized:
            continue
        if any("\u4e00" <= char <= "\u9fff" for char in normalized):
            terms.update(normalized[index : index + 2] for index in range(max(1, len(normalized) - 1)))
        else:
            terms.update(token for token in normalized.replace("_", " ").split() if token)
    return terms


def _json_object(raw: object) -> dict[str, Any]:
    """安全解析候选特征对象。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _get_or_create_candidate(
    db,
    dedup_hash: str,
    session_id: int,
    peak_ts,
    start_ts,
    end_ts,
    rule_score: float,
    llm_score: float,
    highlight_score: float,
    features_json: str,
    reason: str,
    initial_status: str,
) -> HighlightCandidate:
    """并发幂等获取或创建 HighlightCandidate。

    按稳定业务键 dedup_hash 查询:
    → 存在则复用
    → 不存在则尝试插入并 flush
    → IntegrityError → rollback savepoint → 重新查询已存在记录

    :param db: SQLModel session。
    :param dedup_hash: 稳定内容指纹 (业务唯一键)。
    :returns: 已有或新创建的 HighlightCandidate。
    """
    # 1) 优先查询已有 Candidate
    existing = db.exec(select(HighlightCandidate).where(HighlightCandidate.dedup_hash == dedup_hash)).first()
    if existing is not None:
        _logger.info(
            "candidate_reused: cid=%s dedup_hash=%s (幂等复用)",
            existing.id,
            dedup_hash[:16],
        )
        return existing

    # 2) 不存在 → 尝试插入 (在 savepoint 中)
    candidate = HighlightCandidate(
        session_id=session_id,
        peak_ts=peak_ts,
        start_ts=start_ts,
        end_ts=end_ts,
        rule_score=rule_score,
        llm_score=llm_score,
        highlight_score=highlight_score,
        features_json=features_json,
        reason=reason,
        status=initial_status,
        dedup_hash=dedup_hash,
    )
    try:
        with db.begin_nested():
            db.add(candidate)
            db.flush()
        db.refresh(candidate)
        _logger.info(
            "candidate_created: cid=%s dedup_hash=%s score=%.3f",
            candidate.id,
            dedup_hash[:16],
            highlight_score,
        )
        return candidate
    except _IntegrityError:
        # 并发冲突 — 回滚 savepoint, 查询对方创建的记录
        _logger.info(
            "candidate_conflict_resolved: dedup_hash=%s (并发创建, 复用已有)",
            dedup_hash[:16],
        )
        existing = db.exec(select(HighlightCandidate).where(HighlightCandidate.dedup_hash == dedup_hash)).first()
        if existing is not None:
            return existing
        # 极端情况: 冲突后仍查不到 (不可能, 但做防御)
        raise AssertionError(f"IntegrityError on dedup_hash={dedup_hash[:16]} but existing record not found") from None


def _get_or_create_event(
    db,
    candidate_id: int,
    session_id: int,
    raw_start_ts,
    raw_end_ts,
    rule_score: float,
    llm_score: float,
    highlight_score: float,
    features_json: str,
    reason: str,
    *,
    segment_id: int | None = None,
    asr_text: str | None = None,
) -> int:
    """并发幂等获取或创建 HighlightEvent。

    按 candidate_id 查询 (表级唯一约束保护):
    → 存在则返回已有 event_id
    → 不存在则尝试插入
    → IntegrityError → rollback savepoint → 重新查询

    :param db: SQLModel session。
    :param candidate_id: 关联的 HighlightCandidate ID。
    :returns: event_id。
    """
    # 1) 优先查询
    existing = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
    if existing is not None:
        if existing.segment_id is None and segment_id is not None:
            existing.segment_id = segment_id
            db.add(existing)
        if existing.asr_text is None and asr_text:
            existing.asr_text = asr_text
            db.add(existing)
        _logger.debug("event_reused: eid=%s cid=%s (幂等复用)", existing.id, candidate_id)
        return existing.id

    # 2) 尝试插入
    event = HighlightEvent(
        candidate_id=candidate_id,
        session_id=session_id,
        segment_id=segment_id,
        raw_start_ts=raw_start_ts,
        raw_end_ts=raw_end_ts,
        rule_score=rule_score,
        llm_score=llm_score,
        highlight_score=highlight_score,
        features_json=features_json,
        reason=reason,
        asr_text=asr_text,
        review_status=ReviewStatus.PENDING,
        review_by="auto",
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
        db.refresh(event)
        _logger.info("auto_event: eid=%s cid=%s", event.id, candidate_id)
        return event.id
    except _IntegrityError:
        # 并发冲突 — 回滚 savepoint, 查询对方创建的记录
        _logger.info(
            "event_conflict_resolved: cid=%s (并发创建, 复用已有)",
            candidate_id,
        )
        existing = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
        if existing is not None:
            return existing.id
        raise AssertionError(f"IntegrityError on candidate_id={candidate_id} but existing Event not found") from None


def _mark_scored_in_db(db, segment_id: int) -> None:
    """在已有 DB session 中标记片段为已评分 (仅 commit 阶段使用)。

    :param db: SQLModel session。
    :param segment_id: RawSegment ID。
    """
    seg = db.get(RawSegment, segment_id)
    if seg is not None and seg.status != SegmentStatus.SCORED:
        seg.status = SegmentStatus.SCORED
        db.add(seg)


def run_analyze(lease: TaskLease) -> None:
    """执行分析阶段: 计算与提交分离。

    心跳由 scheduler 的 heartbeat thread 管理, 不在 run_* 中重复写入。

    :param lease: 任务租约。
    """
    t0 = time.time()
    compute_result = analyze_compute(lease.task_id)
    ms_val = int((time.time() - t0) * 1000)
    commit_highlight(lease, compute_result, ms_val)


# ══════════════════════════════════════════
# 纯计算辅助函数: 不写任何业务对象
# ══════════════════════════════════════════


def _score_segment_drafts(
    segment_id: int,
    *,
    audio_features: audio_mod.AudioFeatures | None = None,
) -> dict[str, Any] | None:
    """对一个录制分段的多个局部峰值分别评分并执行防扎堆筛选。

    返回值以最高分候选作为顶层结果供任务状态机推进；其余候选
    放在 ``additional_candidates`` 中，由同一个租约事务幂等提交。
    """
    from app.analysis.timeline import suppress_clustered_drafts  # noqa: PLC0415

    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        file_path = segment.file_path

    features = audio_features or audio_mod.analyze_audio(file_path)
    peak_offsets = features.peak_offsets(
        limit=settings.highlight_max_candidates_per_segment,
        min_distance_s=settings.highlight_peak_min_distance_s,
    )
    offsets = audio_mod.analysis_probe_offsets(
        peak_offsets,
        duration_s=features.duration_s or float(segment.duration_s or settings.segment_duration_s),
        limit=settings.highlight_max_candidates_per_segment,
        min_distance_s=settings.highlight_peak_min_distance_s,
    )

    outcomes: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for peak_offset in offsets:
        draft = _score_segment_draft(
            segment_id,
            peak_offset_s=peak_offset,
            audio_features=features,
        )
        if draft is None:
            continue
        outcomes.append(draft)
        if draft.get("decision") == HighlightDecision.CANDIDATE:
            candidates.append(draft)

    if not candidates:
        if not outcomes:
            return None
        return max(outcomes, key=lambda item: float(item.get("highlight_score", 0.0)))

    from app.analysis.scoring_config import get_scoring_config  # noqa: PLC0415

    cfg = get_scoring_config()
    selected = suppress_clustered_drafts(
        candidates,
        cooldown_s=cfg.cooldown_s,
        iou_threshold=cfg.iou_threshold,
        limit=settings.highlight_max_candidates_per_segment,
    )
    if not selected:
        return None
    primary = max(selected, key=lambda item: float(item.get("highlight_score", 0.0)))
    primary["additional_candidates"] = [draft for draft in selected if draft is not primary]
    primary["candidate_count"] = len(selected)
    return primary


def _score_segment_draft(
    segment_id: int,
    *,
    peak_offset_s: float | None = None,
    audio_features: audio_mod.AudioFeatures | None = None,
) -> dict[str, Any] | None:
    """纯评分计算, 不写 DB, 不创建 Candidate。

    返回 dict 包含 decision 字段:
    - CANDIDATE: 通过评分, 应创建候选
    - DUPLICATE: 去重命中
    返回 None: 初筛未过或终分不足 (由调用方转为 BELOW_THRESHOLD)

    :param segment_id: RawSegment ID。
    :param peak_offset_s: 指定的局部峰值；省略时使用全局峰值。
    :param audio_features: 已提取的音频特征，供多峰评分复用。
    :returns: draft dict 含 decision 字段, 或 None (分数不足)。
    """
    from app.analysis import llm as llm_mod
    from app.analysis.highlight import (  # noqa: PLC0415
        _audio_events_score,
        _audio_meta,
        _is_duplicate,
        _trend_score,
        candidate_time_bounds,
        contiguous_recording_range,
        danmaku_score_explain,
        danmaku_sentiment_score,
        fuse_scores,
        laughter_score,
        speech_rate_score,
        weighted_rule_score,
    )
    from app.analysis.highlight import (
        _danmaku_score as _dm_score,
    )
    from app.analysis.scoring_config import get_scoring_config  # noqa: PLC0415
    from app.analysis.transcription.quality import assess_transcript_quality  # noqa: PLC0415

    cfg = get_scoring_config()

    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment_id)).first()
        session = db.get(RecordingSession, segment.session_id)
        room = db.get(LiveRoom, session.room_id) if session else None
        seg_start_ts = segment.start_ts
        seg_end_ts = segment.end_ts
        if seg_start_ts is None or seg_end_ts is None:
            return None
        duration = segment.duration_s or float(settings.segment_duration_s)
        segment_seq = segment.seq
        session_id = segment.session_id
        room_id = room.id if room else None
        threshold = room.highlight_threshold if room else settings.highlight_threshold
        has_transcript = transcript is not None
        text = transcript.final_text if transcript else ""
        file_path = segment.file_path
        room_auto_approve = bool(room.auto_approve) if room else False
        room_auto_approve_threshold = room.auto_approve_threshold if room else settings.highlight_auto_approve_threshold
        room_review_threshold = room.review_threshold if room else settings.highlight_review_threshold
        from app.core.settings_store import get_bool

        use_dm_sentiment = (
            get_bool("danmaku_sentiment_enabled") and room is not None and bool(room.danmaku_sentiment_enabled)
        )
        session_segments = db.exec(
            select(RawSegment).where(RawSegment.session_id == segment.session_id).order_by(RawSegment.seq.asc())
        ).all()
        loaded_transcripts = db.exec(
            select(Transcript).where(Transcript.segment_id.in_([item.id for item in session_segments]))
        ).all()
        session_transcripts = {
            item.segment_id: item for item in loaded_transcripts if assess_transcript_quality(item.final_text).usable
        }
        available_start, available_end = contiguous_recording_range(session_segments, segment)

    transcript_quality = assess_transcript_quality(text) if has_transcript else None
    asr_evidence_state = (
        "unavailable" if transcript_quality is None else ("available" if transcript_quality.usable else "degraded")
    )
    asr_evidence_reason = transcript_quality.reason if transcript_quality is not None else "missing_transcript"

    # 1) 规则特征
    feats = audio_features or audio_mod.analyze_audio(file_path)
    peak_off = feats.peak_offset() if peak_offset_s is None else max(0.0, min(float(peak_offset_s), duration))
    peak_ts = seg_start_ts + timedelta(seconds=peak_off)
    analysis_start_ts = max(available_start, peak_ts - timedelta(seconds=cfg.pre_roll_s))
    analysis_end_ts = min(available_end, peak_ts + timedelta(seconds=cfg.post_roll_s))
    analysis_start_s = (analysis_start_ts - seg_start_ts).total_seconds()
    analysis_end_s = (analysis_end_ts - seg_start_ts).total_seconds()
    from app.analysis.transcript_windows import (  # noqa: PLC0415
        TimedTranscriptPart,
        extract_session_transcript_window,
    )

    transcript_parts = [
        TimedTranscriptPart(
            start_ts=item.start_ts,
            end_ts=item.end_ts,
            text=session_transcripts[item.id].final_text,
            words_json=session_transcripts[item.id].words_json,
        )
        for item in session_segments
        if item.id in session_transcripts and item.start_ts is not None and item.end_ts is not None
    ]
    analysis_window = extract_session_transcript_window(
        transcript_parts,
        start_ts=analysis_start_ts,
        end_ts=analysis_end_ts,
    )
    judgement_text = analysis_window.text
    analysis_duration_s = (analysis_end_ts - analysis_start_ts).total_seconds()
    from app.analysis.timeline import (  # noqa: PLC0415
        TIMELINE_ANALYSIS_VERSION,
        align_danmaku_window,
        confidence_score,
        representative_danmaku,
        source_signals,
    )

    lag_s = session_danmaku_lag_s(session_id)
    danmaku_start_ts, danmaku_end_ts = align_danmaku_window(analysis_start_ts, analysis_end_ts, lag_s=lag_s)
    semantic_text_available = bool(judgement_text.strip())
    kw_score, kw_hits = match_keywords(judgement_text) if semantic_text_available else (0.0, [])
    from app.analysis.source_policy import session_has_danmaku

    has_danmaku = session_has_danmaku(session_id, danmaku_start_ts, danmaku_end_ts)
    features: dict[str, float] = {"volume": feats.volume_score()}
    if has_danmaku:
        features["danmaku"] = _dm_score(session_id, danmaku_start_ts, danmaku_end_ts)
    if semantic_text_available:
        features.update(
            {
                "keywords": kw_score,
                "speech_rate": speech_rate_score(analysis_window.words, analysis_duration_s),
                "laughter": laughter_score(judgement_text),
            }
        )
    if use_dm_sentiment and has_danmaku:
        features["danmaku_sentiment"] = danmaku_sentiment_score(
            session_id,
            danmaku_start_ts,
            danmaku_end_ts,
        )
    audio_event_contribs: list[str] = []
    if settings.asr_sensevoice and settings.asr_sensevoice_enabled:
        aux_json = transcript.auxiliary_json if transcript else None
        audio_evt_score, audio_event_contribs = _audio_events_score(aux_json)
        if audio_evt_score > 0:
            features["audio_events"] = audio_evt_score
    trend_hits: list[str] = []
    if settings.trend_enabled and semantic_text_available:
        trend_score, trend_hits = _trend_score(judgement_text)
        features["trend"] = trend_score
    rule_score = weighted_rule_score(features, cfg.weights)
    plugin_dispatch: HighlightDispatch | None = None
    if plugin_manager.has_capability("highlight_scorer"):
        try:
            request = build_highlight_scoring_request(
                segment_id,
                audio_features=feats,
                rule_score=rule_score,
            )
            plugin_dispatch = plugin_manager.score_highlight(request)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            plugin_dispatch = HighlightDispatch(
                plugin_id="host",
                error=f"{type(exc).__name__}: {exc}",
            )
            _logger.warning("highlight_plugin_context_fallback segment=%s error=%s", segment_id, exc)
    plugin_payload = _dispatch_payload(plugin_dispatch)
    primary_score = _effective_primary_score(rule_score, plugin_dispatch)

    _logger.info(
        "score_draft segment=%s rule=%.3f primary=%.3f features=%s kw_hits=%s trend_hits=%s",
        segment_id,
        rule_score,
        primary_score,
        {k: round(v, 3) for k, v in features.items()},
        kw_hits,
        trend_hits,
    )

    # 2) 初筛 — 不写 DB, 返回显式决策以便 commit 留存插件观测。
    if primary_score < settings.highlight_init_threshold:
        return {
            "decision": HighlightDecision.BELOW_THRESHOLD,
            "segment_id": segment_id,
            "session_id": session_id,
            "room_id": room_id,
            "score": primary_score,
            "rule_score": rule_score,
            "llm_score": 0.0,
            "highlight_score": primary_score,
            "start_ts": None,
            "end_ts": None,
            "peak_ts": None,
            "reason": "低于初筛阈值",
            "dedup_hash": None,
            "features_json": _merge_plugin_metadata("{}", plugin_payload),
            "initial_status": CandidateStatus.REJECTED,
            "config_hash": cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
            "highlight_plugin": plugin_payload,
        }

    # 3) LLM 复核
    top_danmaku = representative_danmaku(session_id, analysis_start_ts, analysis_end_ts)
    danmaku_summary = "；".join(str(item["text"]) for item in top_danmaku)
    judgement = (
        llm_mod.judge_highlight(judgement_text, features, danmaku_summary, analysis_start_s)
        if semantic_text_available
        else None
    )
    llm_score = judgement.score if judgement else None
    if judgement is not None:
        reason = judgement.reason
    elif asr_evidence_state == "degraded":
        reason = "ASR 语义证据质量较低，依据音频与互动信号命中"
    elif asr_evidence_state == "unavailable":
        reason = "ASR 语义证据不可用，依据音频与互动信号命中"
    else:
        reason = "规则命中(未启用/未触发 LLM)"
    highlight_score = fuse_scores(primary_score, llm_score, cfg.alpha, cfg.beta)

    # 终分不足 — 不写 DB, 返回显式决策。
    if highlight_score < threshold:
        return {
            "decision": HighlightDecision.BELOW_THRESHOLD,
            "segment_id": segment_id,
            "session_id": session_id,
            "room_id": room_id,
            "score": highlight_score,
            "rule_score": rule_score,
            "llm_score": llm_score or 0.0,
            "highlight_score": highlight_score,
            "start_ts": None,
            "end_ts": None,
            "peak_ts": None,
            "reason": "低于候选阈值",
            "dedup_hash": None,
            "features_json": _merge_plugin_metadata("{}", plugin_payload),
            "initial_status": CandidateStatus.REJECTED,
            "config_hash": cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
            "highlight_plugin": plugin_payload,
        }

    # 4) 边界吸附
    start_ts, end_ts, peak_ts = candidate_time_bounds(
        segment_start=seg_start_ts,
        available_start=available_start,
        available_end=available_end,
        peak_offset_s=peak_off,
        pre_roll_s=cfg.pre_roll_s,
        post_roll_s=cfg.post_roll_s,
        suggested_start_offset_s=judgement.suggested_start_offset if judgement else None,
        suggested_end_offset_s=judgement.suggested_end_offset if judgement else None,
        silences=feats.silences,
        minimum_pre_roll_s=min(cfg.pre_roll_s, settings.highlight_min_pre_roll_s),
        minimum_post_roll_s=min(cfg.post_roll_s, settings.highlight_min_post_roll_s),
    )

    # 5) 去重 — 不写 DB, 返回 DUPLICATE 决策
    if _is_duplicate(
        session_id,
        (start_ts.timestamp(), end_ts.timestamp()),
        cfg.iou_threshold,
        peak_ts=peak_ts,
        cooldown_s=cfg.cooldown_s,
    ):
        dedup_hash_val = hashlib.sha256(
            f"{session_id}:{start_ts.timestamp():.1f}:{end_ts.timestamp():.1f}".encode()
        ).hexdigest()
        return {
            "decision": HighlightDecision.DUPLICATE,
            "segment_id": segment_id,
            "session_id": session_id,
            "room_id": room_id,
            "dedup_hash": dedup_hash_val,
            "score": highlight_score,
            "rule_score": rule_score,
            "llm_score": llm_score or 0.0,
            "highlight_score": highlight_score,
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
            "peak_ts": peak_ts.isoformat(),
            "reason": "去重: IoU over threshold",
            "features_json": _merge_plugin_metadata("{}", plugin_payload),
            "initial_status": CandidateStatus.REJECTED,
            "config_hash": cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
            "highlight_plugin": plugin_payload,
        }

    # 6) 审核状态
    if room_auto_approve and highlight_score >= room_auto_approve_threshold:
        initial_status = CandidateStatus.APPROVED
    elif highlight_score >= room_review_threshold:
        initial_status = CandidateStatus.PENDING
    else:
        initial_status = CandidateStatus.REJECTED

    danmaku_explain = (
        danmaku_score_explain(session_id, danmaku_start_ts, danmaku_end_ts)
        if has_danmaku
        else {"available": False, "reason": "missing_capture_coverage"}
    )
    signals = source_signals(features, keyword_hits=kw_hits)
    confidence = confidence_score(rule_score, llm_score, signals)

    features_json = _merge_plugin_metadata(
        json.dumps(
            {
                "features": features,
                "keyword_hits": kw_hits,
                "audio": _audio_meta(feats),
                "danmaku_explain": danmaku_explain,
                "asr_evidence": {
                    "state": asr_evidence_state,
                    "reason": asr_evidence_reason,
                    "semantic_window_available": semantic_text_available,
                    "repetition_ratio": (
                        round(transcript_quality.repetition_ratio, 6) if transcript_quality is not None else None
                    ),
                },
                "timeline": {
                    "analysis_version": TIMELINE_ANALYSIS_VERSION,
                    "confidence": confidence,
                    "source_signals": signals,
                    "representative_danmaku": top_danmaku,
                    "danmaku_lag_s": lag_s,
                    "dynamic_bounds": True,
                    "cross_segment": start_ts < seg_start_ts or end_ts > seg_end_ts,
                },
                "analysis_window": {
                    "segment_id": segment_id,
                    "segment_seq": segment_seq,
                    "start_offset_s": round(analysis_start_s, 3),
                    "end_offset_s": round(analysis_end_s, 3),
                    "precise_transcript": analysis_window.precise,
                },
            },
            ensure_ascii=False,
        ),
        plugin_payload,
    )

    dedup_hash_val = hashlib.sha256(
        f"{session_id}:{start_ts.timestamp():.1f}:{end_ts.timestamp():.1f}".encode()
    ).hexdigest()

    return {
        "decision": HighlightDecision.CANDIDATE,
        "segment_id": segment_id,
        "session_id": session_id,
        "room_id": room_id,
        "peak_ts": peak_ts,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "rule_score": rule_score,
        "llm_score": llm_score or 0.0,
        "highlight_score": highlight_score,
        "features_json": features_json,
        "reason": reason,
        "asr_text": judgement_text,
        "initial_status": initial_status,
        "dedup_hash": dedup_hash_val,
        "score": highlight_score,
        "config_hash": cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
        "highlight_plugin": plugin_payload,
    }
