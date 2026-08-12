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
from app.db.models import AppSetting, RecordingSession, SegmentTask, TaskStatus
from app.db.session import get_session

_RESULT_PREFIX = "session_timeline_summary:"
_REQUEST_PREFIX = "session_timeline_summary_request:"
_REANALYSIS_PREFIX = "session_reanalysis:"
_SUMMARY_VERSION = 1
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
    """在调用方事务内登记总结请求；历史孤儿候选缺少场次时安全跳过。"""
    with _SUMMARY_LOCK:
        if db.get(RecordingSession, session_id) is None:
            return False
        key = _request_key(session_id)
        existing = db.get(AppSetting, key)
        previous = _decode_object(existing.value if existing is not None else None)
        if not force and previous.get("status") in {"pending", "running"}:
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


def ensure_session_timeline_summary_requested(session_id: int, timeline_signature: str) -> bool:
    """为已结束且缺少当前时间线有效总结的场次补登记请求。"""
    with get_session() as db:
        recording = db.get(RecordingSession, session_id)
        if recording is None or recording.ended_at is None:
            return False
        request = db.get(AppSetting, _request_key(session_id))
        if request is not None:
            return False
        result = db.get(AppSetting, _result_key(session_id))
        result_payload = _decode_object(result.value if result is not None else None)
        if result_payload.get("timeline_signature") == timeline_signature:
            return False
    return request_session_timeline_summary(session_id, reason="timeline_viewed")


def recover_running_session_summary_requests() -> int:
    """服务启动时把上次进程遗留的运行请求恢复为等待状态。"""
    recovered = 0
    with _SUMMARY_LOCK, get_session() as db:
        rows = db.exec(select(AppSetting).where(AppSetting.key.startswith(_REQUEST_PREFIX))).all()
        for row in rows:
            payload = _decode_object(row.value)
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
            payload = _decode_object(row.value)
            if payload.get("status") != "pending":
                continue
            retry_at = _parse_datetime(payload.get("next_retry_at"))
            if retry_at is not None and retry_at > now:
                continue
            session_id = _session_id(row.key, payload)
            request_id = str(payload.get("request_id") or "")
            if session_id is None or not request_id:
                db.delete(row)
                logger.warning("已清理损坏的整场总结请求 key={}", row.key)
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
        "整场高光总结完成 session={} points={} source={}",
        claim.session_id,
        result["point_count"],
        result["source"],
    )
    return True


def build_session_timeline_summary(session_id: int) -> dict[str, Any]:
    """只使用一场直播的时间线节点，生成按时序贯穿全场的总结。"""
    with get_session() as db:
        if not _session_ready(db, session_id):
            raise SessionSummaryNotReady(f"录制会话尚未完成最终分析: session_id={session_id}")

    # 局部导入避免时间线查询服务读取总结状态时形成模块初始化环。
    from app.analysis import llm
    from app.web.services.timeline import get_session_timeline

    timeline = get_session_timeline(session_id, include_rejected=False, include_summary=False)
    raw_points = timeline.get("points", [])
    points = [point for point in raw_points if isinstance(point, dict) and not point.get("rejected")]
    session_payload = timeline.get("session")
    source_label = str(
        session_payload.get("source_label")
        if isinstance(session_payload, dict) and session_payload.get("source_label")
        else f"会话 #{session_id}"
    )
    signature = timeline_points_signature(points)
    rule_summary = _rule_summary(points)
    generated = None
    if points:
        raw = llm.call_text(
            _summary_prompt(source_label, points),
            max_tokens=settings.highlight_llm_max_tokens,
        )
        parsed = llm.extract_json(raw) if raw else None
        if parsed is not None:
            candidate = parsed.get("summary")
            if isinstance(candidate, str) and candidate.strip():
                generated = _continuous_text(candidate)

    return {
        "version": _SUMMARY_VERSION,
        "session_id": session_id,
        "timeline_signature": signature,
        "point_count": len(points),
        "source": "llm" if generated else "rules",
        "summary": generated or rule_summary,
        "generated_at": _now_iso(),
    }


def session_timeline_summary_view(
    session_id: int,
    points: list[dict[str, Any]],
    *,
    processing_state: str,
    ended: bool,
) -> dict[str, Any]:
    """返回时间线 API 可直接展示的整场总结状态。"""
    visible_points = [point for point in points if not point.get("rejected")]
    signature = timeline_points_signature(visible_points)
    with get_session() as db:
        result_row = db.get(AppSetting, _result_key(session_id))
        request_row = db.get(AppSetting, _request_key(session_id))
        result = _decode_object(result_row.value if result_row is not None else None)
        request = _decode_object(request_row.value if request_row is not None else None)

    base = {
        "point_count": len(visible_points),
        "summary": None,
        "source": None,
        "generated_at": None,
        "needs_regeneration": False,
    }
    if not ended or processing_state == "recording":
        return {**base, "status": "recording"}
    if processing_state in {"finalizing", "processing"}:
        return {**base, "status": "waiting"}

    request_status = str(request.get("status") or "")
    if request_status in {"pending", "running"}:
        return {
            **base,
            "status": "generating" if request_status == "running" else "pending",
        }
    if request_status == "failed":
        return {
            **base,
            "status": "failed",
            "error": str(request.get("last_error") or "整场总结生成失败"),
        }

    if result.get("timeline_signature") == signature and isinstance(result.get("summary"), str):
        return {
            **base,
            "status": "ready",
            "point_count": int(result.get("point_count") or 0),
            "summary": str(result["summary"]),
            "source": str(result.get("source") or "rules"),
            "generated_at": _gmt8_iso(result.get("generated_at")),
        }
    # get_session_timeline 已在读取视图前为缺失/过期结果补登记请求；首个响应
    # 直接展示等待态，避免把内部签名失效细节暴露成需要用户处理的问题。
    return {**base, "status": "pending", "needs_regeneration": True}


def timeline_points_signature(points: list[dict[str, Any]]) -> str:
    """计算影响整场总结正文的稳定时间线签名。"""
    payload = [
        {
            "candidate_id": point.get("candidate_id"),
            "event_id": point.get("event_id"),
            "clock_gmt8": point.get("clock_gmt8"),
            "start_at_gmt8": point.get("start_at_gmt8"),
            "end_at_gmt8": point.get("end_at_gmt8"),
            "summary": point.get("summary"),
            "representative_danmaku": point.get("representative_danmaku"),
            "confidence": point.get("confidence"),
            "review_status": point.get("review_status"),
        }
        for point in points
        if not point.get("rejected")
    ]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _summary_prompt(source_label: str, points: list[dict[str, Any]]) -> str:
    timeline = [
        {
            "time_gmt8": point.get("clock_gmt8"),
            "highlight": point.get("summary"),
            "representative_danmaku": [
                str(item.get("text"))
                for item in _object_list(point.get("representative_danmaku"))
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ],
            "confidence": point.get("confidence"),
        }
        for point in points
    ]
    return (
        "你是一名中文直播内容编辑。请只根据下面这场直播已经确认可见的高光时间线，"
        "写一段贯穿整场的高光总结。\n\n"
        "要求：\n"
        "1. 严格按 GMT+8 时间从早到晚梳理，不按五分钟录制分段，不讨论分片或处理流程；\n"
        "2. 写成一段连续、可读的中文正文，用 HH:MM:SS 时间标记串起事件，不要标题、列表或小节；\n"
        "3. 保留人物、事件、数字和因果，不添加时间线之外的事实；\n"
        "4. 代表弹幕只可作为当时观众反应的辅助，不得反过来虚构事件；\n"
        "5. 只输出 JSON，不要代码围栏或其他说明。\n\n"
        '输出格式：{"summary":"按时间线整理的一段完整正文"}\n\n'
        f"直播来源：{source_label}\n"
        f"高光时间线：{json.dumps(timeline, ensure_ascii=False, separators=(',', ':'))}"
    )


def _rule_summary(points: list[dict[str, Any]]) -> str:
    if not points:
        return "本场没有识别到可纳入时间线的高光节点。"
    entries: list[str] = []
    for point in points:
        clock = str(point.get("clock_gmt8") or "时间未知")
        summary = str(point.get("summary") or "出现高光").strip().rstrip("。；;，,")
        reactions = [
            str(item.get("text") or "").strip()
            for item in _object_list(point.get("representative_danmaku"))
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        reaction_text = f"，观众集中回应“{'”“'.join(reactions[:2])}”" if reactions else ""
        entries.append(f"{clock}，{summary}{reaction_text}")
    return f"本场共记录 {len(entries)} 个高光：" + "；".join(entries) + "。"


def _continuous_text(value: str) -> str:
    """把模型偶发输出的列表/分段压成一个连续时间线段落。"""
    lines = [line.strip().lstrip("-*• ").strip() for line in value.splitlines()]
    return " ".join(line for line in lines if line)


def _commit_summary_result(claim: SessionSummaryClaim, result: dict[str, Any]) -> bool:
    with _SUMMARY_LOCK, get_session() as db:
        request = db.get(AppSetting, _request_key(claim.session_id))
        request_payload = _decode_object(request.value if request is not None else None)
        if request is None or request_payload.get("request_id") != claim.request_id:
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
        payload = _decode_object(row.value if row is not None else None)
        if row is None or payload.get("request_id") != claim.request_id:
            return
        attempts = int(payload.get("attempts") or 0) + (1 if count_attempt else 0)
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


def _decode_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _object_list(value: object) -> list[dict[str, Any]]:
    """把不可信 JSON 值收窄为对象列表。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


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


def _session_id(key: str, payload: dict[str, Any]) -> int | None:
    raw = payload.get("session_id")
    if raw is None:
        raw = key.removeprefix(_REQUEST_PREFIX)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _result_key(session_id: int) -> str:
    return f"{_RESULT_PREFIX}{session_id}"


def _request_key(session_id: int) -> str:
    return f"{_REQUEST_PREFIX}{session_id}"


def _encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
