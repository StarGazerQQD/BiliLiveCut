"""Transcripts."""

from __future__ import annotations

import json
from difflib import SequenceMatcher
from typing import Any

from sqlmodel import Session, select

from app.db.models import (
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
    Topic,
    TopicStatus,
    Transcript,
)
from app.db.session import get_session
from app.pipeline.stage_result import make_idempotency_key, make_pipeline_key, make_stage_key


class TranscriptNotFoundError(LookupError):
    """指定的转写或原始片段不存在。"""


class TranscriptRetranscribeConflict(RuntimeError):
    """当前转写关联的人工或成片资产不允许自动覆盖。"""


def correct_transcript(
    transcript_id: int,
    corrected_text: str,
    *,
    aliases: dict[str, str] | None = None,
    learn_dictionary: bool = True,
    actor: str = "local-admin",
) -> dict[str, Any]:
    """保存人工转写并把可信纠错回流到当前直播间词典。"""
    corrected = corrected_text.strip()
    if not corrected:
        raise ValueError("纠正后的转写不能为空")
    if len(corrected) > 200_000:
        raise ValueError("纠正后的转写超过 200000 字符")

    with get_session() as db:
        transcript = db.get(Transcript, transcript_id)
        if transcript is None:
            raise TranscriptNotFoundError("转写不存在")
        segment = db.get(RawSegment, transcript.segment_id)
        if segment is None:
            raise TranscriptNotFoundError("转写对应的原始片段不存在")
        from app.db.models import LiveRoom, RecordingSession

        session = db.get(RecordingSession, segment.session_id)
        room = db.get(LiveRoom, session.room_id) if session is not None else None
        original = transcript.final_text or transcript.text
        inferred = derive_aliases_from_correction(original, corrected)
        learned = {**inferred, **(aliases or {})}
        if learn_dictionary and room is not None and learned:
            from app.analysis.room_config import learn_room_aliases

            room.room_config_json = json.dumps(learn_room_aliases(room, learned), ensure_ascii=False)
            db.add(room)

        auxiliary = _decode_auxiliary(transcript.auxiliary_json)
        auxiliary.pop("transcript_refinement", None)
        auxiliary["manual_correction"] = {
            "actor": actor,
            "original_text": original,
            "learned_aliases": learned if learn_dictionary else {},
        }
        transcript.text = corrected
        transcript.final_text = corrected
        transcript.final_text_source = "manual"
        transcript.words_json = None
        transcript.auxiliary_json = json.dumps(auxiliary, ensure_ascii=False)
        db.add(transcript)
        session_id = segment.session_id

    from app.analysis.reanalysis import request_session_reanalysis

    reanalysis_requested = request_session_reanalysis(
        session_id,
        reason=f"transcript_manual_correction:{transcript_id}",
        retranscribe=False,
    )
    return {
        "transcript_id": transcript_id,
        "session_id": session_id,
        "learned_aliases": learned if learn_dictionary else {},
        "reanalysis": {"session_id": session_id, "requested": reanalysis_requested},
    }


def derive_aliases_from_correction(original: str, corrected: str) -> dict[str, str]:
    """从小范围人工替换中提取适合作为 ASR 房间词典的映射。"""
    aliases: dict[str, str] = {}
    matcher = SequenceMatcher(a=original, b=corrected, autojunk=False)
    if matcher.ratio() < 0.6:
        return aliases
    for operation, i1, i2, j1, j2 in matcher.get_opcodes():
        if operation != "replace":
            continue
        if i2 - i1 <= 1 and j2 - j1 <= 1:
            left = 1 if i1 > 0 and j1 > 0 else 0
            right_context = 1 if i2 < len(original) and j2 < len(corrected) else 0
            wrong = _clean_alias_term(original[i1 - left : i2 + right_context])
            right = _clean_alias_term(corrected[j1 - left : j2 + right_context])
        else:
            wrong = _clean_alias_term(original[i1:i2])
            right = _clean_alias_term(corrected[j1:j2])
        if not wrong or not right or wrong == right:
            continue
        if 2 <= len(wrong) <= 24 and 2 <= len(right) <= 24:
            aliases[wrong] = right
    return aliases


def _clean_alias_term(value: str) -> str:
    """去掉纠错差异两侧的空白和标点。"""
    start = 0
    end = len(value)
    while start < end and not value[start].isalnum():
        start += 1
    while end > start and not value[end - 1].isalnum():
        end -= 1
    return value[start:end]


def _decode_auxiliary(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def list_transcripts(limit: int = 30) -> list[dict[str, Any]]:
    """列出最近的转写文本(用于"实时转写"视图)。

    :param limit: 数量上限。
    :returns: 转写字典列表(按时间降序)。
    """
    with get_session() as db:
        rows = db.exec(
            select(Transcript).order_by(Transcript.created_at.desc())  # type: ignore[attr-defined]
        ).all()[:limit]
        segments = {
            segment.id: segment
            for segment in db.exec(select(RawSegment).where(RawSegment.id.in_([row.segment_id for row in rows]))).all()
        }
        from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

        sources = source_identities_for_sessions(
            db,
            (segment.session_id for segment in segments.values()),
        )
    result: list[dict[str, Any]] = []
    for transcript in rows:
        segment = segments.get(transcript.segment_id)
        source = sources.get(segment.session_id, unknown_source_identity()) if segment else unknown_source_identity()
        refinement: dict[str, Any] = {}
        if transcript.auxiliary_json:
            try:
                auxiliary = json.loads(transcript.auxiliary_json)
            except (json.JSONDecodeError, TypeError):
                auxiliary = {}
            if isinstance(auxiliary, dict) and isinstance(auxiliary.get("transcript_refinement"), dict):
                refinement = auxiliary["transcript_refinement"]
        result.append(
            {
                "id": transcript.id,
                "segment_id": transcript.segment_id,
                "language": transcript.language,
                "text": transcript.text,
                "raw_text": transcript.final_text or transcript.text,
                "summary": str(refinement.get("summary", "")),
                "llm_refined": refinement.get("applied") is True,
                "primary_backend": transcript.primary_backend,
                "created_at": transcript.created_at.isoformat() if transcript.created_at else None,
                "session_id": segment.session_id if segment else None,
                **source,
            }
        )
    return result


def retranscribe_transcript(transcript_id: int) -> dict[str, int]:
    """安全删除污染转写并把原始片段重新放入转写队列。

    仅自动清理尚未人工处理、尚未渲染的分析产物。若任务正在执行，或已存在
    人工审核、主题确认、成片等不可安全覆盖的数据，则拒绝操作并保留全部数据。

    :param transcript_id: 要重新识别的转写 ID。
    :returns: 重新入队的 ``task_id`` 与 ``segment_id``。
    :raises TranscriptNotFoundError: 转写或原始片段不存在。
    :raises TranscriptRetranscribeConflict: 存在活动任务或受保护的下游资产。
    """
    with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")

        transcript = db.get(Transcript, transcript_id)
        if transcript is None:
            raise TranscriptNotFoundError("转写不存在")
        segment = db.get(RawSegment, transcript.segment_id)
        if segment is None or segment.id is None:
            raise TranscriptNotFoundError("转写对应的原始片段不存在")

        task = db.exec(select(SegmentTask).where(SegmentTask.segment_id == segment.id)).first()
        if task is not None and (
            task.claimed_by is not None
            or task.lease_token is not None
            or task.stage
            in {
                TaskStatus.TRANSCRIBING,
                TaskStatus.ANALYZING,
                TaskStatus.RENDERING,
                TaskStatus.PUBLISHING,
            }
        ):
            raise TranscriptRetranscribeConflict("该片段任务正在执行，请稍后重试")
        if task is not None and task.clip_id is not None:
            raise TranscriptRetranscribeConflict("该片段已经关联成片，不能自动覆盖转写")

        candidate = db.get(HighlightCandidate, task.candidate_id) if task and task.candidate_id else None
        event = db.get(HighlightEvent, task.event_id) if task and task.event_id else None
        if event is None and candidate is not None and candidate.id is not None:
            event = db.exec(select(HighlightEvent).where(HighlightEvent.candidate_id == candidate.id)).first()
        if candidate is None and event is not None and event.candidate_id is not None:
            candidate = db.get(HighlightCandidate, event.candidate_id)

        _assert_downstream_is_discardable(db, task, candidate, event)

        if task is not None:
            task.candidate_id = None
            task.event_id = None
            task.clip_id = None
            db.add(task)
            db.flush()

        if event is not None and event.id is not None:
            memberships = db.exec(select(HighlightTopic).where(HighlightTopic.event_id == event.id)).all()
            for membership in memberships:
                db.delete(membership)
            db.flush()
            db.delete(event)
            db.flush()
        if candidate is not None:
            db.delete(candidate)
            db.flush()

        db.delete(transcript)
        segment.status = SegmentStatus.RECORDED
        db.add(segment)

        if task is None:
            task = SegmentTask(
                segment_id=segment.id,
                session_id=segment.session_id,
                stage=TaskStatus.QUEUED_FOR_TRANS,
                pipeline_key=make_pipeline_key(segment.id),
                stage_key=make_stage_key(segment.id, TaskStatus.QUEUED_FOR_TRANS),
                idempotency_key=make_idempotency_key(segment.id, TaskStatus.QUEUED_FOR_TRANS),
            )
        else:
            _reset_task_for_transcription(task)
        db.add(task)
        db.flush()
        if task.id is None:
            raise RuntimeError("重转写任务创建失败")
        return {"task_id": task.id, "segment_id": segment.id}


def _assert_downstream_is_discardable(
    db: Session,
    task: SegmentTask | None,
    candidate: HighlightCandidate | None,
    event: HighlightEvent | None,
) -> None:
    """确认下游只有可安全重建的自动分析数据。"""
    candidate_id = candidate.id if candidate is not None else None
    event_id = event.id if event is not None else None

    if event is not None:
        if event.review_by != "auto" or event.adjusted_start_ts is not None or event.adjusted_end_ts is not None:
            raise TranscriptRetranscribeConflict("该片段已有人工审核或边界调整，不能自动覆盖转写")
        memberships = db.exec(select(HighlightTopic).where(HighlightTopic.event_id == event_id)).all()
        if any(item.is_manual or item.confirmed_by_user for item in memberships):
            raise TranscriptRetranscribeConflict("该片段已有人工确认的主题归类，不能自动覆盖转写")
        if event.topic_id is not None:
            topic = db.get(Topic, event.topic_id)
            if topic is not None and topic.status != TopicStatus.AUTO:
                raise TranscriptRetranscribeConflict("该片段已进入人工确认主题，不能自动覆盖转写")
        if db.exec(select(ClipVariant).where(ClipVariant.event_id == event_id)).first() is not None:
            raise TranscriptRetranscribeConflict("该片段已经进入渲染流程，不能自动覆盖转写")

    if candidate_id is not None:
        if db.exec(select(FinalClip).where(FinalClip.candidate_id == candidate_id)).first() is not None:
            raise TranscriptRetranscribeConflict("该片段已经生成成片，不能自动覆盖转写")
        if db.exec(select(ThresholdFeedback).where(ThresholdFeedback.candidate_id == candidate_id)).first() is not None:
            raise TranscriptRetranscribeConflict("该片段已有人工审核反馈，不能自动覆盖转写")

    if task is not None and task.id is not None:
        if candidate_id is not None:
            other = db.exec(
                select(SegmentTask).where(SegmentTask.candidate_id == candidate_id, SegmentTask.id != task.id)
            ).first()
            if other is not None:
                raise TranscriptRetranscribeConflict("候选被其他任务引用，不能自动覆盖转写")
        if event_id is not None:
            other = db.exec(
                select(SegmentTask).where(SegmentTask.event_id == event_id, SegmentTask.id != task.id)
            ).first()
            if other is not None:
                raise TranscriptRetranscribeConflict("高光事件被其他任务引用，不能自动覆盖转写")


def _reset_task_for_transcription(task: SegmentTask) -> None:
    """清除旧阶段状态并重新排队转写，同时保留流程级幂等键。"""
    task.stage = TaskStatus.QUEUED_FOR_TRANS
    task.pipeline_key = task.pipeline_key or make_pipeline_key(task.segment_id)
    task.stage_key = make_stage_key(task.segment_id, TaskStatus.QUEUED_FOR_TRANS)
    task.idempotency_key = make_idempotency_key(task.segment_id, TaskStatus.QUEUED_FOR_TRANS)
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
    task.context_json = None
