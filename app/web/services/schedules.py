"""Schedules."""

from __future__ import annotations

from typing import Any

from sqlmodel import select

from app.core.config import settings
from app.db.models import (
    LiveRoom,
    RecordingSchedule,
)
from app.db.session import get_session


def list_schedules() -> list[dict[str, Any]]:
    """返回所有录制预约(含房间名)。

    :returns: 预约列表(按计划时间升序)。
    """
    with get_session() as db:
        rows = db.exec(select(RecordingSchedule).order_by(RecordingSchedule.scheduled_at)).all()
        result = []
        for s in rows:
            room = db.get(LiveRoom, s.room_id)
            result.append(
                {
                    "id": s.id,
                    "room_id": s.room_id,
                    "scheduled_at": s.scheduled_at.isoformat() if s.scheduled_at else None,
                    "enabled": s.enabled,
                    "recurrent": s.recurrent,
                    "triggered": s.triggered,
                    "room_title": room.title if room else "",
                    "uploader_name": room.uploader_name if room else "",
                }
            )
    return result


def create_schedule(room_id: int, scheduled_at: str, recurrent: str = "") -> dict[str, Any]:
    """创建一个录制预约。

    :param room_id: 直播间 db id。
    :param scheduled_at: ISO 格式的计划时间字符串。
    :param recurrent: 空=一次性, daily=每日。
    :returns: 新建的预约摘要。
    :raises ValueError: 房间不存在或未授权时。
    """
    from datetime import datetime as dt

    with get_session() as db:
        room = db.get(LiveRoom, room_id)
        if room is None:
            raise ValueError(f"房间不存在: id={room_id}")
        if settings.require_authorization and not room.authorized:
            raise ValueError("该直播间未确认授权,无法创建预约。")

        try:
            ts = dt.fromisoformat(scheduled_at)
        except ValueError as exc:
            raise ValueError(f"时间格式无效({scheduled_at}),请用 ISO 格式。") from exc

        sched = RecordingSchedule(
            room_id=room_id,
            scheduled_at=ts,
            enabled=True,
            recurrent=recurrent,
        )
        db.add(sched)
        db.flush()
        db.refresh(sched)
        return {"id": sched.id, "room_id": sched.room_id, "scheduled_at": sched.scheduled_at.isoformat()}


def delete_schedule(schedule_id: int) -> None:
    """删除一个录制预约。

    :param schedule_id: 预约 id。
    :raises ValueError: 预约不存在时。
    """
    with get_session() as db:
        sched = db.get(RecordingSchedule, schedule_id)
        if sched is None:
            raise ValueError(f"预约不存在: id={schedule_id}")
        db.delete(sched)


def toggle_schedule(schedule_id: int, enabled: bool) -> dict[str, Any]:
    """切换录制预约的启用/禁用。

    :param schedule_id: 预约 id。
    :param enabled: 新状态。
    :returns: 更新后的摘要。
    :raises ValueError: 预约不存在时。
    """
    with get_session() as db:
        sched = db.get(RecordingSchedule, schedule_id)
        if sched is None:
            raise ValueError(f"预约不存在: id={schedule_id}")
        sched.enabled = enabled
        db.add(sched)
        return {"id": sched.id, "enabled": sched.enabled}


def get_due_schedules() -> list[dict[str, Any]]:
    """返回到期且尚未触发的预约(供后台定时器调用)。

    :returns: 到期预约列表。
    """
    from app.db.models import utcnow

    now = utcnow()
    with get_session() as db:
        rows = db.exec(
            select(RecordingSchedule).where(
                RecordingSchedule.enabled == True,  # noqa: E712
                RecordingSchedule.triggered == False,  # noqa: E712
                RecordingSchedule.scheduled_at <= now,
            )
        ).all()
    return [
        {"id": r.id, "room_id": r.room_id, "scheduled_at": r.scheduled_at.isoformat(), "recurrent": r.recurrent}
        for r in rows
    ]


def mark_schedule_triggered(schedule_id: int) -> None:
    """标记预约已触发。

    :param schedule_id: 预约 id。
    """
    with get_session() as db:
        sched = db.get(RecordingSchedule, schedule_id)
        if sched is not None:
            sched.triggered = True
            db.add(sched)


# --------------------------------------------------------------------------- #
# 进度追踪(V0.1.2)
# --------------------------------------------------------------------------- #
