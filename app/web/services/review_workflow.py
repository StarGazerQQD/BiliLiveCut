"""人工审核工作流的领取、草稿、历史和审计辅助函数。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from sqlmodel import select

from app.core.config import settings
from app.db.entities import FinalClip, HighlightCandidate, HighlightEvent, SegmentTask, SystemLog

if TYPE_CHECKING:
    from sqlmodel import Session

WORKFLOW_KEY = "_review_workflow"
WORKFLOW_VERSION = 1
MAX_HISTORY = 20
_WORKFLOW_FIELDS = {
    "version",
    "claimed_by",
    "claimed_at",
    "claim_expires_at",
    "draft",
    "history",
}
_HISTORY_FIELDS = {
    "action",
    "actor",
    "at",
    "adjusted_start_ts",
    "adjusted_end_ts",
    "review_status",
    "review_reason",
    "review_by",
    "candidate_status",
    "task_states",
    "clip_statuses",
}
_TASK_STATE_FIELDS = {
    "id",
    "stage",
    "stage_key",
    "attempts",
    "last_error",
    "error_is_permanent",
    "next_retry_at",
    "claimed_by",
    "claimed_at",
    "heartbeat_at",
    "lease_token",
    "completed_at",
    "total_elapsed_ms",
}
_CLIP_STATE_FIELDS = {"id", "status"}


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
    """解析当前特征 JSON；非空损坏值必须显式失败。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("高光特征不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("高光特征必须是 JSON 对象")
    return value


def model_features(raw: str | None) -> dict[str, Any]:
    """返回不含内部审核元数据的模型特征副本。"""
    features = decode_features(raw)
    features.pop(WORKFLOW_KEY, None)
    return features


def workflow(event: HighlightEvent) -> dict[str, Any]:
    """读取事件上的审核工作流元数据。"""
    return workflow_from_features(event.features_json)


def workflow_from_features(raw: str | None) -> dict[str, Any]:
    """从特征 JSON 读取当前审核工作流格式。"""
    features = decode_features(raw)
    if WORKFLOW_KEY not in features:
        return _empty_workflow()
    value = features[WORKFLOW_KEY]
    _validate_workflow(value)
    return value


def has_review_draft(raw: str | None) -> bool:
    """返回当前审核工作流中是否存在人工草稿。"""
    return workflow_from_features(raw)["draft"] is not None


def save_workflow(event: HighlightEvent, value: dict[str, Any]) -> None:
    """在保留模型特征的前提下写回审核工作流元数据。"""
    _validate_workflow(value)
    features = decode_features(event.features_json)
    features[WORKFLOW_KEY] = value
    event.features_json = json.dumps(features, ensure_ascii=False, separators=(",", ":"))


def claim_state(event: HighlightEvent, *, now: datetime | None = None) -> dict[str, Any]:
    """返回领取状态，并把已过期的领取视为未领取。"""
    data = workflow(event)
    actor = data["claimed_by"]
    expires_at = _parse_datetime(data["claim_expires_at"])
    current = now or datetime.now(UTC)
    active = bool(actor and expires_at and expires_at > current)
    return {
        "active": active,
        "claimed_by": actor if active else None,
        "claimed_at": data["claimed_at"] if active else None,
        "claim_expires_at": data["claim_expires_at"] if active else None,
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
        data[key] = None
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
    data["draft"] = None
    save_workflow(event, data)


def push_history(
    db: Session,
    event: HighlightEvent,
    candidate: HighlightCandidate,
    *,
    action: str,
    actor: str,
) -> None:
    """在修改前保存可撤销快照。

    :param db: 当前审核事务使用的数据库会话。
    :param event: 即将修改的审核事件。
    :param candidate: 事件关联候选。
    :param action: 审核动作名称。
    :param actor: 操作者标识。
    """
    data = workflow(event)
    history = data["history"]
    tasks = db.exec(
        select(SegmentTask).where(SegmentTask.candidate_id == candidate.id).order_by(SegmentTask.created_at.desc())
    ).all()
    clips = db.exec(select(FinalClip).where(FinalClip.candidate_id == candidate.id)).all()
    snapshot = {
        "action": action,
        "actor": actor,
        "at": datetime.now(UTC).isoformat(),
        "adjusted_start_ts": _iso(event.adjusted_start_ts),
        "adjusted_end_ts": _iso(event.adjusted_end_ts),
        "review_status": event.review_status,
        "review_reason": event.review_reason,
        "review_by": event.review_by,
        "candidate_status": candidate.status,
        "task_states": [_task_state(task) for task in tasks],
        "clip_statuses": [{"id": clip.id, "status": clip.status} for clip in clips if clip.id is not None],
    }
    _validate_history_snapshot(snapshot)
    history.append(snapshot)
    data["history"] = history[-MAX_HISTORY:]
    save_workflow(event, data)


def pop_history(event: HighlightEvent) -> dict[str, Any]:
    """弹出最近一次可撤销快照。"""
    data = workflow(event)
    history = data["history"]
    if not history:
        raise HTTPException(status_code=409, detail="没有可撤销的审核操作")
    snapshot = history.pop()
    data["history"] = history
    save_workflow(event, data)
    return snapshot


def restore_related_state(db: Session, candidate_id: int, snapshot: dict[str, Any]) -> None:
    """恢复审核快照中的任务控制字段和成片状态。

    :param db: 当前审核事务使用的数据库会话。
    :param candidate_id: 快照所属候选 ID。
    :param snapshot: :func:`push_history` 生成的历史快照。
    """
    _validate_history_snapshot(snapshot)
    for raw_state in snapshot["task_states"]:
        task = db.get(SegmentTask, raw_state["id"])
        if task is None or task.candidate_id != candidate_id:
            raise ValueError(f"审核快照任务不属于候选: task_id={raw_state['id']}")
        _restore_task_state(task, raw_state)
        db.add(task)

    for raw_state in snapshot["clip_statuses"]:
        clip = db.get(FinalClip, raw_state["id"])
        if clip is None or clip.candidate_id != candidate_id:
            raise ValueError(f"审核快照成片不属于候选: clip_id={raw_state['id']}")
        clip.status = raw_state["status"]
        db.add(clip)


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


def public_workflow(event: HighlightEvent, actor: str, role: str) -> dict[str, Any]:
    """返回供前端显示且不泄露其他审核员草稿的工作流状态。"""
    data = workflow(event)
    current = claim_state(event)
    draft = data["draft"]
    if draft is not None and draft["updated_by"] != actor and role != "admin":
        draft = None
    history = data["history"]
    return {
        "claim": current,
        "draft": draft,
        "can_undo": bool(history),
        "history_count": len(history),
    }


def _empty_workflow() -> dict[str, Any]:
    """返回当前审核工作流的完整初始格式。"""
    return {
        "version": WORKFLOW_VERSION,
        "claimed_by": None,
        "claimed_at": None,
        "claim_expires_at": None,
        "draft": None,
        "history": [],
    }


def _validate_workflow(value: object) -> None:
    """拒绝旧版、缺字段或包含额外字段的审核工作流。"""
    if not isinstance(value, dict) or set(value) != _WORKFLOW_FIELDS:
        raise ValueError("审核工作流字段不符合当前格式")
    if value["version"] != WORKFLOW_VERSION:
        raise ValueError(f"审核工作流版本必须为 {WORKFLOW_VERSION}")
    for field_name in ("claimed_by", "claimed_at", "claim_expires_at"):
        if value[field_name] is not None and not isinstance(value[field_name], str):
            raise ValueError(f"审核工作流字段 {field_name} 必须是字符串或 null")
    if (value["claimed_by"] is None) != (value["claimed_at"] is None) or (value["claimed_by"] is None) != (
        value["claim_expires_at"] is None
    ):
        raise ValueError("审核领取字段必须同时为空或同时存在")
    for field_name in ("claimed_at", "claim_expires_at"):
        if value[field_name] is not None and _parse_datetime(value[field_name]) is None:
            raise ValueError(f"审核工作流字段 {field_name} 必须是 ISO 时间")
    draft = value["draft"]
    if draft is not None:
        if not isinstance(draft, dict) or set(draft) != {"decision", "reason", "updated_at", "updated_by"}:
            raise ValueError("审核草稿字段不符合当前格式")
        if draft["decision"] is not None and not isinstance(draft["decision"], str):
            raise ValueError("审核草稿 decision 必须是字符串或 null")
        if draft["reason"] is not None and not isinstance(draft["reason"], str):
            raise ValueError("审核草稿 reason 必须是字符串或 null")
        if not isinstance(draft["updated_by"], str) or not draft["updated_by"]:
            raise ValueError("审核草稿 updated_by 必须是非空字符串")
        if _parse_datetime(draft["updated_at"]) is None:
            raise ValueError("审核草稿 updated_at 必须是 ISO 时间")
    history = value["history"]
    if not isinstance(history, list) or len(history) > MAX_HISTORY:
        raise ValueError(f"审核历史必须是不超过 {MAX_HISTORY} 项的数组")
    for snapshot in history:
        _validate_history_snapshot(snapshot)


def _validate_history_snapshot(snapshot: object) -> None:
    """校验当前审核撤销快照的完整字段与类型。"""
    if not isinstance(snapshot, dict) or set(snapshot) != _HISTORY_FIELDS:
        raise ValueError("审核历史快照字段不符合当前格式")
    for field_name in ("action", "actor", "at", "review_status", "review_by", "candidate_status"):
        if not isinstance(snapshot[field_name], str) or not snapshot[field_name]:
            raise ValueError(f"审核历史字段 {field_name} 必须是非空字符串")
    if _parse_datetime(snapshot["at"]) is None:
        raise ValueError("审核历史 at 必须是 ISO 时间")
    for field_name in ("adjusted_start_ts", "adjusted_end_ts"):
        if snapshot[field_name] is not None and _parse_datetime(snapshot[field_name]) is None:
            raise ValueError(f"审核历史字段 {field_name} 必须是 ISO 时间或 null")
    if snapshot["review_reason"] is not None and not isinstance(snapshot["review_reason"], str):
        raise ValueError("审核历史 review_reason 必须是字符串或 null")
    if not isinstance(snapshot["task_states"], list) or not isinstance(snapshot["clip_statuses"], list):
        raise ValueError("审核历史关联状态必须是数组")
    for state in snapshot["task_states"]:
        _validate_task_state(state)
    for state in snapshot["clip_statuses"]:
        if not isinstance(state, dict) or set(state) != _CLIP_STATE_FIELDS:
            raise ValueError("审核历史成片状态字段不符合当前格式")
        if isinstance(state["id"], bool) or not isinstance(state["id"], int):
            raise ValueError("审核历史成片 id 必须是整数")
        if not isinstance(state["status"], str) or not state["status"]:
            raise ValueError("审核历史成片 status 必须是非空字符串")


def _validate_task_state(state: object) -> None:
    """校验当前审核历史中的任务快照。"""
    if not isinstance(state, dict) or set(state) != _TASK_STATE_FIELDS:
        raise ValueError("审核历史任务状态字段不符合当前格式")
    if isinstance(state["id"], bool) or not isinstance(state["id"], int):
        raise ValueError("审核历史任务 id 必须是整数")
    if not isinstance(state["stage"], str) or not state["stage"]:
        raise ValueError("审核历史任务 stage 必须是非空字符串")
    if isinstance(state["attempts"], bool) or not isinstance(state["attempts"], int) or state["attempts"] < 0:
        raise ValueError("审核历史任务 attempts 必须是非负整数")
    if not isinstance(state["error_is_permanent"], bool):
        raise ValueError("审核历史任务 error_is_permanent 必须是布尔值")
    for field_name in ("stage_key", "last_error", "claimed_by", "lease_token"):
        if state[field_name] is not None and not isinstance(state[field_name], str):
            raise ValueError(f"审核历史任务字段 {field_name} 必须是字符串或 null")
    for field_name in ("next_retry_at", "claimed_at", "heartbeat_at", "completed_at"):
        if state[field_name] is not None and _parse_datetime(state[field_name]) is None:
            raise ValueError(f"审核历史任务字段 {field_name} 必须是 ISO 时间或 null")
    elapsed = state["total_elapsed_ms"]
    if elapsed is not None and (isinstance(elapsed, bool) or not isinstance(elapsed, int) or elapsed < 0):
        raise ValueError("审核历史任务 total_elapsed_ms 必须是非负整数或 null")


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
    _validate_task_state(state)
    task.stage = state["stage"]
    task.stage_key = state["stage_key"]
    task.attempts = state["attempts"]
    task.last_error = state["last_error"]
    task.error_is_permanent = state["error_is_permanent"]
    task.next_retry_at = _parse_datetime(state["next_retry_at"])
    task.claimed_by = state["claimed_by"]
    task.claimed_at = _parse_datetime(state["claimed_at"])
    task.heartbeat_at = _parse_datetime(state["heartbeat_at"])
    task.lease_token = state["lease_token"]
    task.completed_at = _parse_datetime(state["completed_at"])
    task.total_elapsed_ms = state["total_elapsed_ms"]
