"""P1 横屏审片工作台路由。

独立于 Dashboard 的完整审片页面,包含:
- 16:9 横屏视频播放器
- 弹幕密度曲线(Canvas)
- 评分解释
- 可拖动的入点/出点
- 扩展按钮(+3/5/10/30s)
- 键盘快捷键(Space/JKL/I/O/←→)
- 细粒度审核决断
- 边界调整后重新渲染
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select as _sql_select

from app.analysis.transcript_windows import extract_transcript_window

if TYPE_CHECKING:
    from sqlmodel import Session

    from app.db.models import HighlightCandidate, HighlightEvent, RawSegment, SegmentTask

review_router = APIRouter(prefix="/review", tags=["review"])

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
_PREVIEW_LOCKS: dict[int, threading.Lock] = {}
_PREVIEW_LOCKS_GUARD = threading.Lock()


class BoundaryAdjustRequest(BaseModel):
    """人工调整剪辑边界的请求。"""

    model_config = ConfigDict(extra="forbid")

    adjust_s: float = Field(ge=-900.0, le=900.0, allow_inf_nan=False)
    side: Literal["start", "end", "both"]


class ReviewSubmitRequest(BaseModel):
    """人工审核决策请求。"""

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=2000)


class ClaimRequest(BaseModel):
    """领取审核项请求。"""

    model_config = ConfigDict(extra="forbid")

    force: bool = False


class ReviewDraftRequest(BaseModel):
    """审核草稿请求。"""

    model_config = ConfigDict(extra="forbid")

    decision: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=2000)


def _ensure_event(db: Session, candidate: HighlightCandidate) -> HighlightEvent:
    """读取或创建候选对应的唯一审核事件。"""
    from app.db.models import HighlightEvent

    event = db.exec(_sql_select(HighlightEvent).where(HighlightEvent.candidate_id == candidate.id)).first()
    if event is not None:
        if event.segment_id is None:
            task = _latest_task(db, int(candidate.id)) if candidate.id is not None else None
            if task is not None:
                event.segment_id = task.segment_id
                db.add(event)
        return event
    task = _latest_task(db, int(candidate.id)) if candidate.id is not None else None
    event = HighlightEvent(
        candidate_id=candidate.id,
        session_id=candidate.session_id,
        segment_id=task.segment_id if task else None,
        raw_start_ts=candidate.start_ts,
        raw_end_ts=candidate.end_ts,
        adjusted_start_ts=candidate.start_ts,
        adjusted_end_ts=candidate.end_ts,
        rule_score=candidate.rule_score,
        llm_score=candidate.llm_score,
        highlight_score=candidate.highlight_score,
        features_json=candidate.features_json,
        reason=candidate.reason,
        asr_text=_get_candidate_asr_text(db, candidate),
    )
    db.add(event)
    db.flush()
    return event


def _latest_task(db: Session, candidate_id: int) -> SegmentTask | None:
    """返回候选最近的流水线任务。"""
    from app.db.models import SegmentTask

    return db.exec(
        _sql_select(SegmentTask).where(SegmentTask.candidate_id == candidate_id).order_by(SegmentTask.created_at.desc())
    ).first()


def _as_utc_naive(value: datetime) -> datetime:
    """把数据库与请求中的时间统一为 UTC-naive 后再比较。"""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _candidate_segments(
    db: Session,
    candidate: HighlightCandidate,
    *,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
) -> list[RawSegment]:
    """返回与候选时间范围重叠的同会话录像片段。"""
    from app.db.models import RawSegment

    start = _as_utc_naive(start_ts or candidate.start_ts)
    end = _as_utc_naive(end_ts or candidate.end_ts)
    rows = db.exec(
        _sql_select(RawSegment).where(RawSegment.session_id == candidate.session_id).order_by(RawSegment.seq.asc())
    ).all()
    covering = [
        segment
        for segment in rows
        if segment.start_ts is not None
        and segment.end_ts is not None
        and _as_utc_naive(segment.end_ts) > start
        and _as_utc_naive(segment.start_ts) < end
    ]
    if covering:
        return covering

    task = _latest_task(db, int(candidate.id)) if candidate.id is not None else None
    segment = db.get(RawSegment, task.segment_id) if task is not None else None
    return [segment] if segment is not None and segment.session_id == candidate.session_id else []


def _candidate_transcript(
    db: Session,
    candidate: HighlightCandidate,
    segments: list[RawSegment],
    *,
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
) -> dict | None:
    """合并候选覆盖片段的转写，并换算为候选播放器相对时间。"""
    from app.db.models import Transcript

    segment_ids = [segment.id for segment in segments if segment.id is not None]
    if not segment_ids:
        return None
    transcripts = db.exec(_sql_select(Transcript).where(Transcript.segment_id.in_(segment_ids))).all()
    transcript_by_segment = {transcript.segment_id: transcript for transcript in transcripts}
    candidate_start = _as_utc_naive(start_ts or candidate.start_ts)
    candidate_duration = (_as_utc_naive(end_ts or candidate.end_ts) - candidate_start).total_seconds()
    texts: list[str] = []
    words: list[dict] = []
    language: str | None = None
    used_segment_ids: list[int] = []

    for segment in segments:
        if segment.id is None or segment.start_ts is None:
            continue
        transcript = transcript_by_segment.get(segment.id)
        if transcript is None:
            continue
        used_segment_ids.append(segment.id)
        language = language or transcript.language
        offset = (_as_utc_naive(segment.start_ts) - candidate_start).total_seconds()
        segment_duration = segment.duration_s
        if segment_duration is None and segment.end_ts is not None:
            segment_duration = (_as_utc_naive(segment.end_ts) - _as_utc_naive(segment.start_ts)).total_seconds()
        window = extract_transcript_window(
            transcript.text,
            transcript.words_json,
            start_s=max(0.0, -offset),
            end_s=max(0.0, candidate_duration - offset),
            duration_s=float(segment_duration or 0.0),
        )
        if window.text:
            texts.append(window.text)
        for raw_word in window.words:
            try:
                word_start = float(raw_word.get("start", 0.0)) + offset
                word_end = float(raw_word.get("end", word_start)) + offset
            except (TypeError, ValueError):
                continue
            if word_end < 0 or word_start > candidate_duration:
                continue
            word = dict(raw_word)
            word["start"] = round(max(0.0, word_start), 3)
            word["end"] = round(min(candidate_duration, max(0.0, word_end)), 3)
            words.append(word)

    if not texts and not words:
        return None
    return {
        "text": "\n".join(text for text in texts if text),
        "words": words,
        "language": language,
        "segment_ids": used_segment_ids,
    }


def _preview_lock(candidate_id: int) -> threading.Lock:
    """返回候选级预览渲染锁，防止播放器和波形请求重复渲染。"""
    with _PREVIEW_LOCKS_GUARD:
        return _PREVIEW_LOCKS.setdefault(candidate_id, threading.Lock())


def _review_preview_root() -> Path:
    """返回受控的审片预览缓存根目录。"""
    from app.core.paths import clips_dir

    return (Path(clips_dir()).resolve() / "review_previews").resolve()


def _review_preview_key(candidate_id: int, start_ts: datetime, end_ts: datetime) -> tuple[str, str]:
    """把候选标识和边界编码为仅含十六进制字符的不可逆缓存键。"""
    if candidate_id <= 0:
        raise ValueError("candidate_id 必须是正整数")
    candidate_key = hashlib.sha256(f"candidate:{candidate_id}".encode()).hexdigest()[:16]
    boundary_key = hashlib.sha256(f"{candidate_key}:{start_ts.isoformat()}:{end_ts.isoformat()}".encode()).hexdigest()[
        :16
    ]
    return candidate_key, boundary_key


def _review_preview_path(candidate_id: int, start_ts: datetime, end_ts: datetime) -> Path:
    """返回受控目录内、仅由十六进制缓存键组成的预览路径。"""
    candidate_key, boundary_key = _review_preview_key(candidate_id, start_ts, end_ts)
    preview_root = _review_preview_root()
    output_path = (preview_root / f"{candidate_key}_{boundary_key}.mp4").resolve()
    if output_path.parent != preview_root:
        raise RuntimeError("审片预览缓存路径越界")
    return output_path


def _ensure_review_preview(candidate_id: int, start_ts: datetime, end_ts: datetime) -> Path:
    """按需渲染候选预览，并以原子替换方式写入专用缓存目录。"""
    from loguru import logger

    from app.clipping.core import render_clip_to_file
    from app.clipping.models import ClipOptions
    from app.core.config import settings

    preview_root = _review_preview_root()
    preview_root.mkdir(parents=True, exist_ok=True)
    output_path = _review_preview_path(candidate_id, start_ts, end_ts)
    if output_path.is_file() and output_path.stat().st_size > 0:
        return output_path

    with _preview_lock(candidate_id):
        if output_path.is_file() and output_path.stat().st_size > 0:
            return output_path
        temp_path = preview_root / f"{uuid4().hex}.partial.mp4"
        try:
            render_clip_to_file(
                candidate_id,
                temp_path,
                ClipOptions(
                    loudnorm=False,
                    remove_silence=False,
                    vertical=False,
                    subtitle=False,
                    max_duration_s=settings.clip_max_duration_s,
                    crf=23,
                    preset="veryfast",
                ),
                start_ts=start_ts,
                end_ts=end_ts,
            )
            temp_path.replace(output_path)
        finally:
            temp_path.unlink(missing_ok=True)
            temp_path.with_suffix(".jpg").unlink(missing_ok=True)

        candidate_key, _ = _review_preview_key(candidate_id, start_ts, end_ts)
        for stale in preview_root.glob(f"{candidate_key}_*.mp4"):
            if stale != output_path:
                try:
                    stale.unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("旧审片预览正在使用，暂缓清理 path={} error={}", stale, exc)
        return output_path


@review_router.get("/queue", response_class=HTMLResponse)
async def review_queue_page(request: Request) -> HTMLResponse:
    """审核队列页面。"""
    return _TEMPLATES.TemplateResponse(request, "review_queue.html")


@review_router.get("/api/queue")
def get_review_queue(
    request: Request,
    status: Literal["pending", "claimed", "reviewed", "all"] = "pending",
    mine: bool = False,
    limit: int = 100,
) -> dict:
    """返回可领取、审核中或已完成的候选队列。"""
    from app.core.config import settings
    from app.db.models import HighlightCandidate, HighlightEvent, ReviewStatus
    from app.db.session import get_session
    from app.web.services.review_workflow import claim_state, review_actor
    from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

    actor, role = review_actor(request)
    safe_limit = max(1, min(limit, 500))
    with get_session() as db:
        candidates = db.exec(
            _sql_select(HighlightCandidate).order_by(HighlightCandidate.created_at.asc()).limit(500)
        ).all()
        ids = [candidate.id for candidate in candidates if candidate.id is not None]
        events = db.exec(_sql_select(HighlightEvent).where(HighlightEvent.candidate_id.in_(ids))).all() if ids else []
        event_by_candidate = {event.candidate_id: event for event in events}
        sources = source_identities_for_sessions(db, (candidate.session_id for candidate in candidates))

    items = []
    counts = {"pending": 0, "claimed": 0, "reviewed": 0}
    for candidate in candidates:
        event = event_by_candidate.get(candidate.id)
        claim = claim_state(event)
        reviewed = bool(event and event.review_status != ReviewStatus.PENDING)
        category = "reviewed" if reviewed else ("claimed" if claim["active"] else "pending")
        counts[category] += 1
        if status != "all" and category != status:
            continue
        if mine and claim["claimed_by"] != actor:
            continue
        blinded = bool(settings.review_blind_mode and role == "reviewer" and not reviewed)
        items.append(
            {
                "id": candidate.id,
                "session_id": candidate.session_id,
                "start_ts": candidate.start_ts.isoformat(),
                "end_ts": candidate.end_ts.isoformat(),
                "status": category,
                "review_status": event.review_status if event else ReviewStatus.PENDING,
                "score": None if blinded else candidate.highlight_score,
                "reason": None if blinded else candidate.reason,
                "claim": claim,
                "blinded": blinded,
                **sources.get(candidate.session_id, unknown_source_identity()),
            }
        )
    return {
        "items": items[:safe_limit],
        "counts": counts,
        "actor": actor,
        "role": role,
        "blind_mode": settings.review_blind_mode,
    }


@review_router.get("/api/audit")
def get_review_audit(request: Request, limit: int = 100) -> dict:
    """管理员查询人工审核审计日志。"""
    import json

    from app.db.models import SystemLog
    from app.db.session import get_session
    from app.web.services.review_workflow import review_actor

    _, role = review_actor(request)
    if role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可查看审核审计")
    with get_session() as db:
        rows = db.exec(
            _sql_select(SystemLog)
            .where(SystemLog.module == "review")
            .order_by(SystemLog.created_at.desc())
            .limit(max(1, min(limit, 500)))
        ).all()
    return {
        "items": [
            {
                "id": row.id,
                "event": row.event,
                "message": row.message,
                "context": json.loads(row.context_json) if row.context_json else {},
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@review_router.get("/{candidate_id}", response_class=HTMLResponse)
async def review_page(request: Request, candidate_id: int) -> HTMLResponse:
    """审片工作台主页面。"""
    from app.db.models import HighlightCandidate
    from app.db.session import get_session

    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
    if c is None:
        raise HTTPException(status_code=404, detail="候选不存在")

    return _TEMPLATES.TemplateResponse(request, "review.html", {"candidate_id": candidate_id})


@review_router.get("/api/{candidate_id}")
def get_review_data(request: Request, candidate_id: int) -> dict:
    """获取审片所需的完整数据:候选详情+转写+弹幕解释+评分曲线+前后上下文。"""
    from app.db.models import (
        Danmaku,
        FinalClip,
        HighlightCandidate,
        HighlightEvent,
        RecordingSession,
    )
    from app.db.session import get_session
    from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        source = source_identities_for_sessions(db, [c.session_id]).get(c.session_id, unknown_source_identity())

        event = db.exec(
            _sql_select(HighlightEvent).where(
                HighlightEvent.candidate_id == candidate_id,
            )
        ).first()

        # 候选可能跨越多个原始分段；转写必须按时间覆盖关系合并。
        transcript_start = event.adjusted_start_ts if event and event.adjusted_start_ts else c.start_ts
        transcript_end = event.adjusted_end_ts if event and event.adjusted_end_ts else c.end_ts
        transcript_data = _candidate_transcript(
            db,
            c,
            _candidate_segments(db, c, start_ts=transcript_start, end_ts=transcript_end),
            start_ts=transcript_start,
            end_ts=transcript_end,
        )
        session = db.get(RecordingSession, c.session_id)

        # 弹幕密度数据:按 5 秒分桶。
        start = c.start_ts
        end = c.end_ts
        margin = 30  # 前后各 30 秒的上下文。

        if start is None or end is None:
            danmaku_buckets = []
            danmaku_window = {"start": None, "end": None, "margin": margin}
        else:
            danmaku_window_start = start.replace(tzinfo=None) if hasattr(start, "replace") and start.tzinfo else start
            # Only import timedelta once.
            from datetime import timedelta

            ctx_start = danmaku_window_start - timedelta(seconds=margin)
            ctx_end = danmaku_window_start + (end - start) + timedelta(seconds=margin)
            danmaku_buckets: list[dict] = []
            if session:
                query_start = ctx_start.replace(tzinfo=None) if ctx_start.tzinfo else ctx_start
                query_end = ctx_end.replace(tzinfo=None) if ctx_end.tzinfo else ctx_end
                danmaku_rows = db.exec(
                    _sql_select(Danmaku.ts)
                    .where(
                        Danmaku.session_id == c.session_id,
                        Danmaku.ts >= query_start,
                        Danmaku.ts <= query_end,
                        Danmaku.msg_type == "danmaku",
                    )
                    .order_by(Danmaku.ts.asc())
                ).all()

                bucket_s = 5
                t0 = ctx_start
                total_s = (ctx_end - ctx_start).total_seconds()
                num_buckets = max(1, int(total_s / bucket_s))
                counts = [0] * num_buckets
                t0_ts = int(t0.timestamp()) if hasattr(t0, "timestamp") else 0
                for ts in danmaku_rows:
                    ts_ts = int(ts.timestamp()) if hasattr(ts, "timestamp") else 0
                    idx = (ts_ts - t0_ts) // bucket_s
                    if 0 <= idx < num_buckets:
                        counts[idx] += 1
                for i, cnt in enumerate(counts):
                    danmaku_buckets.append(
                        {
                            "t": round(t0_ts + i * bucket_s, 1),
                            "count": cnt,
                        }
                    )

            danmaku_window = {
                "start": ctx_start.isoformat() if hasattr(ctx_start, "isoformat") else str(ctx_start),
                "end": ctx_end.isoformat() if hasattr(ctx_end, "isoformat") else str(ctx_end),
                "margin": margin,
            }

        # 评分解释。
        from app.web.services.review_workflow import model_features

        features = model_features(c.features_json)
        danmaku_explain = {}
        # 尝试提取 danmaku 解释。
        if "danmaku_explain" in features:
            danmaku_explain = features.pop("danmaku_explain", {})

        # 前后候选上下文。
        prev_candidates = []
        next_candidates = []
        all_cands = db.exec(
            _sql_select(HighlightCandidate)
            .where(
                HighlightCandidate.session_id == c.session_id,
            )
            .order_by(HighlightCandidate.start_ts.asc())
        ).all()
        for i, cand in enumerate(all_cands):
            if cand.id == candidate_id:
                for pc in all_cands[max(0, i - 2) : i]:
                    prev_candidates.append({"id": pc.id, "score": pc.highlight_score, "reason": pc.reason})
                for nc in all_cands[i + 1 : i + 3]:
                    next_candidates.append({"id": nc.id, "score": nc.highlight_score, "reason": nc.reason})
                break

        # 已有的成品(若有)。
        clips = db.exec(
            _sql_select(FinalClip)
            .where(
                FinalClip.candidate_id == candidate_id,
            )
            .order_by(FinalClip.created_at.desc())
        ).all()
        existing_clips = [
            {
                "id": cl.id,
                "file_path": cl.file_path,
                "video_url": f"/api/clips/{cl.id}/video",
                "title": cl.title,
            }
            for cl in clips
        ]

    from app.core.config import settings
    from app.db.models import ReviewStatus
    from app.web.services.review_workflow import public_workflow, review_actor

    actor, role = review_actor(request)
    reviewed = bool(event and event.review_status != ReviewStatus.PENDING)
    blinded = bool(settings.review_blind_mode and role == "reviewer" and not reviewed)
    if blinded:
        for adjacent in (*prev_candidates, *next_candidates):
            adjacent["score"] = None
            adjacent["reason"] = None

    # 评分维度贡献。
    score_breakdown = []
    dim_labels = {
        "volume": "音量变化",
        "keywords": "关键词命中",
        "speech_rate": "语速变化",
        "laughter": "笑声检测",
        "danmaku": "弹幕突增",
        "danmaku_sentiment": "弹幕情绪",
        "trend": "网感关联",
        "audio_events": "音频事件",
    }
    # 读取权重计算贡献。
    from app.analysis.scoring_config import get_scoring_config

    try:
        cfg = get_scoring_config()
        weights = cfg.weights
    except Exception:
        weights = {}

    for dim, label in dim_labels.items():
        val = features.get(dim, 0.0) if isinstance(features, dict) else 0.0
        w = weights.get(dim, 0.0) if isinstance(weights, dict) else 0.0
        contrib = val * w if isinstance(val, (int, float)) else 0.0
        score_breakdown.append(
            {
                "dim": dim,
                "label": label,
                "value": val,
                "weight": w,
                "contribution": round(contrib, 4),
            }
        )

    return {
        "candidate": {
            "id": c.id,
            "session_id": c.session_id,
            "start_ts": c.start_ts.isoformat() if c.start_ts else None,
            "end_ts": c.end_ts.isoformat() if c.end_ts else None,
            "peak_ts": c.peak_ts.isoformat() if c.peak_ts else None,
            "rule_score": None if blinded else c.rule_score,
            "llm_score": None if blinded else c.llm_score,
            "highlight_score": None if blinded else c.highlight_score,
            "reason": None if blinded else c.reason,
            "status": c.status,
            **source,
        },
        "source": source,
        "transcript": transcript_data,
        "danmaku_buckets": danmaku_buckets,
        "danmaku_window": danmaku_window,
        "features": {} if blinded else features,
        "score_breakdown": [] if blinded else score_breakdown,
        "danmaku_explain": {} if blinded else danmaku_explain,
        "prev_candidates": prev_candidates,
        "next_candidates": next_candidates,
        "existing_clips": existing_clips,
        "preview_url": f"/review/api/{candidate_id}/preview",
        "media_url": existing_clips[0]["video_url"] if existing_clips else f"/review/api/{candidate_id}/preview",
        "boundary": {
            "event_id": event.id if event else None,
            "adjusted_start_ts": event.adjusted_start_ts.isoformat()
            if event and event.adjusted_start_ts
            else c.start_ts.isoformat(),
            "adjusted_end_ts": event.adjusted_end_ts.isoformat()
            if event and event.adjusted_end_ts
            else c.end_ts.isoformat(),
        },
        "workflow": public_workflow(event, actor, role),
        "viewer": {"actor": actor, "role": role, "blinded": blinded},
    }


@review_router.post("/api/{candidate_id}/claim")
def claim_review(candidate_id: int, request: Request, payload: ClaimRequest) -> dict:
    """领取候选，防止多位审核员同时修改。"""
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.services.review_workflow import (
        add_audit,
        begin_review_write,
        claim_event,
        review_actor,
    )

    actor, role = review_actor(request)
    with get_session() as db:
        begin_review_write(db)
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        event = _ensure_event(db, candidate)
        claim = claim_event(event, actor, role, force=payload.force)
        db.add(event)
        add_audit(db, actor=actor, action="claim", candidate_id=candidate_id, details={"force": payload.force})
    return {"claim": claim}


@review_router.post("/api/{candidate_id}/release")
def release_review(candidate_id: int, request: Request) -> dict:
    """释放候选领取。"""
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.services.review_workflow import (
        add_audit,
        begin_review_write,
        release_event,
        review_actor,
    )

    actor, role = review_actor(request)
    with get_session() as db:
        begin_review_write(db)
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        event = _ensure_event(db, candidate)
        release_event(event, actor, role)
        db.add(event)
        add_audit(db, actor=actor, action="release", candidate_id=candidate_id)
    return {"released": True}


@review_router.put("/api/{candidate_id}/draft")
def update_review_draft(candidate_id: int, request: Request, payload: ReviewDraftRequest) -> dict:
    """持久化当前审核草稿。"""
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.services.review_workflow import (
        add_audit,
        begin_review_write,
        refresh_claim,
        require_edit_claim,
        review_actor,
        save_draft,
    )

    actor, role = review_actor(request)
    with get_session() as db:
        begin_review_write(db)
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        event = _ensure_event(db, candidate)
        require_edit_claim(event, actor, role)
        draft = save_draft(event, actor, payload.model_dump())
        refresh_claim(event, actor)
        db.add(event)
        add_audit(db, actor=actor, action="save_draft", candidate_id=candidate_id)
    return {"draft": draft}


@review_router.post("/api/{candidate_id}/undo")
def undo_review_action(candidate_id: int, request: Request) -> dict:
    """撤销最近一次边界或审核决策修改。"""
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.services.review_workflow import (
        add_audit,
        begin_review_write,
        pop_history,
        refresh_claim,
        require_edit_claim,
        restore_related_state,
        review_actor,
    )

    actor, role = review_actor(request)
    with get_session() as db:
        begin_review_write(db)
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        event = _ensure_event(db, candidate)
        require_edit_claim(event, actor, role)
        snapshot = pop_history(event)
        event.adjusted_start_ts = _parse_saved_datetime(snapshot.get("adjusted_start_ts"))
        event.adjusted_end_ts = _parse_saved_datetime(snapshot.get("adjusted_end_ts"))
        event.review_status = str(snapshot["review_status"])
        event.review_reason = snapshot.get("review_reason")
        event.review_by = str(snapshot["review_by"])
        candidate.status = str(snapshot["candidate_status"])
        restored_task = restore_related_state(db, candidate_id, snapshot)
        if not restored_task:
            task = _latest_task(db, candidate_id)
            if task is not None and snapshot.get("task_stage"):
                task.stage = str(snapshot["task_stage"])
                db.add(task)
        refresh_claim(event, actor)
        db.add(event)
        db.add(candidate)
        add_audit(
            db,
            actor=actor,
            action="undo",
            candidate_id=candidate_id,
            details={"undone_action": snapshot.get("action")},
        )
        restored_review_status = event.review_status
        adjusted_start_ts = event.adjusted_start_ts
        adjusted_end_ts = event.adjusted_end_ts
    from app.pipeline.highlight_feedback import record_candidate_review_feedback

    record_candidate_review_feedback(
        candidate_id,
        decision=restored_review_status,
        reviewed_by=actor,
    )
    return {
        "undone": snapshot.get("action"),
        "review_status": restored_review_status,
        "adjusted_start_ts": adjusted_start_ts.isoformat() if adjusted_start_ts else None,
        "adjusted_end_ts": adjusted_end_ts.isoformat() if adjusted_end_ts else None,
    }


# ---- 边界调整与重新渲染 ---- #


@review_router.post("/api/{candidate_id}/adjust")
async def adjust_boundary(
    candidate_id: int,
    request: Request,
    payload: BoundaryAdjustRequest,
) -> dict:
    """调整入点/出点偏移量,保存调整后的边界到 HighlightEvent。

    :param candidate_id: 候选 id。
    :param payload: 调整量与目标边界。
    :returns: 新的边界。
    """
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

    from app.clipping.clipper import ClipOptions, validate_clip_boundary
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.services.review_workflow import (
        add_audit,
        begin_review_write,
        push_history,
        refresh_claim,
        require_edit_claim,
        review_actor,
    )

    actor, role = review_actor(request)
    with get_session() as db:
        begin_review_write(db)
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")

        event = _ensure_event(db, c)
        require_edit_claim(event, actor, role)

        # 应用调整。
        delta = timedelta(seconds=payload.adjust_s)
        current_start = event.adjusted_start_ts or c.start_ts
        current_end = event.adjusted_end_ts or c.end_ts
        proposed_start = current_start + delta if payload.side in ("start", "both") else current_start
        proposed_end = current_end + delta if payload.side in ("end", "both") else current_end

        try:
            validate_clip_boundary(
                c.session_id,
                proposed_start,
                proposed_end,
                max_duration_s=float(ClipOptions.from_settings().max_duration_s),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        push_history(db, event, c, action="adjust_boundary", actor=actor)
        event.adjusted_start_ts = proposed_start
        event.adjusted_end_ts = proposed_end
        event.updated_at = _dt.now(UTC)
        event.review_by = actor
        refresh_claim(event, actor)
        db.add(event)
        add_audit(
            db,
            actor=actor,
            action="adjust_boundary",
            candidate_id=candidate_id,
            details={"side": payload.side, "adjust_s": payload.adjust_s},
        )

        return {
            "event_id": event.id,
            "adjusted_start_ts": event.adjusted_start_ts.isoformat() if event.adjusted_start_ts else None,
            "adjusted_end_ts": event.adjusted_end_ts.isoformat() if event.adjusted_end_ts else None,
            "duration_s": (event.adjusted_end_ts - event.adjusted_start_ts).total_seconds(),
        }


@review_router.post("/api/{candidate_id}/review")
async def submit_review(
    candidate_id: int,
    request: Request,
    payload: ReviewSubmitRequest,
) -> dict:
    """提交审核决断(细粒度), V0.1.12.8: 消除双写, 统一走 approve_event_and_task。

    正向决断时调用 approve_event_and_task 传入外层 db session,
    在同一事务中更新 Task.stage + Event.review_status + Candidate.status。
    明确拒绝时同步更新 Event、Candidate、关联 Task 和未发布 FinalClip。

    :param candidate_id: 候选 id。
    :param decision: 审核决断(approved_solo/rejected/insufficient_context 等)。
    :param reason: 审核原因/备注。
    :returns: 操作结果。
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from app.db.models import CandidateStatus, HighlightCandidate, ReviewStatus, TaskStatus
    from app.db.session import get_session
    from app.web.services.review_workflow import (
        add_audit,
        begin_review_write,
        clear_draft,
        push_history,
        release_event,
        require_edit_claim,
        review_actor,
    )

    decision = payload.decision
    reason = payload.reason
    valid = {
        ReviewStatus.APPROVED_SOLO,
        ReviewStatus.APPROVED_COLLECTION,
        ReviewStatus.IN_COLLECTION,
        ReviewStatus.MAYBE_TOPIC,
        ReviewStatus.HOLD,
        ReviewStatus.NOT_EXCITING,
        ReviewStatus.INSUFFICIENT_CONTEXT,
        ReviewStatus.START_TOO_LATE,
        ReviewStatus.END_TOO_EARLY,
        ReviewStatus.DUPLICATE_CONTENT,
        ReviewStatus.SUBTITLE_ERROR,
        ReviewStatus.VISUAL_ISSUE,
        ReviewStatus.SENSITIVE,
        ReviewStatus.REJECTED,
    }
    if decision not in valid:
        raise HTTPException(status_code=400, detail=f"无效的审核决断: {decision}")

    is_positive = decision in ReviewStatus.POSITIVE

    actor, role = review_actor(request)
    enqueue_unlinked_render = False
    with get_session() as db:
        begin_review_write(db)
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")

        event = _ensure_event(db, c)
        require_edit_claim(event, actor, role)
        task = _latest_task(db, candidate_id)
        push_history(
            db,
            event,
            c,
            action="submit_review",
            actor=actor,
            include_related_state=True,
        )

        if is_positive:
            # V0.1.12.8: 正向决断统一走 approve_event_and_task, 传入外层 db
            from app.pipeline.approval import approve_event_and_task

            if task is not None:
                approved = approve_event_and_task(
                    task_id=task.id,
                    event_id=event.id,
                    approved_by=actor,
                    reason=reason,
                    source="human",
                    review_decision=decision,
                    db=db,
                )
                if approved and decision == ReviewStatus.APPROVED_SOLO:
                    from app.pipeline.stage_result import enqueue_next

                    if task.stage == TaskStatus.APPROVED:
                        enqueue_next(task, TaskStatus.QUEUED_FOR_RENDER)
                        db.add(task)
                    elif task.stage == TaskStatus.APPROVED_WAITING_RENDER:
                        enqueue_next(task, TaskStatus.QUEUED_FOR_RENDER)
                        db.add(task)
                event.review_status = decision
                event.review_reason = reason
                event.review_by = actor
                event.updated_at = _dt.now(UTC)
                db.add(event)
            else:
                # 无关联 task 时仅更新 Event + Candidate
                event.review_status = decision
                event.review_reason = reason
                event.review_by = actor
                event.updated_at = _dt.now(UTC)
                db.add(event)
                c.status = CandidateStatus.APPROVED
                db.add(c)
                enqueue_unlinked_render = decision == ReviewStatus.APPROVED_SOLO
        elif decision in (ReviewStatus.REJECTED, ReviewStatus.NOT_EXCITING):
            from app.pipeline.rejection import reject_candidate_and_outputs

            reject_candidate_and_outputs(
                db,
                candidate_id,
                rejected_by=actor,
                reason=reason,
                review_decision=decision,
            )
        else:
            # 保留待定、边界或质量问题已被人工处理，不再伪装成“等待审核”。
            event.review_status = decision
            event.review_reason = reason
            event.review_by = actor
            event.updated_at = _dt.now(UTC)
            db.add(event)
            db.add(c)
            if task is not None and task.stage in (
                TaskStatus.AWAITING_REVIEW,
                TaskStatus.CANDIDATE_CREATED,
            ):
                from app.pipeline.stage_result import enqueue_next

                enqueue_next(task, TaskStatus.REVIEWED_WAITING_ACTION)
                db.add(task)

        clear_draft(event)
        release_event(event, actor, role)
        db.add(event)
        add_audit(
            db,
            actor=actor,
            action="submit_review",
            candidate_id=candidate_id,
            details={"decision": decision, "reason": reason},
        )
        candidate_session_id = c.session_id

    from app.pipeline.highlight_feedback import record_candidate_review_feedback

    record_candidate_review_feedback(
        candidate_id,
        decision=decision,
        reviewed_by=actor,
    )
    from app.analysis.threshold_learning import sync_candidate_feedback

    try:
        threshold_feedback = sync_candidate_feedback(candidate_id, decision=decision)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        # 审核事务已经成功提交；学习链异常只能降级，不能把已生效的审核误报为失败。
        logger.opt(exception=exc).error(
            "审核反馈同步失败 candidate={} session={} decision={} actor={}",
            candidate_id,
            candidate_session_id,
            decision,
            actor,
        )
        threshold_feedback = {"room_id": None, "action": None, "threshold": None}
    logger.info(
        "审核提交 candidate={} session={} room={} decision={} actor={} threshold_action={} threshold_changed={}",
        candidate_id,
        candidate_session_id,
        threshold_feedback.get("room_id"),
        decision,
        actor,
        threshold_feedback.get("action"),
        threshold_feedback.get("threshold"),
    )
    job = None
    if enqueue_unlinked_render:
        from app.web.services.background_jobs import web_job_manager

        job = await web_job_manager.enqueue(
            "candidate_render",
            {"candidate_id": candidate_id, "reviewed_by": actor},
            label=f"候选 #{candidate_id} 审核通过出片",
            owner=actor,
            dedup_key=f"candidate-render:{candidate_id}",
        )
    return {"status": decision, "reason": reason, "job": job}


@review_router.post("/api/{candidate_id}/rerender")
async def rerender_clip(candidate_id: int, request: Request) -> dict:
    """把调整边界后的切片提交到后台作业。

    :param candidate_id: 候选 id。
    :returns: 新的 clip 信息或状态。
    """
    from app.clipping.clipper import ClipOptions, validate_clip_boundary
    from app.db.models import HighlightCandidate, HighlightEvent
    from app.db.session import get_session
    from app.web.services.background_jobs import web_job_manager
    from app.web.services.review_workflow import (
        add_audit,
        begin_review_write,
        refresh_claim,
        require_edit_claim,
        review_actor,
    )
    from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

    actor, role = review_actor(request)
    with get_session() as db:
        begin_review_write(db)
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        event = db.exec(
            _sql_select(HighlightEvent).where(
                HighlightEvent.candidate_id == candidate_id,
            )
        ).first()
        if event is None:
            event = _ensure_event(db, c)
        require_edit_claim(event, actor, role)
        refresh_claim(event, actor)
        start_ts = event.adjusted_start_ts if event and event.adjusted_start_ts else c.start_ts
        end_ts = event.adjusted_end_ts if event and event.adjusted_end_ts else c.end_ts
        event_id = event.id if event else None
        source_label = source_identities_for_sessions(db, [c.session_id]).get(c.session_id, unknown_source_identity())[
            "source_label"
        ]

        try:
            validate_clip_boundary(
                c.session_id,
                start_ts,
                end_ts,
                max_duration_s=float(ClipOptions.from_settings().max_duration_s),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        db.add(event)
        add_audit(db, actor=actor, action="rerender", candidate_id=candidate_id)

    version = f"review-{event_id or 'base'}-{uuid4().hex[:8]}"
    job = await web_job_manager.enqueue(
        "review_rerender",
        {
            "candidate_id": candidate_id,
            "start_ts": start_ts.isoformat(),
            "end_ts": end_ts.isoformat(),
            "version": version,
        },
        label=f"{source_label} · 候选 #{candidate_id} 审核版本渲染",
        owner=actor,
        dedup_key=f"review-rerender:{candidate_id}:{start_ts.isoformat()}:{end_ts.isoformat()}",
    )
    return {
        "status": "accepted",
        "job": job,
        "version": job["payload"]["version"],
        "start_ts": job["payload"]["start_ts"],
        "end_ts": job["payload"]["end_ts"],
    }


@review_router.get("/api/{candidate_id}/preview")
def get_review_preview(candidate_id: int) -> FileResponse:
    """返回候选的按需渲染预览，不要求候选已批准或已出片。"""
    from app.db.models import HighlightCandidate, HighlightEvent
    from app.db.session import get_session

    with get_session() as db:
        candidate = db.get(HighlightCandidate, candidate_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        event = db.exec(_sql_select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
        start_ts = event.adjusted_start_ts if event and event.adjusted_start_ts else candidate.start_ts
        end_ts = event.adjusted_end_ts if event and event.adjusted_end_ts else candidate.end_ts

    from loguru import logger

    try:
        preview_path = _ensure_review_preview(candidate_id, start_ts, end_ts)
    except ValueError as exc:
        logger.warning("候选预览边界无效 candidate_id={} error={}", candidate_id, exc)
        raise HTTPException(status_code=422, detail="候选预览边界无效") from exc
    except (OSError, RuntimeError) as exc:
        logger.exception("候选预览渲染失败 candidate_id={}", candidate_id)
        raise HTTPException(status_code=500, detail="候选预览渲染失败") from exc
    return FileResponse(preview_path, media_type="video/mp4")


@review_router.get("/api/{candidate_id}/waveform")
def get_waveform(candidate_id: int, resolution: int = 400) -> dict:
    """生成音频波形采样数据(FFmpeg→PCM→RMS峰值数组)。

    :param candidate_id: 候选 id。
    :param resolution: 采样点数(默认 400,前端 Canvas 宽度)。
    :returns: ``{peaks, duration_s, sample_rate}``。
    """
    import json as _json
    import struct as _struct
    import subprocess as _sp
    import tempfile as _tf

    from loguru import logger

    from app.db.models import HighlightCandidate, HighlightEvent
    from app.db.session import get_session

    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        event = db.exec(_sql_select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()
        start_ts = event.adjusted_start_ts if event and event.adjusted_start_ts else c.start_ts
        end_ts = event.adjusted_end_ts if event and event.adjusted_end_ts else c.end_ts

    try:
        file_path = str(_ensure_review_preview(candidate_id, start_ts, end_ts))
        duration_s = (end_ts - start_ts).total_seconds()
    except (OSError, RuntimeError, ValueError):
        logger.exception("候选波形预览渲染失败 candidate_id={}", candidate_id)
        return {
            "peaks": [],
            "duration_s": 0,
            "sample_rate": 0,
            "error": "候选预览渲染失败",
        }

    if duration_s <= 0:
        # 用 ffprobe 获取时长。
        try:
            result = _sp.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", file_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            info = _json.loads(result.stdout)
            duration_s = float(info.get("format", {}).get("duration", 0))
        except Exception:
            duration_s = 30  # fallback

    if duration_s <= 0:
        return {"peaks": [0.0] * resolution, "duration_s": 0, "sample_rate": 0}

    # FFmpeg 提取单声道 16-bit PCM,并降采样到约 resolution*2 个样本。
    sample_rate = 8000  # 低频足以表示波形包络
    _total_samples = resolution * 2  # 每点 2 个样本取 max
    with _tf.NamedTemporaryFile(suffix=".pcm", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        _sp.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "quiet",
                "-i",
                file_path,
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                tmp_path,
            ],
            check=True,
            timeout=30,
        )
        with open(tmp_path, "rb") as f:
            raw = f.read()
    except Exception as exc:
        return {
            "peaks": [],
            "duration_s": duration_s,
            "sample_rate": sample_rate,
            "error": f"FFmpeg 波形生成失败: {exc}",
        }  # noqa: E501
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    # 解析 16-bit signed PCM。
    sample_count = len(raw) // 2
    if sample_count < resolution:
        return {"peaks": [0.0] * resolution, "duration_s": duration_s, "sample_rate": sample_rate}

    samples_per_bucket = max(1, sample_count // resolution)
    peaks = []
    for i in range(resolution):
        start = i * samples_per_bucket
        end = min(sample_count, start + samples_per_bucket * 2)
        chunk = raw[start * 2 : end * 2]
        max_val = 0
        for j in range(0, len(chunk), 2):
            val = abs(_struct.unpack_from("<h", chunk, j)[0])
            if val > max_val:
                max_val = val
        peaks.append(round(max_val / 32768.0, 4))

    return {"peaks": peaks, "duration_s": round(duration_s, 2), "sample_rate": sample_rate}


def _get_candidate_asr_text(db, candidate) -> str | None:
    """根据候选关联的片段获取转写文本。

    :param db: 数据库会话。
    :param candidate: HighlightCandidate 实例。
    :returns: ASR 文本或 ``None``。
    """
    transcript = _candidate_transcript(db, candidate, _candidate_segments(db, candidate))
    return str(transcript["text"]) if transcript and transcript.get("text") else None


def _parse_saved_datetime(value: object) -> datetime | None:
    """解析撤销历史中的 ISO 时间并统一为 UTC。"""
    from datetime import UTC

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
