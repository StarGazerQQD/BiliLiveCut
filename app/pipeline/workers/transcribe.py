"""转写阶段 Worker — compute/commit 真正分离。

transcribe_compute 只做 ASR 计算, 不写 Transcript/RawSegment。
commit_transcript 在租约保护下单事务写入全部业务对象。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from app.core.config import settings
from app.db.entities import (
    HotspotEvent,
    RawSegment,
    RecordingSession,
    SegmentStatus,
    SegmentTask,
    SessionStatus,
    TaskStatus,
    Transcript,
)
from app.db.entities.base import utcnow
from app.db.session import get_session
from app.pipeline.lease import LeaseLostError, TaskLease, still_owns_lease
from app.pipeline.stage_result import enqueue_next, mark_completed
from app.pipeline.task_context import event_first_context, update_event_first_context

if TYPE_CHECKING:
    from app.analysis.transcription.pipeline import ASRPipeline

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
        _refine_transcript_for_storage,
        get_task_pipeline,
    )
    from app.analysis.transcription.quality import (  # noqa: PLC0415
        assess_transcript_quality,
        transcript_quality_payload,
    )

    # 1) 读取 Task -> segment_id
    with get_session() as db:
        task = db.get(SegmentTask, task_id)
        if task is None:
            return {"error": "task not found"}
        segment_id = task.segment_id
        task_session_id = task.session_id
        task_event_first = event_first_context(task.context_json)

    # 2) 读取片段元数据
    try:
        ctx = _load_segment_ctx(segment_id)
    except ValueError as exc:
        return {"error": str(exc)}

    file_path = ctx["file_path"]
    initial_prompt = ctx["initial_prompt"]
    if ctx["session_id"] != task_session_id:
        return {
            "error": (
                f"任务来源不一致: task={task_id} session={task_session_id},"
                f" segment={segment_id} session={ctx['session_id']}"
            )
        }

    if not file_path:
        return {"error": "segment has no file_path"}

    # 3) 热点局部 ASR 与完整后台 ASR 使用同一 production pipeline，
    #    但作为两次独立可调度工作负载执行。
    pipeline = get_task_pipeline()
    if task_event_first.get("asr_mode") == "hotspot":
        windows = task_event_first.get("attention_windows")
        return _transcribe_hotspot_attention(
            pipeline=pipeline,
            file_path=file_path,
            initial_prompt=initial_prompt,
            segment_id=segment_id,
            session_id=task_session_id,
            windows=windows if isinstance(windows, list) else [],
        )

    result = pipeline.transcribe(file_path, initial_prompt=initial_prompt)

    # 4) 阿里别名纠错
    text = _apply_room_aliases(result.text, segment_id)
    final_text = _apply_room_aliases(result.final_text or result.text, segment_id)
    raw_text = final_text or text
    quality = assess_transcript_quality(raw_text)
    if not quality.usable:
        _logger.warning(
            "transcribe_quality_degraded segment=%s backend=%s reason=%s repetition_ratio=%.3f",
            segment_id,
            result.backend,
            quality.reason,
            quality.repetition_ratio,
        )
    refinement = _refine_transcript_for_storage(raw_text) if quality.usable else None
    display_text = refinement.clean_text if refinement is not None else raw_text

    # 5) words JSON
    words_json = json.dumps(
        [{"w": w.word, "start": w.start, "end": w.end} for seg in result.segments for w in seg.words],
        ensure_ascii=False,
    )

    # 6) 辅助特征 JSON
    auxiliary_payload: dict[str, object] = {"asr_quality": transcript_quality_payload(quality)}
    if result.emotions or result.reviewed_segments:
        auxiliary_payload.update(
            {
                "emotions": [
                    {"type": e.event_type, "start": e.start, "end": e.end, "confidence": e.confidence}
                    for e in result.emotions
                ],
                "reviewed_segments": result.reviewed_segments,
                "engine": result.backend,
            }
        )
    if result.metadata.get("repetition_repair"):
        auxiliary_payload["repetition_repair"] = result.metadata["repetition_repair"]
    if refinement is not None:
        auxiliary_payload["transcript_refinement"] = {
            "applied": True,
            "summary": refinement.summary,
        }
    auxiliary_json = json.dumps(auxiliary_payload, ensure_ascii=False) if auxiliary_payload else None

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
        len(display_text),
        result.backend,
        result.review_triggered,
    )

    return {
        "transcribed": True,
        "segment_id": segment_id,
        "session_id": task_session_id,
        "text": display_text,
        "words_json": words_json,
        "avg_logprob": avg_logprob_val,
        "auxiliary_json": auxiliary_json,
        "language": result.language,
        "base_text": result.base_text or result.text,
        "final_text": raw_text,
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


def _transcribe_hotspot_attention(
    *,
    pipeline: ASRPipeline,
    file_path: str,
    initial_prompt: str | None,
    segment_id: int,
    session_id: int,
    windows: list[object],
) -> dict[str, Any]:
    """先识别持久化 attention windows，不在本次任务中执行完整转写。"""
    from app.analysis.transcription.pipeline import _apply_room_aliases  # noqa: PLC0415
    from app.analysis.transcription.quality import (  # noqa: PLC0415
        assess_transcript_quality,
        transcript_quality_payload,
    )

    results: list[dict[str, object]] = []
    for raw_window in windows:
        if not isinstance(raw_window, Mapping):
            continue
        event_key = raw_window.get("event_key")
        start_value = raw_window.get("start_offset_s")
        end_value = raw_window.get("end_offset_s")
        if not isinstance(event_key, str) or not event_key:
            continue
        if not isinstance(start_value, (int, float)) or not isinstance(end_value, (int, float)):
            continue
        start_s = float(start_value)
        end_s = float(end_value)
        if end_s <= start_s:
            continue
        try:
            result = pipeline.transcribe_window(
                file_path,
                start_s,
                end_s,
                initial_prompt=initial_prompt,
            )
            text = _apply_room_aliases(result.final_text or result.text, segment_id).strip()
            quality = assess_transcript_quality(text)
            results.append(
                {
                    "event_key": event_key,
                    "start_offset_s": start_s,
                    "end_offset_s": end_s,
                    "text": text,
                    "quality": transcript_quality_payload(quality),
                    "backend": result.backend,
                    "model_id": result.model_id,
                    "model_revision": result.model_revision,
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            _logger.warning(
                "hotspot_asr_unavailable segment=%s event=%s window=[%.3f,%.3f) error=%s",
                segment_id,
                event_key,
                start_s,
                end_s,
                exc,
            )
            results.append(
                {
                    "event_key": event_key,
                    "start_offset_s": start_s,
                    "end_offset_s": end_s,
                    "text": "",
                    "quality": {
                        "state": "unavailable",
                        "usable": False,
                        "reason": "runtime_error",
                    },
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
            )

    return {
        "hotspot_asr": True,
        "segment_id": segment_id,
        "session_id": session_id,
        "attention_results": results,
    }


def commit_transcript(lease: TaskLease, compute_result: dict[str, Any], ms: int) -> None:
    """单事务提交转写结果: 先校验租约, 再幂等创建 Transcript。

    事务顺序:
    1. 验证租约
    2. 检查 compute 错误 (不创建非法记录)
    3. 幂等查询 Transcript
    4. 不存在则创建
    5. 更新 RawSegment 状态为 TRANSCRIBED
    6. 推进 Task

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

            # 检查 compute 错误 — 不创建非法记录
            if "error" in compute_result:
                _logger.error("commit_transcript_error: task=%s error=%s", lease.task_id, compute_result["error"])
                from app.pipeline.stage_result import mark_failed

                mark_failed(
                    task,
                    compute_result["error"],
                    permanent="task not found" in str(compute_result.get("error", "")),
                )
                db.add(task)
                db.commit()
                return

            segment_id = compute_result.get("segment_id")
            if segment_id is None or segment_id < 0:
                _logger.error("commit_transcript: invalid segment_id=%s", segment_id)
                from app.pipeline.stage_result import mark_failed

                mark_failed(task, "invalid segment_id in compute result", permanent=True)
                db.add(task)
                db.commit()
                return
            result_session_id = compute_result.get("session_id", task.session_id)
            segment = db.get(RawSegment, segment_id)
            if (
                segment_id != task.segment_id
                or result_session_id != task.session_id
                or segment is None
                or segment.session_id != task.session_id
            ):
                from app.pipeline.stage_result import mark_failed

                mark_failed(task, "transcribe compute result source mismatch", permanent=True)
                db.add(task)
                db.commit()
                return

            if compute_result.get("hotspot_asr") is True:
                _commit_hotspot_attention(db, task, compute_result, ms)
                db.commit()
                return

            # 幂等: 查询已有 Transcript
            from sqlmodel import select as _sel  # noqa: PLC0415

            existing = db.exec(_sel(Transcript).where(Transcript.segment_id == segment_id)).first()
            if existing is not None:
                _logger.info("idempotent_skip: segment=%s 已有 Transcript id=%s", segment_id, existing.id)
                # 修复 RawSegment 状态
                seg = db.get(RawSegment, segment_id)
                if seg is not None and seg.status != SegmentStatus.TRANSCRIBED:
                    seg.status = SegmentStatus.TRANSCRIBED
                    db.add(seg)
                task.context_json = update_event_first_context(
                    task.context_json,
                    analysis_pass="candidate",
                    asr_mode="completed",
                    asr_evidence_state=_stored_transcript_state(existing),
                )
                mark_completed(task, ms)
                enqueue_next(task, TaskStatus.TRANSCRIBED)
                db.add(task)
                db.commit()
                return

            # 创建 Transcript
            transcript = Transcript(
                segment_id=segment_id,
                language=compute_result.get("language"),
                words_json=compute_result.get("words_json"),
                avg_logprob=compute_result.get("avg_logprob"),
                auxiliary_json=compute_result.get("auxiliary_json"),
                base_text=compute_result.get("base_text"),
                final_text=str(compute_result.get("final_text") or ""),
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
            task.context_json = update_event_first_context(
                task.context_json,
                analysis_pass="candidate",
                asr_mode="completed",
                asr_evidence_state=_compute_result_asr_state(compute_result),
            )
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


def _commit_hotspot_attention(
    db: Session,
    task: SegmentTask,
    compute_result: Mapping[str, object],
    processing_ms: int,
) -> None:
    """提交局部 ASR 证据并把同一任务重新排队为后台完整转写。"""
    raw_results = compute_result.get("attention_results")
    results = [dict(item) for item in raw_results if isinstance(item, Mapping)] if isinstance(raw_results, list) else []
    result_by_key = {
        str(item["event_key"]): item
        for item in results
        if isinstance(item.get("event_key"), str) and item.get("event_key")
    }

    states: list[str] = []
    for event_key, item in result_by_key.items():
        event = db.exec(
            select(HotspotEvent).where(
                HotspotEvent.event_key == event_key,
                HotspotEvent.session_id == task.session_id,
            )
        ).first()
        if event is None:
            _logger.warning("hotspot_asr_event_missing task=%s event=%s", task.id, event_key)
            continue
        state = _attention_result_state(item)
        states.append(state)
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            event.transcript_text = text.strip()
        event.evidence_json = _merge_attention_evidence(event.evidence_json, event_key, item, state)
        event.updated_at = utcnow()
        db.add(event)

    current = event_first_context(task.context_json)
    raw_windows = current.get("attention_windows")
    windows: list[dict[str, object]] = []
    if isinstance(raw_windows, list):
        for raw_window in raw_windows:
            if not isinstance(raw_window, Mapping):
                continue
            window = dict(raw_window)
            event_key = window.get("event_key")
            item = result_by_key.get(event_key) if isinstance(event_key, str) else None
            if item is not None:
                state = _attention_result_state(item)
                window["status"] = "failed" if state == "unavailable" else "completed"
                window["quality_state"] = state
                error = item.get("error")
                if isinstance(error, str) and error:
                    window["error"] = error[:500]
            windows.append(window)

    task.context_json = update_event_first_context(
        task.context_json,
        analysis_pass="candidate",
        asr_mode="background",
        asr_evidence_state=_aggregate_evidence_state(states),
        attention_windows=windows,
        hotspot_asr_completed_at=utcnow().isoformat(),
    )
    task.priority = _background_asr_priority(db, task.session_id)
    task.processing_time_ms = processing_ms
    enqueue_next(task, TaskStatus.QUEUED_FOR_TRANS)
    db.add(task)
    _logger.info(
        "hotspot_asr_committed task=%s segment=%s windows=%s next=background priority=%s",
        task.id,
        task.segment_id,
        len(results),
        task.priority,
    )


def _merge_attention_evidence(
    raw_evidence: str | None,
    event_key: str,
    result: Mapping[str, object],
    state: str,
) -> str:
    """幂等写入一条热点局部 ASR 证据，不破坏其他模态。"""
    try:
        payload = json.loads(raw_evidence) if raw_evidence else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    raw_items = payload.get("items")
    items = [dict(item) for item in raw_items if isinstance(item, Mapping)] if isinstance(raw_items, list) else []
    evidence_id = f"hotspot-asr:{event_key}"
    items = [item for item in items if item.get("id") != evidence_id]
    text = result.get("text")
    excerpts = [text.strip()] if isinstance(text, str) and text.strip() else []
    detail: dict[str, object] = {
        "state": state,
        "start_offset_s": result.get("start_offset_s"),
        "end_offset_s": result.get("end_offset_s"),
        "backend": result.get("backend"),
        "model_id": result.get("model_id"),
        "model_revision": result.get("model_revision"),
        "quality": result.get("quality"),
    }
    if result.get("error_type") is not None:
        detail["error_type"] = result.get("error_type")
    if result.get("error") is not None:
        detail["error"] = result.get("error")
    items.append(
        {
            "id": evidence_id,
            "type": "asr",
            "score": 1.0 if state == "available" else (0.35 if state == "degraded" else 0.0),
            "detail": detail,
            "excerpts": excerpts,
        }
    )
    payload["version"] = 1
    payload["items"] = items
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _attention_result_state(result: Mapping[str, object]) -> str:
    quality = result.get("quality")
    if isinstance(quality, Mapping):
        state = quality.get("state")
        if state in {"available", "degraded", "unavailable"}:
            return str(state)
    return "unavailable"


def _aggregate_evidence_state(states: list[str]) -> str:
    if "available" in states:
        return "available"
    if "degraded" in states:
        return "degraded"
    return "unavailable"


def _background_asr_priority(db: Session, session_id: int) -> int:
    session = db.get(RecordingSession, session_id)
    if session is not None and session.status in {
        SessionStatus.STARTING,
        SessionStatus.RECORDING,
        SessionStatus.RECONNECTING,
        SessionStatus.RECONNECTED,
    }:
        return settings.near_live_asr_priority
    return settings.background_asr_priority


def _compute_result_asr_state(compute_result: Mapping[str, object]) -> str:
    raw = compute_result.get("auxiliary_json")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            quality = payload.get("asr_quality")
            if isinstance(quality, Mapping) and quality.get("state") in {"available", "degraded"}:
                return str(quality["state"])
    return "degraded"


def _stored_transcript_state(transcript: Transcript) -> str:
    return _compute_result_asr_state({"auxiliary_json": transcript.auxiliary_json})


def run_transcribe(lease: TaskLease) -> None:
    """执行转写阶段: 计算与提交分离。

    心跳由 scheduler 的 heartbeat thread 管理, 不在 run_* 中重复写入。

    :param lease: 任务租约。
    """
    t0 = time.time()
    compute_result = transcribe_compute(lease.task_id)
    ms_val = int((time.time() - t0) * 1000)
    commit_transcript(lease, compute_result, ms_val)
