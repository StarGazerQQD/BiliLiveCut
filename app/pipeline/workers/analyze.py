"""分析阶段 Worker — compute/commit 真正分离。

analyze_compute 只做评分计算, 不创建 Candidate/Event, 不写 DB。
commit_highlight 在租约保护下创建 Candidate + Event, 并执行所有 DB 写操作。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlmodel import select

from app.analysis import audio as audio_mod
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
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_heartbeat

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
    session_id: int
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

    if draft is None:
        return {"decision": HighlightDecision.BELOW_THRESHOLD, "segment_id": segment_id}

    # draft 包含 decision 字段 (CANDIDATE / DUPLICATE)
    return draft


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

            # ── CANDIDATE: 从 draft 数据创建 Candidate ───
            dedup_hash = compute_result.get("dedup_hash") or hashlib.sha1(
                f"{compute_result.get('session_id', '')}:"
                f"{round(compute_result.get('start_ts', ''))}:"
                f"{round(compute_result.get('end_ts', ''))}".encode()
            ).hexdigest()

            candidate = HighlightCandidate(
                session_id=compute_result["session_id"],
                peak_ts=compute_result["peak_ts"],
                start_ts=compute_result["start_ts"],
                end_ts=compute_result["end_ts"],
                rule_score=compute_result["rule_score"],
                llm_score=compute_result.get("llm_score", 0.0),
                highlight_score=compute_result["highlight_score"],
                features_json=compute_result.get("features_json", "{}"),
                reason=compute_result.get("reason", ""),
                status=compute_result.get("initial_status", CandidateStatus.PENDING),
                dedup_hash=dedup_hash,
            )
            db.add(candidate)
            db.flush()
            db.refresh(candidate)

            cid = candidate.id
            _logger.info(
                "candidate_created: cid=%s segment=%s score=%.3f",
                cid,
                segment_id,
                compute_result["highlight_score"],
            )

            # 幂等创建 Event
            from app.db.models import HighlightEvent as HE  # noqa: PLC0415

            existing = db.exec(select(HE).where(HE.candidate_id == cid)).first()
            event_id = existing.id if existing is not None else None
            if event_id is None:
                event = HighlightEvent(
                    candidate_id=cid,
                    session_id=compute_result["session_id"],
                    raw_start_ts=compute_result["start_ts"],
                    raw_end_ts=compute_result["end_ts"],
                    rule_score=compute_result["rule_score"],
                    llm_score=compute_result.get("llm_score", 0.0),
                    highlight_score=compute_result["highlight_score"],
                    features_json=compute_result.get("features_json", "{}"),
                    reason=compute_result.get("reason", ""),
                    review_status=ReviewStatus.PENDING,
                    review_by="auto",
                )
                db.add(event)
                db.flush()
                db.refresh(event)
                event_id = event.id
                _logger.info("auto_event: eid=%s cid=%s", event_id, cid)

            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.CANDIDATE_CREATED, candidate_id=cid, event_id=event_id)
            db.add(task)
            db.commit()

    except LeaseLostError:
        _logger.warning("stale_result_discarded: highlight task=%s 已失去租约", lease.task_id)


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

    :param lease: 任务租约。
    """
    t0 = time.time()
    with get_session() as db:
        task = db.get(SegmentTask, lease.task_id)
        if task is None:
            return
        mark_heartbeat(task)
        db.add(task)
        db.commit()
    compute_result = analyze_compute(lease.task_id)
    ms_val = int((time.time() - t0) * 1000)
    commit_highlight(lease, compute_result, ms_val)


# ══════════════════════════════════════════
# 纯计算辅助函数: 不写任何业务对象
# ══════════════════════════════════════════


def _score_segment_draft(segment_id: int) -> dict[str, Any] | None:
    """纯评分计算, 不写 DB, 不创建 Candidate。

    返回 dict 包含 decision 字段:
    - CANDIDATE: 通过评分, 应创建候选
    - DUPLICATE: 去重命中
    返回 None: 初筛未过或终分不足 (由调用方转为 BELOW_THRESHOLD)

    :param segment_id: RawSegment ID。
    :returns: draft dict 含 decision 字段, 或 None (分数不足)。
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
            return None
        duration = segment.duration_s or float(settings.segment_duration_s)
        session_id = segment.session_id
        threshold = room.highlight_threshold if room else settings.highlight_threshold
        has_transcript = transcript is not None
        text = transcript.text if transcript else ""
        words_json = transcript.words_json if transcript else None
        file_path = segment.file_path
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

    _logger.info(
        "score_draft segment=%s rule=%.3f features=%s kw_hits=%s trend_hits=%s",
        segment_id,
        rule_score,
        {k: round(v, 3) for k, v in features.items()},
        kw_hits,
        trend_hits,
    )

    # 2) 初筛 — 不写 DB, 直接返回 None
    if rule_score < settings.highlight_init_threshold:
        return None

    # 3) LLM 复核
    judgement = llm_mod.judge_highlight(text, features)
    llm_score = judgement.score if judgement else None
    reason = judgement.reason if judgement else "规则命中(未启用/未触发 LLM)"
    highlight_score = fuse_scores(rule_score, llm_score, cfg.alpha, cfg.beta)

    # 终分不足 — 不写 DB, 直接返回 None
    if highlight_score < threshold:
        return None

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
        return {
            "decision": HighlightDecision.DUPLICATE,
            "segment_id": segment_id,
            "session_id": session_id,
            "dedup_hash": dedup_hash_val,
            "score": highlight_score,
            "rule_score": rule_score,
            "llm_score": llm_score or 0.0,
            "highlight_score": highlight_score,
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
            "peak_ts": peak_ts.isoformat(),
            "reason": "去重: IoU over threshold",
            "features_json": "{}",
            "initial_status": CandidateStatus.REJECTED,
            "config_hash": cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
        }

    # 6) 审核状态
    if room_auto_approve and highlight_score >= room_auto_approve_threshold:
        initial_status = CandidateStatus.APPROVED
    elif highlight_score >= room_review_threshold:
        initial_status = CandidateStatus.PENDING
    else:
        initial_status = CandidateStatus.REJECTED

    danmaku_explain = danmaku_score_explain(session_id, seg_start_ts, seg_end_ts)

    features_json = json.dumps(
        {
            "features": features,
            "keyword_hits": kw_hits,
            "audio": _audio_meta(feats),
            "danmaku_explain": danmaku_explain,
        },
        ensure_ascii=False,
    )

    dedup_hash_val = hashlib.sha1(
        f"{session_id}:{start_ts.timestamp():.1f}:{end_ts.timestamp():.1f}".encode()
    ).hexdigest()

    return {
        "decision": HighlightDecision.CANDIDATE,
        "segment_id": segment_id,
        "session_id": session_id,
        "peak_ts": peak_ts,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "rule_score": rule_score,
        "llm_score": llm_score or 0.0,
        "highlight_score": highlight_score,
        "features_json": features_json,
        "reason": reason,
        "initial_status": initial_status,
        "dedup_hash": dedup_hash_val,
        "score": highlight_score,
        "config_hash": cfg.model_dump_json() if hasattr(cfg, "model_dump_json") else "",
    }


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
