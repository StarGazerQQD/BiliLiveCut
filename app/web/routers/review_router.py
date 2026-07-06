"""P1 横屏审片工作台路由(V0.1.6)。

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

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select as _sql_select

review_router = APIRouter(prefix="/review", tags=["review"])

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


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
        "review.html",
        {
            "request": request,
            "candidate_id": candidate_id,
        },
    )


@review_router.get("/api/{candidate_id}")
def get_review_data(candidate_id: int) -> dict:
    """获取审片所需的完整数据:候选详情+转写+弹幕解释+评分曲线+前后上下文。"""
    from app.db.models import (
        Danmaku,
        FinalClip,
        HighlightCandidate,
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
                danmaku_rows = db.exec(
                    _sql_select(Danmaku.ts)
                    .where(
                        Danmaku.session_id == c.session_id,
                        Danmaku.ts >= ctx_start.replace(tzinfo=None)
                        if hasattr(ctx_start, "tzinfo") and ctx_start.tzinfo
                        else ctx_start,  # noqa: E501
                        Danmaku.ts <= ctx_end.replace(tzinfo=None)
                        if hasattr(ctx_end, "tzinfo") and ctx_end.tzinfo
                        else ctx_end,  # noqa: E501
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
        import json as _json2

        features = {}
        danmaku_explain = {}
        if c.features_json:
            try:
                features = _json2.loads(c.features_json)
            except _json2.JSONDecodeError:
                pass
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
        existing_clips = [{"id": cl.id, "file_path": cl.file_path, "title": cl.title} for cl in clips]

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
            "rule_score": c.rule_score,
            "llm_score": c.llm_score,
            "highlight_score": c.highlight_score,
            "reason": c.reason,
            "status": c.status,
        },
        "transcript": transcript_data,
        "danmaku_buckets": danmaku_buckets,
        "danmaku_window": danmaku_window,
        "features": features,
        "score_breakdown": score_breakdown,
        "danmaku_explain": danmaku_explain,
        "prev_candidates": prev_candidates,
        "next_candidates": next_candidates,
        "existing_clips": existing_clips,
    }


# ---- 边界调整与重新渲染 ---- #


@review_router.post("/api/{candidate_id}/adjust")
async def adjust_boundary(
    candidate_id: int,
    adjust_s: float = 0.0,
    side: str = "none",  # "start" / "end" / "both"
) -> dict:
    """调整入点/出点偏移量,保存调整后的边界到 HighlightEvent。

    :param candidate_id: 候选 id。
    :param adjust_s: 调整量(秒),正=向右/扩展,负=向左/收缩。
    :param side: "start"(调起点)/"end"(调终点)/"both"(同时调)。
    :returns: 新的边界。
    """
    from datetime import UTC, timedelta
    from datetime import datetime as _dt

    from app.db.models import HighlightCandidate, HighlightEvent
    from app.db.session import get_session

    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")

        # 查找或创建 HighlightEvent。
        event = db.exec(
            _sql_select(HighlightEvent).where(
                HighlightEvent.candidate_id == candidate_id,
            )
        ).first()
        if event is None:
            event = HighlightEvent(
                candidate_id=candidate_id,
                session_id=c.session_id,
                raw_start_ts=c.start_ts,
                raw_end_ts=c.end_ts,
                adjusted_start_ts=c.start_ts,
                adjusted_end_ts=c.end_ts,
                rule_score=c.rule_score,
                llm_score=c.llm_score,
                highlight_score=c.highlight_score,
                features_json=c.features_json,
                reason=c.reason,
                asr_text=_get_candidate_asr_text(db, c),
            )
            db.add(event)
            db.flush()

        # 应用调整。
        delta = timedelta(seconds=adjust_s)
        if side in ("start", "both"):
            event.adjusted_start_ts = (event.adjusted_start_ts or c.start_ts or _dt.now(UTC)) + delta
        if side in ("end", "both"):
            event.adjusted_end_ts = (event.adjusted_end_ts or c.end_ts or _dt.now(UTC)) + delta
        event.updated_at = _dt.now(UTC)
        event.review_by = "manual"
        db.add(event)

        return {
            "event_id": event.id,
            "adjusted_start_ts": event.adjusted_start_ts.isoformat() if event.adjusted_start_ts else None,
            "adjusted_end_ts": event.adjusted_end_ts.isoformat() if event.adjusted_end_ts else None,
        }


@review_router.post("/api/{candidate_id}/review")
def submit_review(
    candidate_id: int,
    decision: str,
    reason: str | None = None,
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

    from app.db.models import (
        CandidateStatus,
        HighlightCandidate,
        HighlightEvent,
        ReviewStatus,
        SegmentTask,
    )
    from app.db.session import get_session

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

    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")

        # 查找或创建 HighlightEvent
        event = db.exec(
            _sql_select(HighlightEvent).where(
                HighlightEvent.candidate_id == candidate_id,
            )
        ).first()
        if event is None:
            event = HighlightEvent(
                candidate_id=candidate_id,
                session_id=c.session_id,
                raw_start_ts=c.start_ts,
                raw_end_ts=c.end_ts,
                adjusted_start_ts=c.start_ts,
                adjusted_end_ts=c.end_ts,
                rule_score=c.rule_score,
                llm_score=c.llm_score,
                highlight_score=c.highlight_score,
                features_json=c.features_json,
                reason=c.reason,
                asr_text=_get_candidate_asr_text(db, c),
            )
            db.add(event)
            db.flush()

        if is_positive:
            # V0.1.12.8: 正向决断统一走 approve_event_and_task, 传入外层 db
            from app.pipeline.approval import approve_event_and_task

            task = db.exec(
                _sql_select(SegmentTask)
                .where(
                    SegmentTask.candidate_id == candidate_id,
                )
                .order_by(SegmentTask.created_at.desc())
            ).first()
            if task is not None:
                approve_event_and_task(
                    task_id=task.id,
                    event_id=event.id,
                    approved_by="manual",
                    reason=reason,
                    source="human",
                    review_decision=decision,
                    db=db,
                )
            else:
                # 无关联 task 时仅更新 Event + Candidate
                event.review_status = decision
                event.review_reason = reason
                event.review_by = "manual"
                event.updated_at = _dt.now(UTC)
                db.add(event)
                c.status = CandidateStatus.APPROVED
                db.add(c)
        else:
            # 非正向决断: 仅更新 Event 和 Candidate
            event.review_status = decision
            event.review_reason = reason
            event.review_by = "manual"
            event.updated_at = _dt.now(UTC)
            db.add(event)
            if decision in (ReviewStatus.REJECTED, ReviewStatus.NOT_EXCITING):
                c.status = CandidateStatus.REJECTED
            db.add(c)

    return {"status": decision, "reason": reason}


@review_router.post("/api/{candidate_id}/rerender")
async def rerender_clip(candidate_id: int) -> dict:
    """使用调整后的边界重新渲染切片(异步)。

    :param candidate_id: 候选 id。
    :returns: 新的 clip 信息或状态。
    """
    import asyncio

    from app.db.models import HighlightCandidate, HighlightEvent
    from app.db.session import get_session
    from app.pipeline.orchestrator import produce_clip

    with get_session() as db:
        c = db.get(HighlightCandidate, candidate_id)
        if c is None:
            raise HTTPException(status_code=404, detail="候选不存在")
        # 检查是否有调整后的边界。
        event = db.exec(
            _sql_select(HighlightEvent).where(
                HighlightEvent.candidate_id == candidate_id,
            )
        ).first()
        if event and event.adjusted_start_ts and event.adjusted_end_ts:
            # 临时修改候选边界用于重渲染。
            orig_start = c.start_ts
            orig_end = c.end_ts
            c.start_ts = event.adjusted_start_ts
            c.end_ts = event.adjusted_end_ts
            db.add(c)

            try:
                clip = await asyncio.to_thread(produce_clip, candidate_id, auto_upload=False)
            finally:
                # 恢复原始边界。
                c.start_ts = orig_start
                c.end_ts = orig_end
                db.add(c)
        else:
            clip = await asyncio.to_thread(produce_clip, candidate_id, auto_upload=False)

    if clip is None:
        raise HTTPException(status_code=500, detail="渲染失败")
    return {"clip_id": clip.id, "file_path": clip.file_path, "title": clip.title}


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
