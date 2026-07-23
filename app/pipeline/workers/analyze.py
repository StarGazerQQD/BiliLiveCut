"""分析阶段 Worker — compute/commit 真正分离。

analyze_compute 只做评分计算, 不创建 Candidate/Event, 不写 DB。
commit_highlight 在租约保护下实现真正并发幂等的 Candidate + Event 创建。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError as _IntegrityError
from sqlmodel import select

from app.analysis import audio as audio_mod
from app.analysis.highlight_ml.online import (
    OnlinePrediction,
    add_prediction_log,
    effective_primary_score,
    merge_prediction_metadata,
    predict_online,
)
from app.analysis.keywords import match_keywords
from app.core.config import settings
from app.db.models import (
    CandidateStatus,
    HighlightCandidate,
    HighlightEvent,
    LiveRoom,
    RawSegment,
    RecordingSession,
    ReviewStatus,
    SegmentStatus,
    SegmentTask,
    TaskStatus,
    Transcript,
)
from app.db.session import get_session
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed

_logger = logging.getLogger(__name__)


class HighlightDecision(StrEnum):
    """分析结果决策类型。"""

    CANDIDATE = "candidate"
    BELOW_THRESHOLD = "below_threshold"
    DUPLICATE = "duplicate"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class HighlightDraft:
    """纯计算产物 — 不包含任何 ORM 对象, 不可变。"""

    segment_id: int
    session_id: int | None
    room_id: int | None
    decision: HighlightDecision
    score: float | None
    rule_score: float
    llm_score: float
    highlight_score: float
    start_ts: datetime | None
    end_ts: datetime | None
    peak_ts: datetime | None
    reason: str | None
    dedup_hash: str | None
    features_json: str
    initial_status: str
    config_hash: str
    ml_prediction: OnlinePrediction

    def to_dict(self) -> dict[str, Any]:
        """返回 Worker commit 阶段可直接消费的稳定字典。"""
        return asdict(self)


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

    try:
        draft = _score_segment_draft(segment_id)
    except ValueError as exc:
        return {"error": str(exc), "decision": HighlightDecision.SKIPPED, "segment_id": segment_id}

    return draft.to_dict()


def commit_highlight(lease: TaskLease, compute_result: dict[str, Any], ms: int) -> None:
    """单事务提交分析结果: 先校验租约, 按决策类型执行 DB 写操作。

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

            prediction_payload = compute_result.get("ml_prediction")
            if isinstance(prediction_payload, dict):
                prediction = OnlinePrediction(**prediction_payload)
                add_prediction_log(
                    db,
                    prediction=prediction,
                    segment_id=segment_id,
                    session_id=compute_result.get("session_id"),
                    room_id=compute_result.get("room_id"),
                    rule_score=float(compute_result.get("rule_score", 0.0)),
                    final_score=compute_result.get("highlight_score"),
                )

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

            # ── CANDIDATE: 幂等创建 Candidate ───────────
            dedup_hash = (
                compute_result.get("dedup_hash")
                or hashlib.sha1(
                    f"{compute_result.get('session_id', '')}:"
                    f"{round(compute_result.get('start_ts', ''))}:"
                    f"{round(compute_result.get('end_ts', ''))}".encode()
                ).hexdigest()
            )

            candidate = _get_or_create_candidate(
                db,
                dedup_hash,
                compute_result["session_id"],
                compute_result["peak_ts"],
                compute_result["start_ts"],
                compute_result["end_ts"],
                compute_result["rule_score"],
                compute_result.get("llm_score", 0.0),
                compute_result["highlight_score"],
                compute_result.get("features_json", "{}"),
                compute_result.get("reason", ""),
                compute_result.get("initial_status", CandidateStatus.PENDING),
            )
            cid = candidate.id

            # 幂等创建 Event (含 IntegrityError 保护)
            event_id = _get_or_create_event(
                db,
                cid,
                compute_result["session_id"],
                compute_result["start_ts"],
                compute_result["end_ts"],
                compute_result["rule_score"],
                compute_result.get("llm_score", 0.0),
                compute_result["highlight_score"],
                compute_result.get("features_json", "{}"),
                compute_result.get("reason", ""),
            )

            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.CANDIDATE_CREATED, candidate_id=cid, event_id=event_id)
            db.add(task)
            db.commit()

    except LeaseLostError:
        _logger.warning("stale_result_discarded: highlight task=%s 已失去租约", lease.task_id)


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
        _logger.debug("event_reused: eid=%s cid=%s (幂等复用)", existing.id, candidate_id)
        return existing.id

    # 2) 尝试插入
    event = HighlightEvent(
        candidate_id=candidate_id,
        session_id=session_id,
        raw_start_ts=raw_start_ts,
        raw_end_ts=raw_end_ts,
        rule_score=rule_score,
        llm_score=llm_score,
        highlight_score=highlight_score,
        features_json=features_json,
        reason=reason,
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


def _score_segment_draft(segment_id: int) -> HighlightDraft:
    """纯评分计算, 不写 DB, 不创建 Candidate。

    返回不可变 ``HighlightDraft``，其中 decision 字段取值:
    - CANDIDATE: 通过评分, 应创建候选
    - DUPLICATE: 去重命中
    :param segment_id: RawSegment ID。
    :returns: 显式决策及其完整评分上下文。
    """
    from app.analysis import llm as llm_mod
    from app.analysis.highlight import (  # noqa: PLC0415
        _audio_events_score,
        _audio_meta,
        _is_duplicate,
        _trend_score,
        danmaku_score_explain,
        danmaku_sentiment_score,
        fuse_scores,
        get_scoring_config,
        laughter_score,  # noqa: PLC0415
        speech_rate_score,
        weighted_rule_score,
    )
    from app.analysis.highlight import (
        _danmaku_score as _dm_score,
    )

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
            return HighlightDraft(
                segment_id=segment_id,
                session_id=segment.session_id,
                room_id=room.id if room else None,
                decision=HighlightDecision.SKIPPED,
                score=None,
                rule_score=0.0,
                llm_score=0.0,
                highlight_score=0.0,
                start_ts=None,
                end_ts=None,
                peak_ts=None,
                reason="片段缺少时间边界",
                dedup_hash=None,
                features_json="{}",
                initial_status=CandidateStatus.REJECTED,
                config_hash=cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
                ml_prediction=OnlinePrediction(requested_mode="off", effective_mode="off"),
            )
        duration = segment.duration_s or float(settings.segment_duration_s)
        session_id = segment.session_id
        threshold = room.highlight_threshold if room else settings.highlight_threshold
        has_transcript = transcript is not None
        text = transcript.text if transcript else ""
        words_json = transcript.words_json if transcript else None
        file_path = segment.file_path
        room_id = room.id if room else None
        room_config_json = room.room_config_json if room else None
        room_auto_approve = bool(room.auto_approve) if room else False
        room_auto_approve_threshold = room.auto_approve_threshold if room else 0.82
        room_review_threshold = room.review_threshold if room else 0.50
        use_dm_sentiment = room is not None and bool(room.danmaku_sentiment_enabled) and settings.collect_danmaku

    if not has_transcript:
        raise ValueError(f"片段尚未转写: id={segment_id}")

    words = json.loads(words_json) if words_json else []

    # 1) 规则特征
    feats = audio_mod.analyze_audio(file_path)
    kw_score, kw_hits = match_keywords(text)
    features: dict[str, float] = {
        "volume": feats.volume_score(),
        "keywords": kw_score,
        "speech_rate": speech_rate_score(words, duration),
        "laughter": laughter_score(text),
        "danmaku": _dm_score(session_id, seg_start_ts, seg_end_ts),
    }
    if use_dm_sentiment:
        features["danmaku_sentiment"] = danmaku_sentiment_score(session_id, seg_start_ts, seg_end_ts)
    audio_event_contribs: list[str] = []
    if settings.asr_sensevoice and settings.asr_sensevoice_enabled:
        aux_json = transcript.auxiliary_json if transcript else None
        audio_evt_score, audio_event_contribs = _audio_events_score(aux_json)
        if audio_evt_score > 0:
            features["audio_events"] = audio_evt_score
    trend_hits: list[str] = []
    if settings.trend_enabled:
        trend_score, trend_hits = _trend_score(text)
        features["trend"] = trend_score
    rule_score = weighted_rule_score(features, cfg.weights)
    ml_prediction = predict_online(
        segment_id,
        audio_features=feats,
        room_config_json=room_config_json,
    )
    primary_score = effective_primary_score(rule_score, ml_prediction)

    _logger.info(
        "score_draft segment=%s rule=%.3f features=%s kw_hits=%s trend_hits=%s",
        segment_id,
        rule_score,
        {k: round(v, 3) for k, v in features.items()},
        kw_hits,
        trend_hits,
    )

    # 2) 初筛 — 不写 DB，返回显式 BELOW_THRESHOLD
    if primary_score < settings.highlight_init_threshold:
        return HighlightDraft(
            segment_id=segment_id,
            session_id=session_id,
            room_id=room_id,
            decision=HighlightDecision.BELOW_THRESHOLD,
            score=primary_score,
            rule_score=rule_score,
            llm_score=0.0,
            highlight_score=primary_score,
            start_ts=None,
            end_ts=None,
            peak_ts=None,
            reason="低于初筛阈值",
            dedup_hash=None,
            features_json=merge_prediction_metadata("{}", ml_prediction),
            initial_status=CandidateStatus.REJECTED,
            config_hash=cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
            ml_prediction=ml_prediction,
        )

    # 3) LLM 复核
    judgement = llm_mod.judge_highlight(text, features)
    llm_score = judgement.score if judgement else None
    reason = judgement.reason if judgement else "规则命中(未启用/未触发 LLM)"
    highlight_score = fuse_scores(primary_score, llm_score, cfg.alpha, cfg.beta)

    # 终分不足 — 不写 DB，返回显式 BELOW_THRESHOLD
    if highlight_score < threshold:
        return HighlightDraft(
            segment_id=segment_id,
            session_id=session_id,
            room_id=room_id,
            decision=HighlightDecision.BELOW_THRESHOLD,
            score=highlight_score,
            rule_score=rule_score,
            llm_score=llm_score or 0.0,
            highlight_score=highlight_score,
            start_ts=None,
            end_ts=None,
            peak_ts=None,
            reason="低于候选阈值",
            dedup_hash=None,
            features_json=merge_prediction_metadata("{}", ml_prediction),
            initial_status=CandidateStatus.REJECTED,
            config_hash=cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
            ml_prediction=ml_prediction,
        )

    # 4) 边界吸附
    peak_off = feats.peak_offset()
    if judgement and judgement.suggested_start_offset is not None:
        start_off = judgement.suggested_start_offset
    else:
        start_off = peak_off - cfg.pre_roll_s
    if judgement and judgement.suggested_end_offset is not None:
        end_off = judgement.suggested_end_offset
    else:
        end_off = peak_off + cfg.post_roll_s

    start_off = audio_mod.snap_to_silence(start_off, feats.silences)
    end_off = audio_mod.snap_to_silence(end_off, feats.silences)

    peak_ts = seg_start_ts + timedelta(seconds=peak_off)
    start_ts = seg_start_ts + timedelta(seconds=start_off)
    end_ts = seg_start_ts + timedelta(seconds=end_off)

    # 5) 去重 — 不写 DB, 返回 DUPLICATE 决策
    if _is_duplicate(session_id, (start_ts.timestamp(), end_ts.timestamp()), cfg.iou_threshold):
        dedup_hash_val = hashlib.sha1(
            f"{session_id}:{start_ts.timestamp():.1f}:{end_ts.timestamp():.1f}".encode()
        ).hexdigest()
        return HighlightDraft(
            segment_id=segment_id,
            session_id=session_id,
            room_id=room_id,
            decision=HighlightDecision.DUPLICATE,
            score=highlight_score,
            rule_score=rule_score,
            llm_score=llm_score or 0.0,
            highlight_score=highlight_score,
            start_ts=start_ts,
            end_ts=end_ts,
            peak_ts=peak_ts,
            reason="去重: IoU over threshold",
            dedup_hash=dedup_hash_val,
            features_json=merge_prediction_metadata("{}", ml_prediction),
            initial_status=CandidateStatus.REJECTED,
            config_hash=cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
            ml_prediction=ml_prediction,
        )

    # 6) 审核状态
    if room_auto_approve and highlight_score >= room_auto_approve_threshold:
        initial_status = CandidateStatus.APPROVED
    elif highlight_score >= room_review_threshold:
        initial_status = CandidateStatus.PENDING
    else:
        initial_status = CandidateStatus.REJECTED

    danmaku_explain = danmaku_score_explain(session_id, seg_start_ts, seg_end_ts)

    features_json = merge_prediction_metadata(
        json.dumps(
            {
                "features": features,
                "keyword_hits": kw_hits,
                "audio": _audio_meta(feats),
                "danmaku_explain": danmaku_explain,
            },
            ensure_ascii=False,
        ),
        ml_prediction,
    )

    dedup_hash_val = hashlib.sha1(
        f"{session_id}:{start_ts.timestamp():.1f}:{end_ts.timestamp():.1f}".encode()
    ).hexdigest()

    return HighlightDraft(
        segment_id=segment_id,
        session_id=session_id,
        room_id=room_id,
        decision=HighlightDecision.CANDIDATE,
        score=highlight_score,
        rule_score=rule_score,
        llm_score=llm_score or 0.0,
        highlight_score=highlight_score,
        start_ts=start_ts,
        end_ts=end_ts,
        peak_ts=peak_ts,
        reason=reason,
        dedup_hash=dedup_hash_val,
        features_json=features_json,
        initial_status=initial_status,
        config_hash=cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
        ml_prediction=ml_prediction,
    )


# 兼容导出: _ensure_event 供 task_worker 和测试使用


def _ensure_event(candidate_id: int) -> int | None:
    """确保每个 HighlightCandidate 有唯一 HighlightEvent (幂等)。

    此函数供 task_worker.py 和测试直接调用。
    新 Worker 路径已内联此逻辑到 commit_highlight 中。

    :param candidate_id: HighlightCandidate ID。
    :returns: event_id 或 None。
    """
    from sqlalchemy.exc import IntegrityError as _IE  # noqa: PLC0415

    with get_session() as db:
        existing = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
        if existing is not None:
            return existing.id
        cand = db.get(HighlightCandidate, candidate_id)
        if cand is None:
            return None
        event = HighlightEvent(
            candidate_id=candidate_id,
            session_id=cand.session_id,
            raw_start_ts=cand.start_ts,
            raw_end_ts=cand.end_ts,
            rule_score=cand.rule_score,
            llm_score=cand.llm_score,
            highlight_score=cand.highlight_score,
            features_json=cand.features_json,
            reason=cand.reason,
            review_status=ReviewStatus.PENDING,
            review_by="auto",
        )
        db.add(event)
        try:
            db.flush()
            db.refresh(event)
            _logger.info("auto event: eid=%s cid=%s", event.id, candidate_id)
            return event.id
        except _IE:
            db.rollback()
            _logger.info("idempotency_conflict_resolved: event cid=%s 已被并发创建", candidate_id)

    with get_session() as db:
        existing = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
        if existing is not None:
            return existing.id
        _logger.error("IntegrityError 后无法找到已有 Event: candidate_id=%s", candidate_id)
        return None
