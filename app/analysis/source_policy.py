"""区分导入媒体与直播的时间轴、弹幕证据语义。"""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session

from app.db.entities import AppSetting, LiveRoom, RecordingSession
from app.db.session import get_session


@dataclass(frozen=True, slots=True)
class DanmakuCoverageSnapshot:
    """一次分析使用的覆盖快照，不跨调用缓存采集状态。"""

    lag_s: float
    local_has_comments: bool | None
    starts: tuple[datetime, ...]
    furthest_ends: tuple[datetime, ...]

    def contains(self, start: datetime | None = None, end: datetime | None = None) -> bool:
        """要求至少一个原始连接区间完整覆盖窗口，不拼接相邻区间。"""
        if self.local_has_comments is not None:
            return self.local_has_comments
        if start is None or end is None:
            return bool(self.starts)
        left = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
        right = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
        index = bisect_right(self.starts, left) - 1
        return index >= 0 and self.furthest_ends[index] >= right


def load_danmaku_coverage(db: Session, session_id: int) -> DanmakuCoverageSnapshot:
    """从当前数据库会话一次读取延迟与完整覆盖，保留本地导入的证据语义。"""
    from app.recording.danmaku import read_evidence

    evidence = read_evidence(db, session_id)
    lag_s = evidence.lag_s if evidence is not None else 0.0
    if is_local_session(session_id, db):
        row = db.get(AppSetting, f"local_source:{session_id}")
        available = row is not None and json.loads(row.value).get("has_comments") is True
        return DanmakuCoverageSnapshot(lag_s, available, (), ())
    if evidence is None:
        return DanmakuCoverageSnapshot(lag_s, None, (), ())
    starts: list[datetime] = []
    ends: list[datetime] = []
    for interval in sorted(evidence.intervals, key=lambda item: item.start):
        starts.append(interval.start)
        end = interval.end or evidence.confirmed_until or interval.start
        ends.append(max(end, ends[-1]) if ends else end)
    return DanmakuCoverageSnapshot(lag_s, None, tuple(starts), tuple(ends))


def is_local_session(session_id: int, db: Session | None = None) -> bool:
    """由持久来源平台判断本地场次，避免依赖可丢失的任务快照。"""
    if db is None:
        with get_session() as connection:
            return is_local_session(session_id, connection)
    session = db.get(RecordingSession, session_id)
    room = db.get(LiveRoom, session.room_id) if session else None
    return room is not None and room.platform == "local"


def session_danmaku_lag_s(session_id: int) -> float:
    """本地及外部 UTC 事件无需 Bili 接收延迟；新场次使用当场固定值。"""
    from app.recording.danmaku import read_evidence

    with get_session() as db:
        evidence = read_evidence(db, session_id)
        if evidence is not None:
            return evidence.lag_s
        return 0.0


def session_has_danmaku(
    session_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
) -> bool:
    """区分缺失和有效零事件；直播窗口必须有持久化的完整连接覆盖。"""
    from app.recording.danmaku import read_evidence

    with get_session() as db:
        if is_local_session(session_id, db):
            row = db.get(AppSetting, f"local_source:{session_id}")
            return row is not None and json.loads(row.value).get("has_comments") is True
        evidence = read_evidence(db, session_id)
        if evidence is not None:
            if start is None or end is None:
                return bool(evidence.intervals)
            left = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
            right = end.replace(tzinfo=UTC) if end.tzinfo is None else end.astimezone(UTC)
            return any(
                interval.start <= left and (interval.end or evidence.confirmed_until or interval.start) >= right
                for interval in evidence.intervals
            )
        return False


def session_danmaku_view(session_id: int) -> dict[str, object]:
    """用于界面与评分插件的证据说明，计数和覆盖状态分别展示。"""
    from app.recording.danmaku import read_evidence

    with get_session() as db:
        evidence = read_evidence(db, session_id)
        if evidence is not None:
            return {
                "state": evidence.status.value,
                "interrupted": evidence.interrupted,
                "available": bool(evidence.intervals),
                "ended_at": evidence.ended_at.isoformat() if evidence.ended_at else None,
                "archive_error": evidence.archive_error,
            }
        if is_local_session(session_id, db):
            available = session_has_danmaku(session_id)
            return {"state": "available" if available else "unsupported", "available": available, "ended_at": None}
        return {"state": "unavailable", "available": False, "ended_at": None}
