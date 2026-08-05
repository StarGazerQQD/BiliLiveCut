"""录制场次重分析队列管理。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from loguru import logger
from sqlmodel import Session, select

from app.analysis.timeline import TIMELINE_ANALYSIS_VERSION
from app.db.models import (
    AppSetting,
    ClipVariant,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    HighlightTopic,
    RawSegment,
    SegmentStatus,
    SegmentTask,
    TaskStatus,
    ThresholdFeedback,
    Transcript,
)
from app.db.session import get_session
from app.pipeline.stage_result import make_idempotency_key, make_pipeline_key, make_stage_key

_PENDING_KEY_PREFIX = "session_reanalysis:"
_UNSETTLED_ANALYSIS_STAGES = {
    TaskStatus.RECORDED,
    TaskStatus.QUEUED_FOR_TRANS,
    TaskStatus.TRANSCRIBING,
    TaskStatus.TRANSCRIBED,
    TaskStatus.QUEUED_FOR_ANALYSIS,
    TaskStatus.ANALYZING,
}
_OLD_RESULT_STAGES = {TaskStatus.TRANSCRIBING, TaskStatus.TRANSCRIBED, TaskStatus.ANALYZING}


@dataclass(slots=True)
class ReanalysisResult:
    """场次重分析入队结果。"""

    session_id: int
    queued: int = 0
    preserved: int = 0
    skipped_active: int = 0
    retranscribe: int = 0

    def as_dict(self) -> dict[str, int]:
        """返回 API 可序列化结果。"""
        return asdict(self)


def queue_session_reanalysis(
    session_id: int,
    *,
    reason: str,
    retranscribe: bool = False,
) -> ReanalysisResult:
    """重新排队一场直播的安全分段，同时保留所有人工成果。

    人工审核、人工边界、审核草稿、阈值反馈、主题或成片任一存在时，整段
    保持不动。其余自动候选会被清理并重新评分；词典或模型变更可选择先
    重跑 ASR，但人工纠正过的转写永不被覆盖。
    """
    result = ReanalysisResult(session_id=session_id)
    with get_session() as db:
        segments = db.exec(
            select(RawSegment).where(RawSegment.session_id == session_id).order_by(RawSegment.seq.asc())
        ).all()
        if not segments:
            raise ValueError(f"录制会话不存在或没有分段: session_id={session_id}")

        tasks_by_segment = {
            task.segment_id: task
            for task in db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).all()
        }
        active_tasks = [task for task in tasks_by_segment.values() if _task_is_active(task)]
        if active_tasks:
            # 严禁部分重置：否则一部分任务被重排、另一部分仍在提交旧结果，会形成混合版本时间线。
            result.skipped_active = len(active_tasks)
            return result

        for segment in segments:
            task = tasks_by_segment.get(segment.id)
            events = db.exec(select(HighlightEvent).where(HighlightEvent.segment_id == segment.id)).all()
            if any(_event_is_protected(db, event) for event in events):
                result.preserved += 1
                continue

            _delete_auto_events(db, events)
            transcript = db.exec(select(Transcript).where(Transcript.segment_id == segment.id)).first()
            rerun_asr = bool(retranscribe and transcript is not None and transcript.final_text_source != "manual")
            needs_asr = rerun_asr or transcript is None
            if task is None:
                task = SegmentTask(
                    segment_id=segment.id,
                    session_id=session_id,
                    pipeline_key=make_pipeline_key(segment.id),
                )
            _reset_task(task, reason=reason, rerun_asr=needs_asr)
            if needs_asr:
                if rerun_asr and transcript is not None:
                    db.delete(transcript)
                    result.retranscribe += 1
                segment.status = SegmentStatus.RECORDED
            else:
                segment.status = SegmentStatus.TRANSCRIBED
            db.add(segment)
            db.add(task)
            result.queued += 1
    return result


def request_session_reanalysis(
    session_id: int,
    *,
    reason: str,
    retranscribe: bool = False,
) -> bool:
    """持久化场次重分析请求，等待当前尾段流水线稳定后执行。

    同一场次的重复请求会合并；只要任一请求要求重新转写，最终请求就保留该要求。
    没有原始分段，或既没有流水线任务也未启用自动分析的场次不会入队。
    """
    from app.db.models import LiveRoom, RecordingSession

    key = f"{_PENDING_KEY_PREFIX}{session_id}"
    with get_session() as db:
        recording = db.get(RecordingSession, session_id)
        if recording is None:
            raise ValueError(f"录制会话不存在: session_id={session_id}")
        if db.exec(select(RawSegment.id).where(RawSegment.session_id == session_id).limit(1)).first() is None:
            return False
        has_tasks = db.exec(select(SegmentTask.id).where(SegmentTask.session_id == session_id).limit(1)).first()
        room = db.get(LiveRoom, recording.room_id)
        if has_tasks is None and (room is None or not room.auto_analyze):
            return False

        existing = db.get(AppSetting, key)
        previous = _decode_pending_payload(existing.value if existing is not None else None)
        payload = {
            "session_id": session_id,
            "reason": reason[:200],
            "retranscribe": bool(retranscribe or previous.get("retranscribe", False)),
            "requested_at": datetime.now(UTC).isoformat(),
        }
        if existing is None:
            existing = AppSetting(key=key, value=json.dumps(payload, ensure_ascii=False))
        else:
            existing.value = json.dumps(payload, ensure_ascii=False)
            existing.updated_at = datetime.now(UTC)
        db.add(existing)
    logger.info("场次重分析已登记 session={} reason={} retranscribe={}", session_id, reason, payload["retranscribe"])
    return True


def process_pending_session_reanalyses(*, limit: int = 4) -> list[ReanalysisResult]:
    """处理已经稳定的持久化场次重分析请求。

    转写或分析仍在排队、执行或等待重试时保持请求不动。执行成功后才删除标记，
    因此应用重启不会丢失下播后的最终跨分片分析。
    """
    with get_session() as db:
        rows = db.exec(
            select(AppSetting)
            .where(AppSetting.key.startswith(_PENDING_KEY_PREFIX))
            .order_by(AppSetting.updated_at.asc())
            .limit(max(1, limit))
        ).all()
        pending = [(row.key, row.value) for row in rows]

    completed: list[ReanalysisResult] = []
    for key, raw_payload in pending:
        payload = _decode_pending_payload(raw_payload)
        session_id = _session_id_from_pending(key, payload)
        if session_id is None:
            _delete_pending_request(key)
            logger.warning("已清理无效的场次重分析请求 key={}", key)
            continue
        if _session_analysis_unsettled(session_id):
            continue
        try:
            result = queue_session_reanalysis(
                session_id,
                reason=str(payload.get("reason") or "session_finalized"),
                retranscribe=bool(payload.get("retranscribe", False)),
            )
        except ValueError as exc:
            _delete_pending_request(key)
            logger.warning("场次重分析请求不可执行并已清理 session={}: {}", session_id, exc)
            continue
        if result.skipped_active:
            continue
        _delete_pending_request(key)
        completed.append(result)
        logger.info(
            "场次最终重分析已入队 session={} queued={} preserved={}",
            session_id,
            result.queued,
            result.preserved,
        )
    return completed


def _event_is_protected(db: Session, event: HighlightEvent) -> bool:
    """判断事件是否包含不可覆盖的人工或下游资产。"""
    if event.review_by != "auto" or _has_manual_boundary(event) or _has_review_draft(event.features_json):
        return True
    candidate = db.get(HighlightCandidate, event.candidate_id) if event.candidate_id else None
    if event.id is not None:
        if db.exec(select(ClipVariant).where(ClipVariant.event_id == event.id)).first() is not None:
            return True
        if db.exec(select(HighlightTopic).where(HighlightTopic.event_id == event.id)).first() is not None:
            return True
    if candidate is not None and candidate.id is not None:
        if db.exec(select(FinalClip).where(FinalClip.candidate_id == candidate.id)).first() is not None:
            return True
        if db.exec(select(ThresholdFeedback).where(ThresholdFeedback.candidate_id == candidate.id)).first() is not None:
            return True
    return False


def _has_review_draft(raw: str | None) -> bool:
    """检查候选特征中是否保存了人工审核草稿。"""
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    workflow = payload.get("_review_workflow", {}) if isinstance(payload, dict) else {}
    return isinstance(workflow, dict) and isinstance(workflow.get("draft"), dict)


def _has_manual_boundary(event: HighlightEvent) -> bool:
    """只有偏离原始窗口的边界才视为人工资产；旧数据中相同值不应阻止重分析。"""
    if event.adjusted_start_ts is not None and event.adjusted_start_ts != event.raw_start_ts:
        return True
    return event.adjusted_end_ts is not None and event.adjusted_end_ts != event.raw_end_ts


def _delete_auto_events(db: Session, events: list[HighlightEvent]) -> None:
    """删除确认无人工资产的旧自动分析结果。"""
    for event in events:
        candidate = db.get(HighlightCandidate, event.candidate_id) if event.candidate_id else None
        db.delete(event)
        db.flush()
        if candidate is not None:
            db.delete(candidate)
            db.flush()


def _reset_task(task: SegmentTask, *, reason: str, rerun_asr: bool) -> None:
    """把非活跃任务重置到转写或分析队列。"""
    target = TaskStatus.QUEUED_FOR_TRANS if rerun_asr else TaskStatus.QUEUED_FOR_ANALYSIS
    task.stage = target
    task.pipeline_key = task.pipeline_key or make_pipeline_key(task.segment_id)
    task.stage_key = make_stage_key(task.segment_id, target)
    task.idempotency_key = make_idempotency_key(task.segment_id, target)
    task.candidate_id = None
    task.event_id = None
    task.clip_id = None
    task.failed_stage = None
    task.attempts = 0
    task.next_retry_at = None
    task.last_error = None
    task.error_is_permanent = False
    task.claimed_by = None
    task.claimed_at = None
    task.heartbeat_at = None
    task.lease_token = None
    task.started_at = None
    task.completed_at = None
    task.processing_time_ms = None
    task.total_elapsed_ms = None
    task.context_json = json.dumps(
        {
            "reanalysis": {
                "reason": reason[:200],
                "analysis_version": TIMELINE_ANALYSIS_VERSION,
                "retranscribe": rerun_asr,
            }
        },
        ensure_ascii=False,
    )


def _task_is_active(task: SegmentTask) -> bool:
    return (
        task.stage
        in {
            TaskStatus.TRANSCRIBING,
            TaskStatus.ANALYZING,
            TaskStatus.RENDERING,
            TaskStatus.PUBLISHING,
        }
        or task.claimed_by is not None
        or task.lease_token is not None
    )


def _session_analysis_unsettled(session_id: int) -> bool:
    """检查场次是否仍有可能提交旧版转写或分析结果的任务。"""
    with get_session() as db:
        tasks = db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).all()
    for task in tasks:
        if task.claimed_by is not None or task.lease_token is not None:
            return True
        if task.stage in _UNSETTLED_ANALYSIS_STAGES:
            return True
        if task.stage == TaskStatus.TRANSIENT_FAILED and task.failed_stage in _OLD_RESULT_STAGES:
            return True
        if task.stage == TaskStatus.STALE and (task.failed_stage is None or task.failed_stage in _OLD_RESULT_STAGES):
            return True
    return False


def _decode_pending_payload(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _session_id_from_pending(key: str, payload: dict[str, object]) -> int | None:
    value = payload.get("session_id")
    try:
        return int(value if value is not None else key.removeprefix(_PENDING_KEY_PREFIX))
    except (TypeError, ValueError):
        return None


def _delete_pending_request(key: str) -> None:
    with get_session() as db:
        row = db.get(AppSetting, key)
        if row is not None:
            db.delete(row)
