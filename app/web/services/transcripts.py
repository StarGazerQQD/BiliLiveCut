"""Transcripts."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.runtime_settings import configured_task
from app.db.entities import (
    ClipVariant,
    Danmaku,
    FinalClip,
    HighlightCandidate,
    HighlightEvent,
    HighlightTopic,
    RawSegment,
    RecordingSession,
    SegmentStatus,
    SegmentTask,
    TaskStatus,
    ThresholdFeedback,
    Topic,
    TopicStatus,
    Transcript,
)
from app.db.session import get_session
from app.pipeline.stage_result import make_pipeline_key, make_stage_key


class TranscriptNotFoundError(LookupError):
    """指定的转写或原始片段不存在。"""


class TranscriptRetranscribeConflict(RuntimeError):
    """当前转写关联的人工或成片资产不允许自动覆盖。"""


class TranscriptMediaError(RuntimeError):
    """转写关联的原始媒体无法安全导出。"""


_SOURCE_EXPORT_LOCKS: dict[int, threading.Lock] = {}
_SOURCE_EXPORT_LOCKS_GUARD = threading.Lock()
_GMT8 = timezone(timedelta(hours=8), name="GMT+8")


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
        from app.db.entities import LiveRoom, RecordingSession

        session = db.get(RecordingSession, segment.session_id)
        room = db.get(LiveRoom, session.room_id) if session is not None else None
        original = transcript.final_text
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


def _source_file_name(file_path: str | None) -> str | None:
    """从跨平台保存的原始片段路径中提取文件名。"""
    normalized = (file_path or "").strip().replace("\\", "/")
    return normalized.rsplit("/", maxsplit=1)[-1] or None


@configured_task
def remux_transcript_source(transcript_id: int) -> Path:
    """把转写关联的 TS 无损重封装为首视频帧从 0 秒开始的 MP4。

    MPEG-TS 中 AAC 音频常比首个 H.264/H.265 画面早几十毫秒。常规 ``-c copy``
    会把音频归零，却让视频从一个非零时间戳开始，剪辑软件因而显示一帧黑画面。
    本函数先探测音视频起点差，再用 ``setts`` bitstream filter 仅校正视频包的
    PTS/DTS；视频帧、音频包和编码数据全部保留。输出按源文件大小和修改时间缓存，
    源文件变化后自动生成新路径，避免把陈旧 MP4 当作当前原片。

    :param transcript_id: 转写主键。
    :returns: 可直接剪辑的 MP4 路径。
    :raises TranscriptNotFoundError: 转写或片段不存在。
    :raises TranscriptMediaError: 源文件越界、缺失、探测或 FFmpeg 失败。
    """
    from app.core.paths import clips_dir, raw_dir

    with get_session() as db:
        transcript = db.get(Transcript, transcript_id)
        if transcript is None:
            raise TranscriptNotFoundError("转写不存在")
        segment = db.get(RawSegment, transcript.segment_id)
        if segment is None:
            raise TranscriptNotFoundError("转写对应的原始片段不存在")
        source = Path(segment.file_path).resolve()

    raw_root = raw_dir().resolve()
    if not source.is_relative_to(raw_root):
        raise TranscriptMediaError("原始片段不在受控录像目录内")
    if not source.is_file():
        raise TranscriptMediaError("原始片段文件不存在")
    if source.suffix.casefold() != ".ts":
        raise TranscriptMediaError("仅支持把 MPEG-TS 原片无损导出为 MP4")

    stat = source.stat()
    fingerprint = f"{stat.st_size:x}_{stat.st_mtime_ns:x}"
    export_root = (clips_dir() / "source_exports").resolve()
    export_root.mkdir(parents=True, exist_ok=True)
    output = (export_root / f"segment_{segment.id}_{fingerprint}.mp4").resolve()
    if output.parent != export_root:
        raise TranscriptMediaError("导出路径越界")
    if output.is_file() and output.stat().st_size > 0:
        return output

    lock = _source_export_lock(int(segment.id))
    with lock:
        if output.is_file() and output.stat().st_size > 0:
            return output
        _render_source_export(source, output, int(segment.id), export_root)
    return output


def _source_export_lock(segment_id: int) -> threading.Lock:
    """返回片段级导出锁，避免并发请求互相替换同一临时文件。"""
    with _SOURCE_EXPORT_LOCKS_GUARD:
        return _SOURCE_EXPORT_LOCKS.setdefault(segment_id, threading.Lock())


def _render_source_export(source: Path, output: Path, segment_id: int, export_root: Path) -> None:
    """执行一次无损重封装并原子发布到导出缓存。"""
    from app.core.config import settings
    from app.core.ffmpeg_errors import classify_ffmpeg_error

    video_start, earliest_start = _probe_stream_start_times(source)
    if video_start is None:
        raise TranscriptMediaError("原始片段没有可导出的视频流")
    video_shift = max(0.0, video_start - (earliest_start if earliest_start is not None else video_start))
    temp_output = output.with_name(f"{output.stem}.{uuid4().hex}.partial.mp4")
    command = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
    ]
    if video_shift > 0.0005:
        shift_expression = f"{video_shift:.9f}/TB"
        command += ["-bsf:v", f"setts=pts=PTS-{shift_expression}:dts=DTS-{shift_expression}"]
    command += ["-movflags", "+faststart", str(temp_output)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        temp_output.unlink(missing_ok=True)
        raise TranscriptMediaError("FFmpeg 无损导出超时") from exc
    except OSError as exc:
        temp_output.unlink(missing_ok=True)
        raise TranscriptMediaError(f"无法启动 FFmpeg: {exc}") from exc
    if result.returncode != 0:
        temp_output.unlink(missing_ok=True)
        stderr = result.stderr.decode("utf-8", errors="ignore")
        error_type = classify_ffmpeg_error(result.returncode, stderr)
        raise TranscriptMediaError(f"FFmpeg 无损导出失败 [{error_type.name}]: {stderr}")
    temp_output.replace(output)
    for stale in export_root.glob(f"segment_{segment_id}_*.mp4"):
        if stale != output:
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                # Windows 下载响应可能仍持有旧缓存句柄；旧文件由下次导出再清理，
                # 不能因为清理失败而让本次已完成的导出报错。
                continue


def _probe_stream_start_times(source: Path) -> tuple[float | None, float | None]:
    """探测首视频流起点和所有音视频流中的最早起点。"""
    from app.core.config import settings
    from app.core.ffmpeg_errors import classify_ffmpeg_error

    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,start_time",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=True, text=True, timeout=30)
        streams = json.loads(result.stdout).get("streams", [])
    except subprocess.TimeoutExpired as exc:
        raise TranscriptMediaError("ffprobe 探测原片超时") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        error_type = classify_ffmpeg_error(exc.returncode, stderr)
        raise TranscriptMediaError(f"ffprobe 探测原片失败 [{error_type.name}]: {stderr}") from exc
    except OSError as exc:
        raise TranscriptMediaError(f"无法启动 ffprobe: {exc}") from exc
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise TranscriptMediaError("ffprobe 返回了无效的媒体信息") from exc

    video_start: float | None = None
    starts: list[float] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        try:
            start = float(stream["start_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if stream.get("codec_type") in {"video", "audio"}:
            starts.append(start)
        if video_start is None and stream.get("codec_type") == "video":
            video_start = start
    return video_start, min(starts) if starts else None


def list_recording_session_history() -> list[dict[str, Any]]:
    """返回全部录制场次及其转写、弹幕数量，供历史选择器使用。

    该查询不设置“最近 N 场”上限，避免较早场次再次因为列表截断而不可访问。

    :returns: 按开录时间倒序排列的场次摘要。
    """
    with get_session() as db:
        sessions = db.exec(
            select(RecordingSession).order_by(RecordingSession.started_at.desc())  # type: ignore[attr-defined]
        ).all()
        session_ids = [session.id for session in sessions if session.id is not None]
        if not session_ids:
            return []

        transcript_rows = db.exec(
            select(RawSegment.session_id, func.count(Transcript.id))
            .join(Transcript, Transcript.segment_id == RawSegment.id)
            .where(RawSegment.session_id.in_(session_ids))
            .group_by(RawSegment.session_id)
        ).all()
        danmaku_rows = db.exec(
            select(Danmaku.session_id, func.count(Danmaku.id))
            .where(Danmaku.session_id.in_(session_ids))
            .group_by(Danmaku.session_id)
        ).all()
        from app.web.services.source_identity import source_identities_for_sessions, unknown_source_identity

        sources = source_identities_for_sessions(db, session_ids)

    transcript_counts = {int(session_id): int(count) for session_id, count in transcript_rows}
    danmaku_counts = {int(session_id): int(count) for session_id, count in danmaku_rows}
    return [
        {
            "session_id": int(session.id),
            "status": session.status,
            "started_at": _iso_utc(session.started_at),
            "ended_at": _iso_utc(session.ended_at),
            "started_at_gmt8": _iso_gmt8(session.started_at),
            "ended_at_gmt8": _iso_gmt8(session.ended_at),
            "transcript_count": transcript_counts.get(int(session.id), 0),
            "danmaku_count": danmaku_counts.get(int(session.id), 0),
            **sources.get(int(session.id), unknown_source_identity()),
        }
        for session in sessions
        if session.id is not None
    ]


def _as_utc(value: datetime) -> datetime:
    """把数据库时间规范化为带 UTC 时区的时间。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime | None) -> str | None:
    """返回带时区的 UTC ISO 时间。"""
    return _as_utc(value).isoformat() if value is not None else None


def _iso_gmt8(value: datetime | None) -> str | None:
    """返回 GMT+8 ISO 时间。"""
    return _as_utc(value).astimezone(_GMT8).isoformat() if value is not None else None


def list_transcripts(limit: int = 30, session_id: int | None = None) -> list[dict[str, Any]]:
    """列出最近的转写文本(用于"实时转写"视图)。

    :param limit: 数量上限。
    :param session_id: 仅查询指定录制场次；为空时返回全局最近记录。
    :returns: 转写字典列表(按时间降序)。
    """
    with get_session() as db:
        statement = select(Transcript)
        if session_id is not None:
            statement = statement.join(RawSegment, RawSegment.id == Transcript.segment_id).where(
                RawSegment.session_id == session_id
            )
        rows = db.exec(
            statement.order_by(Transcript.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
        ).all()
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
                "text": transcript.final_text,
                "raw_text": transcript.base_text or transcript.final_text,
                "summary": str(refinement.get("summary", "")),
                "llm_refined": refinement.get("applied") is True,
                "primary_backend": transcript.primary_backend,
                "created_at": transcript.created_at.isoformat() if transcript.created_at else None,
                "session_id": segment.session_id if segment else None,
                "source_file_name": _source_file_name(segment.file_path) if segment else None,
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
        if task is not None and task.candidate_id is not None and candidate is None:
            raise TranscriptRetranscribeConflict("任务关联的候选不存在，不能自动重转写")
        if task is not None and task.event_id is not None and event is None:
            raise TranscriptRetranscribeConflict("任务关联的审核事件不存在，不能自动重转写")
        if candidate is not None:
            if event is None:
                raise TranscriptRetranscribeConflict("候选缺少审核事件，不能自动重转写")
            if event.candidate_id != candidate.id or event.session_id != candidate.session_id:
                raise TranscriptRetranscribeConflict("候选与审核事件关联不一致，不能自动重转写")
        elif event is not None:
            raise TranscriptRetranscribeConflict("审核事件缺少任务候选关联，不能自动重转写")

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
            from app.analysis.reanalysis import detach_hotspot_candidate

            detach_hotspot_candidate(db, candidate.id)
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
    task.pipeline_key = make_pipeline_key(task.segment_id)
    task.stage_key = make_stage_key(task.segment_id, TaskStatus.QUEUED_FOR_TRANS)
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
