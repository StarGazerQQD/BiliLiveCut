"""整场高光时间线总结的持久请求、生成与查询状态。"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from loguru import logger
from sqlmodel import Session, select

from app.core.config import settings
from app.db.entities import (
    AppSetting,
    LiveRoom,
    RawSegment,
    RecordingSession,
    SegmentTask,
    TaskStatus,
    Transcript,
)
from app.db.session import get_session

_RESULT_PREFIX = "session_timeline_summary:"
_REQUEST_PREFIX = "session_timeline_summary_request:"
_REANALYSIS_PREFIX = "session_reanalysis:"
_SUMMARY_VERSION = 2
_REQUEST_FIELDS = {
    "version",
    "session_id",
    "request_id",
    "reason",
    "status",
    "attempts",
    "requested_at",
    "started_at",
    "next_retry_at",
    "last_error",
}
_RESULT_FIELDS = {
    "version",
    "session_id",
    "analysis_basis",
    "transcript_signature",
    "transcript_count",
    "character_count",
    "source",
    "summary",
    "generated_at",
}
_MAX_ATTEMPTS = 3
_GMT8 = timezone(timedelta(hours=8))
_SUMMARY_LOCK = threading.RLock()
_UNSETTLED_STAGES = {
    TaskStatus.RECORDED,
    TaskStatus.QUEUED_FOR_TRANS,
    TaskStatus.TRANSCRIBING,
    TaskStatus.TRANSCRIBED,
    TaskStatus.QUEUED_FOR_ANALYSIS,
    TaskStatus.ANALYZING,
}
_ACTIVE_RESULT_STAGES = {TaskStatus.TRANSCRIBING, TaskStatus.TRANSCRIBED, TaskStatus.ANALYZING}


@dataclass(frozen=True, slots=True)
class SessionSummaryClaim:
    """一个已经原子领取的整场总结请求。"""

    session_id: int
    request_id: str


@dataclass(frozen=True, slots=True)
class SessionTranscriptBlock:
    """按录制时间定位的最终 ASR 文本块。"""

    segment_id: int
    sequence: int
    source_file: str
    start_at: datetime | None
    end_at: datetime | None
    text: str


class SessionSummaryNotReady(RuntimeError):
    """场次在领取后又进入重分析，暂时不能生成总结。"""


def request_session_timeline_summary(
    session_id: int,
    *,
    reason: str,
    force: bool = False,
) -> bool:
    """持久化一场直播的整场时间线总结请求。

    同一场次已有等待或运行请求时，普通请求保持幂等；重分析等明确失效
    操作使用 ``force=True`` 创建新请求版本，使旧线程不能删除新请求。

    :param session_id: 录制会话主键。
    :param reason: 触发原因，写入诊断元数据。
    :param force: 是否覆盖已有活动请求。
    :returns: 是否创建了新请求。
    :raises ValueError: 录制会话不存在。
    """
    with _SUMMARY_LOCK, get_session() as db:
        if db.get(RecordingSession, session_id) is None:
            raise ValueError(f"录制会话不存在: session_id={session_id}")
        created = request_session_timeline_summary_in_session(
            db,
            session_id,
            reason=reason,
            force=force,
        )
    if not created:
        return False
    logger.info("整场高光总结已登记 session={} reason={} force={}", session_id, reason, force)
    return True


def request_session_timeline_summary_in_session(
    db: Session,
    session_id: int,
    *,
    reason: str,
    force: bool = False,
) -> bool:
    """在调用方事务内登记总结请求。"""
    with _SUMMARY_LOCK:
        reason = reason.strip()
        if not reason:
            raise ValueError("整场总结触发原因不能为空")
        if db.get(RecordingSession, session_id) is None:
            raise ValueError(f"录制会话不存在: session_id={session_id}")
        key = _request_key(session_id)
        existing = db.get(AppSetting, key)
        previous = _decode_request(existing.value if existing is not None else None)
        if (
            not force
            and previous.get("version") == _SUMMARY_VERSION
            and previous.get("status") in {"pending", "running"}
        ):
            return False
        payload = {
            "version": _SUMMARY_VERSION,
            "session_id": session_id,
            "request_id": uuid4().hex,
            "reason": reason[:200],
            "status": "pending",
            "attempts": 0,
            "requested_at": _now_iso(),
            "started_at": None,
            "next_retry_at": None,
            "last_error": None,
        }
        value = _encode(payload)
        if existing is None:
            existing = AppSetting(key=key, value=value)
        else:
            existing.value = value
            existing.updated_at = datetime.now(UTC)
        db.add(existing)
        return True


def ensure_session_timeline_summary_requested(session_id: int) -> bool:
    """为已结束且缺少当前整场 ASR 有效总结的场次补登记请求。"""
    with get_session() as db:
        recording = db.get(RecordingSession, session_id)
        if recording is None or recording.ended_at is None:
            return False
        request = db.get(AppSetting, _request_key(session_id))
        request_payload = _decode_request(request.value if request is not None else None)
        if request is not None and request_payload.get("version") == _SUMMARY_VERSION:
            return False
        result = db.get(AppSetting, _result_key(session_id))
        result_payload = _decode_result(result.value if result is not None else None)
        blocks = _session_transcript_blocks(db, session_id)
        if result_payload.get("version") == _SUMMARY_VERSION and result_payload.get(
            "transcript_signature"
        ) == session_transcript_signature(blocks):
            return False
    return request_session_timeline_summary(session_id, reason="timeline_viewed")


def recover_running_session_summary_requests() -> int:
    """服务启动时把上次进程遗留的运行请求恢复为等待状态。"""
    recovered = 0
    with _SUMMARY_LOCK, get_session() as db:
        rows = db.exec(select(AppSetting).where(AppSetting.key.startswith(_REQUEST_PREFIX))).all()
        for row in rows:
            payload = _decode_request(row.value)
            if payload.get("version") != _SUMMARY_VERSION:
                db.delete(row)
                logger.warning("已删除非当前格式的整场总结请求 key={}", row.key)
                continue
            if payload.get("status") != "running":
                continue
            payload.update(
                {
                    "status": "pending",
                    "started_at": None,
                    "next_retry_at": None,
                    "last_error": "服务重启后重新排队",
                }
            )
            row.value = _encode(payload)
            row.updated_at = datetime.now(UTC)
            db.add(row)
            recovered += 1
    if recovered:
        logger.info("已恢复 {} 个整场高光总结请求", recovered)
    return recovered


def claim_pending_session_summary() -> SessionSummaryClaim | None:
    """原子领取一个已经结束且分析稳定的整场总结请求。"""
    now = datetime.now(UTC)
    with _SUMMARY_LOCK, get_session() as db:
        rows = db.exec(
            select(AppSetting).where(AppSetting.key.startswith(_REQUEST_PREFIX)).order_by(AppSetting.updated_at.asc())
        ).all()
        for row in rows:
            payload = _decode_request(row.value)
            if payload.get("version") != _SUMMARY_VERSION:
                db.delete(row)
                logger.warning("已删除非当前格式的整场总结请求 key={}", row.key)
                continue
            if payload.get("status") != "pending":
                continue
            retry_at = _parse_datetime(payload["next_retry_at"])
            if retry_at is not None and retry_at > now:
                continue
            session_id = payload["session_id"]
            request_id = payload["request_id"]
            if row.key != _request_key(session_id):
                db.delete(row)
                logger.warning("已清理键值不一致的整场总结请求 key={}", row.key)
                continue
            if not _session_ready(db, session_id):
                continue
            payload.update(
                {
                    "status": "running",
                    "started_at": now.isoformat(),
                    "next_retry_at": None,
                    "last_error": None,
                }
            )
            row.value = _encode(payload)
            row.updated_at = now
            db.add(row)
            return SessionSummaryClaim(session_id=session_id, request_id=request_id)
    return None


def execute_session_summary_claim(claim: SessionSummaryClaim) -> bool:
    """执行一个已领取请求，并以请求版本保护最终提交。"""
    try:
        result = build_session_timeline_summary(claim.session_id)
    except SessionSummaryNotReady:
        _return_claim_to_queue(claim, error="场次重分析尚未稳定", count_attempt=False)
        return False
    except Exception as exc:  # noqa: BLE001 — 持久任务需要记录并有限重试
        logger.exception("整场高光总结失败 session={}", claim.session_id)
        _return_claim_to_queue(claim, error=str(exc), count_attempt=True)
        return False
    committed = _commit_summary_result(claim, result)
    if not committed:
        return False
    logger.info(
        "整场高光总结完成 session={} transcripts={} source={}",
        claim.session_id,
        result["transcript_count"],
        result["source"],
    )
    return True


def build_session_timeline_summary(session_id: int) -> dict[str, Any]:
    """把整场最终 ASR 一次性交给 LLM，生成全场高光分析。"""
    with get_session() as db:
        if not _session_ready(db, session_id):
            raise SessionSummaryNotReady(f"录制会话尚未完成最终分析: session_id={session_id}")
        blocks = _session_transcript_blocks(db, session_id)
        source_label = _session_source_label(db, session_id)

    signature = session_transcript_signature(blocks)
    if not blocks:
        generated = "本场没有可供分析的最终 ASR 文本。"
        source = "empty"
    else:
        from app.analysis import llm

        raw = llm.call_text(
            _summary_prompt(source_label, blocks),
            max_tokens=settings.highlight_llm_max_tokens,
        )
        parsed = llm.extract_json(raw) if raw else None
        candidate = parsed.get("summary") if parsed is not None else None
        if not isinstance(candidate, str) or not candidate.strip():
            raise RuntimeError("整场 ASR 分析未返回有效 summary")
        generated = candidate.strip()
        source = "llm"

    return {
        "version": _SUMMARY_VERSION,
        "session_id": session_id,
        "analysis_basis": "full_session_asr",
        "transcript_signature": signature,
        "transcript_count": len(blocks),
        "character_count": sum(len(block.text) for block in blocks),
        "source": source,
        "summary": generated,
        "generated_at": _now_iso(),
    }


def session_timeline_summary_view(
    session_id: int,
    *,
    processing_state: str,
    ended: bool,
) -> dict[str, Any]:
    """返回时间线 API 可直接展示的整场总结状态。"""
    with get_session() as db:
        result_row = db.get(AppSetting, _result_key(session_id))
        request_row = db.get(AppSetting, _request_key(session_id))
        result = _decode_result(result_row.value if result_row is not None else None)
        request = _decode_request(request_row.value if request_row is not None else None)
        blocks = _session_transcript_blocks(db, session_id)
    signature = session_transcript_signature(blocks)

    base = {
        "analysis_basis": "full_session_asr",
        "transcript_count": len(blocks),
        "character_count": sum(len(block.text) for block in blocks),
        "summary": None,
        "source": None,
        "generated_at": None,
        "needs_regeneration": False,
    }
    if not ended or processing_state == "recording":
        return {**base, "status": "recording"}
    if processing_state in {"finalizing", "processing"}:
        return {**base, "status": "waiting"}

    request_status = request["status"] if request else ""
    if request_status in {"pending", "running"}:
        return {
            **base,
            "status": "generating" if request_status == "running" else "pending",
        }
    if request_status == "failed":
        return {
            **base,
            "status": "failed",
            "error": request["last_error"] or "整场总结生成失败",
        }

    if result and result["transcript_signature"] == signature:
        return {
            **base,
            "status": "ready",
            "transcript_count": result["transcript_count"],
            "character_count": result["character_count"],
            "summary": result["summary"],
            "source": result["source"],
            "generated_at": _gmt8_iso(result["generated_at"]),
        }
    # get_session_timeline 已在读取视图前为缺失/过期结果补登记请求；首个响应
    # 直接展示等待态，避免把内部签名失效细节暴露成需要用户处理的问题。
    return {**base, "status": "pending", "needs_regeneration": True}


def session_transcript_signature(blocks: list[SessionTranscriptBlock]) -> str:
    """计算影响整场分析的最终 ASR 签名。"""
    payload = [
        {
            "segment_id": block.segment_id,
            "sequence": block.sequence,
            "source_file": block.source_file,
            "start_at": block.start_at,
            "end_at": block.end_at,
            "text": block.text,
        }
        for block in blocks
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _summary_prompt(source_label: str, blocks: list[SessionTranscriptBlock]) -> str:
    full_transcript = "\n".join(
        f"[{_gmt8_clock(block.start_at)}][{block.source_file}] {block.text}" for block in blocks
    )
    return (
        "你是一名中文直播内容编辑。下面是同一场直播按时间排序的全部最终 ASR。"
        "请把全部文本作为一个完整上下文，只调用一次分析并完成全局推理，写出贯穿全场的高光总结。\n\n"
        "要求：\n"
        "1. 根据前后文判断事件、人物、因果和真正高光，不得把各个 ASR 块分别摘抄后直接拼接；\n"
        "2. 严格按 GMT+8 时间从早到晚梳理，可跨越原始录制断点合并同一事件；\n"
        "3. 写成一份统一、可读的中文分析，用 HH:MM:SS 标记重要事件；可以按事件组织段落，"
        "但不得按五分钟录制片段逐块作答；\n"
        "4. 保留关键事实和数字，不添加 ASR 之外的信息，对无法确认的识别错误不要臆测；\n"
        "5. 只输出 JSON，不要代码围栏或其他说明。\n\n"
        '输出格式：{"summary":"按全场 ASR 深度分析后的完整高光总结"}\n\n'
        f"直播来源：{source_label}\n"
        f"整场 ASR：\n{full_transcript}"
    )


def _commit_summary_result(claim: SessionSummaryClaim, result: dict[str, Any]) -> bool:
    with _SUMMARY_LOCK, get_session() as db:
        request = db.get(AppSetting, _request_key(claim.session_id))
        request_payload = _decode_request(request.value if request is not None else None)
        if (
            request is None
            or request_payload.get("version") != _SUMMARY_VERSION
            or request_payload.get("request_id") != claim.request_id
        ):
            logger.info("整场总结结果已过期并丢弃 session={}", claim.session_id)
            return False
        key = _result_key(claim.session_id)
        row = db.get(AppSetting, key)
        value = _encode(result)
        if row is None:
            row = AppSetting(key=key, value=value)
        else:
            row.value = value
            row.updated_at = datetime.now(UTC)
        db.add(row)

        db.delete(request)
        return True


def _return_claim_to_queue(
    claim: SessionSummaryClaim,
    *,
    error: str,
    count_attempt: bool,
) -> None:
    with _SUMMARY_LOCK, get_session() as db:
        row = db.get(AppSetting, _request_key(claim.session_id))
        payload = _decode_request(row.value if row is not None else None)
        if row is None or payload.get("version") != _SUMMARY_VERSION or payload.get("request_id") != claim.request_id:
            return
        attempts = payload["attempts"] + (1 if count_attempt else 0)
        failed = count_attempt and attempts >= _MAX_ATTEMPTS
        delay_s = min(60, 2 ** max(1, attempts)) if count_attempt else 2
        payload.update(
            {
                "status": "failed" if failed else "pending",
                "attempts": attempts,
                "started_at": None,
                "next_retry_at": None if failed else (datetime.now(UTC) + timedelta(seconds=delay_s)).isoformat(),
                "last_error": error[:2000],
            }
        )
        row.value = _encode(payload)
        row.updated_at = datetime.now(UTC)
        db.add(row)


def _session_ready(db: Session, session_id: int) -> bool:
    recording = db.get(RecordingSession, session_id)
    if recording is None or recording.ended_at is None:
        return False
    if db.get(AppSetting, f"{_REANALYSIS_PREFIX}{session_id}") is not None:
        return False
    tasks = db.exec(select(SegmentTask).where(SegmentTask.session_id == session_id)).all()
    for task in tasks:
        if task.claimed_by is not None or task.lease_token is not None:
            return False
        if task.stage in _UNSETTLED_STAGES:
            return False
        if task.stage == TaskStatus.TRANSIENT_FAILED and task.failed_stage in _ACTIVE_RESULT_STAGES:
            return False
        if task.stage == TaskStatus.STALE and (task.failed_stage is None or task.failed_stage in _ACTIVE_RESULT_STAGES):
            return False
    return True


def _session_transcript_blocks(db: Session, session_id: int) -> list[SessionTranscriptBlock]:
    """按片段时间和序号读取一场直播的全部最终 ASR。"""
    segments = db.exec(
        select(RawSegment)
        .where(RawSegment.session_id == session_id)
        .order_by(RawSegment.start_ts.asc(), RawSegment.seq.asc(), RawSegment.id.asc())
    ).all()
    segment_ids = [segment.id for segment in segments if segment.id is not None]
    transcripts = db.exec(select(Transcript).where(Transcript.segment_id.in_(segment_ids))).all() if segment_ids else []
    transcript_by_segment = {transcript.segment_id: transcript for transcript in transcripts}
    blocks: list[SessionTranscriptBlock] = []
    for segment in segments:
        if segment.id is None:
            continue
        transcript = transcript_by_segment.get(segment.id)
        text = transcript.final_text.strip() if transcript is not None else ""
        if not text:
            continue
        source_file = segment.file_path.strip().replace("\\", "/").rsplit("/", maxsplit=1)[-1]
        blocks.append(
            SessionTranscriptBlock(
                segment_id=segment.id,
                sequence=segment.seq,
                source_file=source_file,
                start_at=segment.start_ts,
                end_at=segment.end_ts,
                text=text,
            )
        )
    return blocks


def _session_source_label(db: Session, session_id: int) -> str:
    """返回整场 ASR 分析中使用的直播来源。"""
    recording = db.get(RecordingSession, session_id)
    if recording is None:
        raise ValueError(f"录制会话不存在: session_id={session_id}")
    room = db.get(LiveRoom, recording.room_id)
    if room is None:
        return f"会话 #{session_id}"
    primary = (room.uploader_name or room.title or "").strip()
    if room.room_id is None:
        return primary or f"会话 #{session_id}"
    return f"{primary} · 房间 {room.room_id}" if primary else f"房间 {room.room_id}"


def _decode_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _decode_request(raw: str | None) -> dict[str, Any]:
    """解析当前且唯一的整场分析请求格式。"""
    value = _decode_object(raw)
    if set(value) != _REQUEST_FIELDS or value.get("version") != _SUMMARY_VERSION:
        return {}
    if not isinstance(value["session_id"], int) or isinstance(value["session_id"], bool):
        return {}
    if not isinstance(value["request_id"], str) or not value["request_id"]:
        return {}
    if not isinstance(value["reason"], str) or not value["reason"]:
        return {}
    if value["status"] not in {"pending", "running", "failed"}:
        return {}
    if not isinstance(value["attempts"], int) or isinstance(value["attempts"], bool) or value["attempts"] < 0:
        return {}
    if not isinstance(value["requested_at"], str) or not value["requested_at"]:
        return {}
    for field_name in ("started_at", "next_retry_at", "last_error"):
        if value[field_name] is not None and not isinstance(value[field_name], str):
            return {}
    return value


def _decode_result(raw: str | None) -> dict[str, Any]:
    """解析当前且唯一的整场分析结果格式。"""
    value = _decode_object(raw)
    if set(value) != _RESULT_FIELDS or value.get("version") != _SUMMARY_VERSION:
        return {}
    if not isinstance(value["session_id"], int) or isinstance(value["session_id"], bool):
        return {}
    if value["analysis_basis"] != "full_session_asr":
        return {}
    if not isinstance(value["transcript_signature"], str) or len(value["transcript_signature"]) != 64:
        return {}
    for field_name in ("transcript_count", "character_count"):
        if not isinstance(value[field_name], int) or isinstance(value[field_name], bool) or value[field_name] < 0:
            return {}
    if value["source"] not in {"llm", "empty"}:
        return {}
    if not isinstance(value["summary"], str) or not value["summary"]:
        return {}
    if not isinstance(value["generated_at"], str) or not value["generated_at"]:
        return {}
    return value


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _gmt8_iso(value: object) -> str | None:
    """把持久化的 UTC 时间转换成时间线页面约定的 GMT+8 ISO 时间。"""
    parsed = _parse_datetime(value)
    return parsed.astimezone(_GMT8).isoformat() if parsed is not None else None


def _gmt8_clock(value: datetime | None) -> str:
    """把片段起点转换为 GMT+8 时钟。"""
    if value is None:
        return "--:--:--"
    parsed = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return parsed.astimezone(_GMT8).strftime("%H:%M:%S")


def _result_key(session_id: int) -> str:
    return f"{_RESULT_PREFIX}{session_id}"


def _request_key(session_id: int) -> str:
    return f"{_REQUEST_PREFIX}{session_id}"


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
