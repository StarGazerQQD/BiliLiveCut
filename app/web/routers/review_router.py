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

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select as _sql_select

if TYPE_CHECKING:
    from sqlmodel import Session

    from app.db.models import HighlightCandidate, HighlightEvent, SegmentTask

review_router = APIRouter(prefix="/review", tags=["review"])

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


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
        return event
    event = HighlightEvent(
        candidate_id=candidate.id,
        session_id=candidate.session_id,
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

    actor, role = review_actor(request)
    safe_limit = max(1, min(limit, 500))
    with get_session() as db:
        candidates = db.exec(
            _sql_select(HighlightCandidate).order_by(HighlightCandidate.created_at.asc()).limit(500)
        ).all()
        ids = [candidate.id for candidate in candidates if candidate.id is not None]
        events = db.exec(_sql_select(HighlightEvent).where(HighlightEvent.candidate_id.in_(ids))).all() if ids else []
        event_by_candidate = {event.candidate_id: event for event in events}

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

    return _TEMPLATES.TemplateResponse(
        request,
        "review.html",
        {"candidate_id": candidate_id},
    )


@review_router.get("/api/{candidate_id}")
def get_review_data(request: Request, candidate_id: int) -> dict:
    """获取审片所需的完整数据:候选详情+转写+弹幕解释+评分曲线+前后上下文。"""
    from app.db.models import (
        Danmaku,
        FinalClip,
        HighlightCandidate,
        HighlightEvent,
        RawSegment,
        RecordingSession,
        Transcript,
    )
    from app.db.session import get_session

    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")

        # 转写文本。
        transcript_data = None
        # 候选关联的片段通过评分时的 session+时间范围查找。
        session = db.get(RecordingSession, c.session_id)
        segment = db.exec(
            _sql_select(RawSegment).where(
                RawSegment.session_id == c.session_id,
            )
        ).first()
        if segment:
            trans = db.exec(
                _sql_select(Transcript).where(
                    Transcript.segment_id == segment.id,
                )
            ).first()
            if trans:
                import json as _json

                words = []
                if trans.words_json:
                    try:
                        words = _json.loads(trans.words_json)
                    except _json.JSONDecodeError:
                        pass
                transcript_data = {
                    "text": trans.text,
                    "words": words,
                    "language": trans.language,
                }

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
                for (ts,) in danmaku_rows:
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
            _sql_select(FinalClip).where(
                FinalClip.candidate_id == candidate_id,
            )
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
        event = db.exec(_sql_select(HighlightEvent).where(HighlightEvent.candidate_id == candidate_id)).first()

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
        },
        "transcript": transcript_data,
        "danmaku_buckets": danmaku_buckets,
        "danmaku_window": danmaku_window,
        "features": {} if blinded else features,
        "score_breakdown": [] if blinded else score_breakdown,
        "danmaku_explain": {} if blinded else danmaku_explain,
        "prev_candidates": prev_candidates,
        "next_candidates": next_candidates,
        "existing_clips": existing_clips,
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
    return {
        "undone": snapshot.get("action"),
        "review_status": event.review_status,
        "adjusted_start_ts": event.adjusted_start_ts.isoformat() if event.adjusted_start_ts else None,
        "adjusted_end_ts": event.adjusted_end_ts.isoformat() if event.adjusted_end_ts else None,
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
    :param payload: 调整秒数和目标边界。
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

        push_history(event, c, _latest_task(db, candidate_id), action="adjust_boundary", actor=actor)
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
def submit_review(
    candidate_id: int,
    request: Request,
    payload: ReviewSubmitRequest,
) -> dict:
    """提交审核决断(细粒度), V0.1.12.8: 消除双写, 统一走 approve_event_and_task。

    正向决断时调用 approve_event_and_task 传入外层 db session,
    在同一事务中更新 Task.stage + Event.review_status + Candidate.status。
    非正向决断 (拒绝等) 仅更新 Event 和 Candidate。

    :param candidate_id: 候选 id。
    :param decision: 审核决断(approved_solo/rejected/insufficient_context 等)。
    :param reason: 审核原因/备注。
    :returns: 操作结果。
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from app.db.models import CandidateStatus, HighlightCandidate, ReviewStatus
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
    with get_session() as db:
        begin_review_write(db)
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")

        event = _ensure_event(db, c)
        require_edit_claim(event, actor, role)
        task = _latest_task(db, candidate_id)
        push_history(event, c, task, action="submit_review", actor=actor)

        if is_positive:
            # V0.1.12.8: 正向决断统一走 approve_event_and_task, 传入外层 db
            from app.pipeline.approval import approve_event_and_task

            if task is not None:
                approve_event_and_task(
                    task_id=task.id,
                    event_id=event.id,
                    approved_by=actor,
                    reason=reason,
                    source="human",
                    review_decision=decision,
                    db=db,
                )
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
        else:
            # 非正向决断: 仅更新 Event 和 Candidate
            event.review_status = decision
            event.review_reason = reason
            event.review_by = actor
            event.updated_at = _dt.now(UTC)
            db.add(event)
            if decision in (ReviewStatus.REJECTED, ReviewStatus.NOT_EXCITING):
                c.status = CandidateStatus.REJECTED
            db.add(c)

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

    return {"status": decision, "reason": reason}


@review_router.post("/api/{candidate_id}/rerender")
async def rerender_clip(candidate_id: int, request: Request) -> dict:
    """把调整边界后的切片提交到后台作业。

    :param candidate_id: 候选 id。
    :returns: 新的 clip 信息或状态。
    """
    from app.clipping.clipper import ClipOptions, validate_clip_boundary
    from app.db.models import HighlightCandidate
    from app.db.session import get_session
    from app.web.services.background_jobs import web_job_manager
    from app.web.services.review_workflow import (
        add_audit,
        refresh_claim,
        require_edit_claim,
        review_actor,
    )

    actor, role = review_actor(request)
    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        # 检查是否有调整后的边界。
        event = _ensure_event(db, c)
        require_edit_claim(event, actor, role)
        refresh_claim(event, actor)
        db.add(event)
        start_ts = event.adjusted_start_ts or c.start_ts
        end_ts = event.adjusted_end_ts or c.end_ts
        event_id = event.id
        try:
            validate_clip_boundary(
                c.session_id,
                start_ts,
                end_ts,
                max_duration_s=float(ClipOptions.from_settings().max_duration_s),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        label=f"候选 #{candidate_id} 审核版本渲染",
        owner=actor,
        dedup_key=f"review-rerender:{candidate_id}:{start_ts.isoformat()}:{end_ts.isoformat()}",
    )
    return {
        "status": "accepted",
        "job": job,
        "start_ts": job["payload"]["start_ts"],
        "end_ts": job["payload"]["end_ts"],
        "version": job["payload"]["version"],
    }


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

    from app.core.paths import clips_dir
    from app.db.models import FinalClip, HighlightCandidate
    from app.db.session import get_session

    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        # 找已有成品。
        clip = db.exec(
            _sql_select(FinalClip)
            .where(
                FinalClip.candidate_id == candidate_id,
            )
            .limit(1)
        ).first()
        if clip is None or not clip.file_path or not Path(clip.file_path).exists():
            return {
                "peaks": [],
                "duration_s": 0,
                "sample_rate": 0,
                "error": "尚未生成切片,请先「批准并出片」或「重新渲染」",
            }  # noqa: E501

        file_path = clip.file_path
        # 路径遍历保护:确保文件在 clips 目录内。
        resolved = Path(file_path).resolve()
        clips_root = Path(clips_dir()).resolve()
        if resolved.parent != clips_root and not any(p == clips_root for p in resolved.parents):
            return {"peaks": [], "duration_s": 0, "sample_rate": 0, "error": "文件路径不可访问"}
        file_path = str(resolved)
        duration_s = clip.duration_s or 0

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
    from app.db.models import RawSegment, Transcript

    segment = db.exec(
        _sql_select(RawSegment).where(
            RawSegment.session_id == candidate.session_id,
        )
    ).first()
    if segment is None:
        return None
    trans = db.exec(
        _sql_select(Transcript).where(
            Transcript.segment_id == segment.id,
        )
    ).first()
    return trans.text if trans else None


def _parse_saved_datetime(value: object) -> datetime | None:
    """解析审核历史中的 ISO 时间。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
