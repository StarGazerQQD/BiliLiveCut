"""转写阶段 Worker — compute/commit 真正分离。

transcribe_compute 只做 ASR 计算, 不写 Transcript/RawSegment。
commit_transcript 在租约保护下单事务写入全部业务对象。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.config import settings
from app.db.models import RawSegment, SegmentStatus, SegmentTask, TaskStatus, Transcript
from app.db.session import get_session
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed, mark_heartbeat

_logger = logging.getLogger(__name__)


def _load_segment_ctx(segment_id: int) -> dict[str, Any]:
    """读取转写计算所需的片段元数据 (纯读取, 不写入)。

    :returns: {"file_path": str, "initial_prompt": str | None, "session_id": int}
    """
    from app.analysis.transcription.pipeline import _build_whisper_prompt  # noqa: PLC0415

    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        file_path = segment.file_path
        initial_prompt = _build_whisper_prompt(db, segment)
        session_id = segment.session_id
    return {"file_path": file_path, "initial_prompt": initial_prompt, "session_id": session_id}


def transcribe_compute(task_id: int) -> dict[str, Any]:
    """仅执行转写计算, 不写 Transcript / RawSegment / SegmentTask 状态。

    流程:
    1. 读取片段元数据
    2. 调用 ASR 管线
    3. 应用房间级 aliases 纠错
    4. 序列化所有计算产物
    5. 返回纯数据 dict

    :param task_id: SegmentTask ID。
    :returns: 纯计算产物, 包含 transcribed=True 或 error 标记。
    """
    from app.analysis.transcription.pipeline import (  # noqa: PLC0415
        _apply_room_aliases,
        get_default_pipeline,
    )

    # 1) 读取 Task -> segment_id
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return {"error": "task not found"}
        segment_id = task.segment_id

    # 2) 读取片段元数据
    try:
        ctx = _load_segment_ctx(segment_id)
    except ValueError as exc:
        return {"error": str(exc)}

    file_path = ctx["file_path"]
    initial_prompt = ctx["initial_prompt"]

    if not file_path:
        return {"error": "segment has no file_path"}

    # 3) 执行 ASR
    pipeline = get_default_pipeline()
    result = pipeline.transcribe(file_path, initial_prompt=initial_prompt)

    # 4) 阿里别名纠错
    text = _apply_room_aliases(result.text, segment_id)
    final_text = _apply_room_aliases(result.final_text or result.text, segment_id)

    # 5) words JSON
    words_json = json.dumps(
        [{"w": w.word, "start": w.start, "end": w.end} for seg in result.segments for w in seg.words],
        ensure_ascii=False,
    )

    # 6) 辅助特征 JSON
    auxiliary_json: str | None = None
    if result.emotions or result.reviewed_segments:
        auxiliary_json = json.dumps(
            {
                "emotions": [
                    {"type": e.event_type, "start": e.start, "end": e.end, "confidence": e.confidence}
                    for e in result.emotions
                ],
                "reviewed_segments": result.reviewed_segments,
                "engine": result.backend,
            },
            ensure_ascii=False,
        )

    # 7) review reasons
    review_reasons_json = json.dumps(result.review_reasons, ensure_ascii=False) if result.review_reasons else None

    # 8) 平均 logprob
    avg_logprob_val: float | None = None
    if result.segments:
        fs = result.segments[0]
        if fs.confidence_type == "avg_logprob" and fs.raw_confidence is not None:
            avg_logprob_val = float(fs.raw_confidence)
        elif fs.normalized_confidence is not None:
            avg_logprob_val = fs.normalized_confidence

    _logger.info(
        "transcribe_compute segment=%s text=%d chars backend=%s review=%s",
        segment_id,
        len(final_text or text),
        result.backend,
        result.review_triggered,
    )

    return {
        "transcribed": True,
        "segment_id": segment_id,
        "text": final_text or text,
        "words_json": words_json,
        "avg_logprob": avg_logprob_val,
        "auxiliary_json": auxiliary_json,
        "language": result.language,
        "base_text": result.base_text or result.text,
        "final_text": final_text or result.text,
        "text_version": settings.transcript_version or "v0",
        "primary_backend": result.backend,
        "primary_model_id": result.model_id,
        "primary_model_revision": result.model_revision,
        "review_backend": result.review_backend or None,
        "fallback_backend": result.fallback_backend or None,
        "review_triggered": result.review_triggered,
        "review_risk_score": result.review_risk_score,
        "review_reasons": review_reasons_json,
        "final_text_source": result.final_text_source or "primary",
        "inference_duration": result.inference_duration,
    }


def commit_transcript(lease: TaskLease, compute_result: dict[str, Any], ms: int) -> None:
    """单事务提交转写结果: 先校验租约, 再幂等创建 Transcript。

    事务顺序:
    1. 验证租约
    2. 幂等查询 Transcript
    3. 不存在则创建
    4. 更新 RawSegment 状态
    5. 推进 Task

    :param lease: 任务租约。
    :param compute_result: transcribe_compute 的输出 (必须含 "segment_id" 和 "text")。
    :param ms: 处理耗时 (毫秒)。
    """
    try:
        with get_session() as db:
            if not still_owns_lease(db, lease):
                raise LeaseLostError()

            task = db.get(SegmentTask, lease.task_id)
            if task is None:
                return

            segment_id = compute_result.get("segment_id", -1)

            # 幂等: 查询已有 Transcript
            from sqlmodel import select as _sel  # noqa: PLC0415

            existing = db.exec(_sel(Transcript).where(Transcript.segment_id == segment_id)).first()
            if existing is not None:
                _logger.info("idempotent_skip: segment=%s 已有 Transcript id=%s", segment_id, existing.id)
                mark_completed(task, ms)
                enqueue_next(task, TaskStatus.TRANSCRIBED)
                db.add(task)
                db.commit()
                return

            # 创建 Transcript
            transcript = Transcript(
                segment_id=segment_id,
                language=compute_result.get("language"),
                text=compute_result.get("text"),
                words_json=compute_result.get("words_json"),
                avg_logprob=compute_result.get("avg_logprob"),
                auxiliary_json=compute_result.get("auxiliary_json"),
                base_text=compute_result.get("base_text"),
                final_text=compute_result.get("final_text"),
                primary_backend=compute_result.get("primary_backend"),
                primary_model_id=compute_result.get("primary_model_id"),
                primary_model_revision=compute_result.get("primary_model_revision"),
                review_backend=compute_result.get("review_backend"),
                fallback_backend=compute_result.get("fallback_backend"),
                review_triggered=compute_result.get("review_triggered"),
                review_risk_score=compute_result.get("review_risk_score"),
                review_reasons=compute_result.get("review_reasons"),
                final_text_source=compute_result.get("final_text_source"),
                inference_duration=compute_result.get("inference_duration"),
            )
            db.add(transcript)
            db.flush()
            db.refresh(transcript)

            # 更新 RawSegment
            seg = db.get(RawSegment, segment_id)
            if seg is not None:
                seg.status = SegmentStatus.TRANSCRIBED
                db.add(seg)

            # 推进任务
            mark_completed(task, ms)
            enqueue_next(task, TaskStatus.TRANSCRIBED)
            db.add(task)

            db.commit()

            _logger.info(
                "commit_transcript segment=%s transcript=%s chars=%s",
                segment_id,
                transcript.id,
                len(compute_result.get("text", "")),
            )

    except LeaseLostError:
        _logger.warning("stale_result_discarded: transcript task=%s 已失去租约", lease.task_id)


def run_transcribe(lease: TaskLease) -> None:
    """执行转写阶段: 计算与提交分离。

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
    compute_result = transcribe_compute(lease.task_id)
    ms_val = int((time.time() - t0) * 1000)
    commit_transcript(lease, compute_result, ms_val)
