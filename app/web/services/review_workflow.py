"""人工审核工作流的领取、草稿、历史和审计辅助函数。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from app.core.config import settings
from app.db.models import HighlightCandidate, HighlightEvent, SegmentTask, SystemLog

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
    event: HighlightEvent,
    candidate: HighlightCandidate,
    task: SegmentTask | None,
    *,
    action: str,
    actor: str,
) -> None:
    """在修改前保存可撤销快照。"""
    data = workflow(event)
    history = data.get("history", [])
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "action": action,
            "actor": actor,
            "at": datetime.now(UTC).isoformat(),
            "adjusted_start_ts": _iso(event.adjusted_start_ts),
            "adjusted_end_ts": _iso(event.adjusted_end_ts),
            "review_status": event.review_status,
            "review_reason": event.review_reason,
            "review_by": event.review_by,
            "candidate_status": candidate.status,
            "task_stage": task.stage if task else None,
        }
    )
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
