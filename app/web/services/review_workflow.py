"""人工审核工作流的领取、草稿、历史和审计辅助函数。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from sqlmodel import select

from app.core.config import settings
from app.db.models import FinalClip, HighlightCandidate, HighlightEvent, SegmentTask, SystemLog

if TYPE_CHECKING:
    from sqlmodel import Session

WORKFLOW_KEY = "_review_workflow"
MAX_HISTORY = 20


def review_actor(request: Request) -> tuple[str, str]:
    """返回认证中间件写入的审核者身份和角色。"""
    actor = str(getattr(request.state, "auth_user", "local-admin"))
    role = str(getattr(request.state, "auth_role", "admin"))
    return actor, role


def begin_review_write(db: Session) -> None:
    """在 SQLite 上提前取得写锁，令领取和释放操作具备互斥性。"""
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def decode_features(raw: str | None) -> dict[str, Any]:
    """解析特征 JSON；损坏或非对象值按空对象处理。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def model_features(raw: str | None) -> dict[str, Any]:
    """返回不含内部审核元数据的模型特征副本。"""
    features = decode_features(raw)
    features.pop(WORKFLOW_KEY, None)
    return features


def workflow(event: HighlightEvent | None) -> dict[str, Any]:
    """读取事件上的审核工作流元数据。"""
    if event is None:
        return {}
    value = decode_features(event.features_json).get(WORKFLOW_KEY, {})
    return value if isinstance(value, dict) else {}


def save_workflow(event: HighlightEvent, value: dict[str, Any]) -> None:
    """在保留模型特征的前提下写回审核工作流元数据。"""
    features = decode_features(event.features_json)
    features[WORKFLOW_KEY] = value
    event.features_json = json.dumps(features, ensure_ascii=False, separators=(",", ":"))


def claim_state(event: HighlightEvent | None, *, now: datetime | None = None) -> dict[str, Any]:
    """返回领取状态，并把已过期的领取视为未领取。"""
    data = workflow(event)
    actor = data.get("claimed_by")
    expires_at = _parse_datetime(data.get("claim_expires_at"))
    current = now or datetime.now(UTC)
    active = bool(actor and expires_at and expires_at > current)
    return {
        "active": active,
        "claimed_by": actor if active else None,
        "claimed_at": data.get("claimed_at") if active else None,
        "claim_expires_at": data.get("claim_expires_at") if active else None,
    }


def claim_event(event: HighlightEvent, actor: str, role: str, *, force: bool = False) -> dict[str, Any]:
    """领取事件；管理员可显式强制接管，普通审核员不可覆盖有效领取。"""
    current = claim_state(event)
    if current["active"] and current["claimed_by"] != actor and not (role == "admin" and force):
        raise HTTPException(status_code=409, detail=f"该候选正由 {current['claimed_by']} 审核")
    now = datetime.now(UTC)
    data = workflow(event)
    data.update(
        {
            "claimed_by": actor,
            "claimed_at": now.isoformat(),
            "claim_expires_at": (now + timedelta(seconds=settings.review_claim_ttl_s)).isoformat(),
        }
    )
    save_workflow(event, data)
    return claim_state(event, now=now)


def release_event(event: HighlightEvent, actor: str, role: str) -> None:
    """释放自己的领取；管理员可以释放任意领取。"""
    current = claim_state(event)
    if current["active"] and current["claimed_by"] != actor and role != "admin":
        raise HTTPException(status_code=409, detail=f"该候选正由 {current['claimed_by']} 审核")
    data = workflow(event)
    for key in ("claimed_by", "claimed_at", "claim_expires_at"):
        data.pop(key, None)
    save_workflow(event, data)


def require_edit_claim(event: HighlightEvent, actor: str, role: str) -> None:
    """要求审核员持有领取；管理员仅可直接操作未领取项。"""
    current = claim_state(event)
    if role == "admin" and (not current["active"] or current["claimed_by"] == actor):
        return
    if not current["active"] or current["claimed_by"] != actor:
        if role == "admin" and current["active"]:
            raise HTTPException(status_code=409, detail="请先强制接管该候选再修改")
        raise HTTPException(status_code=409, detail="请先领取该候选再修改")


def refresh_claim(event: HighlightEvent, actor: str) -> None:
    """审核员活动时延长其领取租约。"""
    current = claim_state(event)
    if current["active"] and current["claimed_by"] == actor:
        data = workflow(event)
        data["claim_expires_at"] = (datetime.now(UTC) + timedelta(seconds=settings.review_claim_ttl_s)).isoformat()
        save_workflow(event, data)


def save_draft(event: HighlightEvent, actor: str, payload: dict[str, Any]) -> dict[str, Any]:
    """保存当前审核者的草稿。"""
    now = datetime.now(UTC).isoformat()
    draft = {**payload, "updated_at": now, "updated_by": actor}
    data = workflow(event)
    data["draft"] = draft
    save_workflow(event, data)
    return draft


def clear_draft(event: HighlightEvent) -> None:
    """清除已提交的审核草稿。"""
    data = workflow(event)
    data.pop("draft", None)
    save_workflow(event, data)


def push_history(
    db: Session,
    event: HighlightEvent,
    candidate: HighlightCandidate,
    *,
    action: str,
    actor: str,
    include_related_state: bool = False,
) -> None:
    """在修改前保存可撤销快照。

    :param db: 当前审核事务使用的数据库会话。
    :param event: 即将修改的审核事件。
    :param candidate: 事件关联候选。
    :param action: 审核动作名称。
    :param actor: 操作者标识。
    :param include_related_state: 是否同时快照会被拒绝流程改写的任务和成片。
    """
    data = workflow(event)
    history = data.get("history", [])
    if not isinstance(history, list):
        history = []
    tasks = db.exec(
        select(SegmentTask).where(SegmentTask.candidate_id == candidate.id).order_by(SegmentTask.created_at.desc())
    ).all()
    snapshot: dict[str, Any] = {
        "action": action,
        "actor": actor,
        "at": datetime.now(UTC).isoformat(),
        "adjusted_start_ts": _iso(event.adjusted_start_ts),
        "adjusted_end_ts": _iso(event.adjusted_end_ts),
        "review_status": event.review_status,
        "review_reason": event.review_reason,
        "review_by": event.review_by,
        "candidate_status": candidate.status,
        "task_stage": tasks[0].stage if tasks else None,
    }
    if include_related_state:
        clips = db.exec(select(FinalClip).where(FinalClip.candidate_id == candidate.id)).all()
        snapshot["task_states"] = [_task_state(task) for task in tasks]
        snapshot["clip_statuses"] = [{"id": clip.id, "status": clip.status} for clip in clips if clip.id is not None]
    history.append(snapshot)
    data["history"] = history[-MAX_HISTORY:]
    save_workflow(event, data)


def pop_history(event: HighlightEvent) -> dict[str, Any]:
    """弹出最近一次可撤销快照。"""
    data = workflow(event)
    history = data.get("history", [])
    if not isinstance(history, list) or not history:
        raise HTTPException(status_code=409, detail="没有可撤销的审核操作")
    snapshot = history.pop()
    data["history"] = history
    save_workflow(event, data)
    return snapshot


def restore_related_state(db: Session, candidate_id: int, snapshot: dict[str, Any]) -> bool:
    """恢复审核快照中的任务控制字段和成片状态。

    :param db: 当前审核事务使用的数据库会话。
    :param candidate_id: 快照所属候选 ID。
    :param snapshot: :func:`push_history` 生成的历史快照。
    :returns: 是否恢复了至少一个新版任务快照。
    """
    restored_task = False
    task_states = snapshot.get("task_states", [])
    if isinstance(task_states, list):
        for raw_state in task_states:
            if not isinstance(raw_state, dict) or not isinstance(raw_state.get("id"), int):
                continue
            task = db.get(SegmentTask, raw_state["id"])
            if task is None or task.candidate_id != candidate_id:
                continue
            _restore_task_state(task, raw_state)
            db.add(task)
            restored_task = True

    clip_statuses = snapshot.get("clip_statuses", [])
    if isinstance(clip_statuses, list):
        for raw_state in clip_statuses:
            if not isinstance(raw_state, dict) or not isinstance(raw_state.get("id"), int):
                continue
            status = raw_state.get("status")
            if not isinstance(status, str):
                continue
            clip = db.get(FinalClip, raw_state["id"])
            if clip is None or clip.candidate_id != candidate_id:
                continue
            clip.status = status
            db.add(clip)
    return restored_task


def add_audit(
    db: Session,
    *,
    actor: str,
    action: str,
    candidate_id: int,
    details: dict[str, Any] | None = None,
) -> None:
    """把审核动作写入结构化系统日志。"""
    context = {"actor": actor, "candidate_id": candidate_id, **(details or {})}
    db.add(
        SystemLog(
            level="INFO",
            module="review",
            event=f"review.{action}",
            message=f"{actor} {action} candidate {candidate_id}",
            context_json=json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        )
    )


def public_workflow(event: HighlightEvent | None, actor: str, role: str) -> dict[str, Any]:
    """返回供前端显示且不泄露其他审核员草稿的工作流状态。"""
    data = workflow(event)
    current = claim_state(event)
    draft = data.get("draft")
    if not isinstance(draft, dict) or (draft.get("updated_by") != actor and role != "admin"):
        draft = None
    history = data.get("history", [])
    return {
        "claim": current,
        "draft": draft,
        "can_undo": bool(history),
        "history_count": len(history) if isinstance(history, list) else 0,
    }


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _task_state(task: SegmentTask) -> dict[str, Any]:
    """把取消任务时会改写的字段序列化到审核快照。"""
    return {
        "id": task.id,
        "stage": task.stage,
        "stage_key": task.stage_key,
        "idempotency_key": task.idempotency_key,
        "attempts": task.attempts,
        "last_error": task.last_error,
        "error_is_permanent": task.error_is_permanent,
        "next_retry_at": _iso(task.next_retry_at),
        "claimed_by": task.claimed_by,
        "claimed_at": _iso(task.claimed_at),
        "heartbeat_at": _iso(task.heartbeat_at),
        "lease_token": task.lease_token,
        "completed_at": _iso(task.completed_at),
        "total_elapsed_ms": task.total_elapsed_ms,
    }


def _restore_task_state(task: SegmentTask, state: dict[str, Any]) -> None:
    """从可信的本地审核快照恢复任务字段。"""
    task.stage = str(state["stage"])
    task.stage_key = _optional_string(state.get("stage_key"))
    task.idempotency_key = _optional_string(state.get("idempotency_key"))
    task.attempts = int(state.get("attempts", 0))
    task.last_error = _optional_string(state.get("last_error"))
    task.error_is_permanent = bool(state.get("error_is_permanent", False))
    task.next_retry_at = _parse_datetime(state.get("next_retry_at"))
    task.claimed_by = _optional_string(state.get("claimed_by"))
    task.claimed_at = _parse_datetime(state.get("claimed_at"))
    task.heartbeat_at = _parse_datetime(state.get("heartbeat_at"))
    task.lease_token = _optional_string(state.get("lease_token"))
    task.completed_at = _parse_datetime(state.get("completed_at"))
    total_elapsed_ms = state.get("total_elapsed_ms")
    task.total_elapsed_ms = total_elapsed_ms if isinstance(total_elapsed_ms, int) else None


def _optional_string(value: object) -> str | None:
    """仅接受审核快照中的字符串或空值。"""
    return value if isinstance(value, str) else None
