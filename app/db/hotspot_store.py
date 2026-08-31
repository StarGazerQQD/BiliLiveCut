"""HotspotEvent 的最小持久化原语。"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.db.entities import HotspotEvent, HotspotStatus, RecordingSession


def get_or_create_hotspot_event(
    db: Session,
    *,
    event_key: str,
    session_id: int,
    start_ts: datetime,
    peak_ts: datetime,
    end_ts: datetime,
    status: str = HotspotStatus.PROVISIONAL,
    heat_score: float = 0.0,
    clip_score: float = 0.0,
    semantic_confidence: float = 0.0,
    evidence_coverage: float = 0.0,
    title: str | None = None,
    summary: str | None = None,
    category: str | None = None,
    features_json: str | None = None,
    evidence_json: str | None = None,
    representative_danmaku_json: str | None = None,
    transcript_text: str | None = None,
) -> tuple[HotspotEvent, bool]:
    """按稳定业务键幂等获取或创建热点事件。

    :param db: 活动的 SQLModel 会话。
    :param event_key: 检测链生成的非空稳定业务键。
    :param session_id: 所属录制场次 ID。
    :param start_ts: 事件起点。
    :param peak_ts: 事件峰值时刻。
    :param end_ts: 事件终点。
    :param status: 初始生命周期状态。
    :returns: ``(event, created)``；并发冲突时复用已提交记录。
    :raises ValueError: 参数不合法或业务键跨场次冲突。
    """
    normalized_key = event_key.strip()
    if not normalized_key:
        raise ValueError("event_key 不能为空")
    if not start_ts <= peak_ts <= end_ts:
        raise ValueError("热点时间必须满足 start_ts <= peak_ts <= end_ts")
    if status not in HotspotStatus.ALL:
        raise ValueError(f"未知 HotspotEvent 状态: {status}")

    existing = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == normalized_key)).first()
    if existing is not None:
        _assert_same_session(existing, session_id)
        return existing, False

    if db.get(RecordingSession, session_id) is None:
        raise ValueError(f"录制场次不存在: session_id={session_id}")

    event = HotspotEvent(
        event_key=normalized_key,
        session_id=session_id,
        start_ts=start_ts,
        peak_ts=peak_ts,
        end_ts=end_ts,
        status=status,
        heat_score=heat_score,
        clip_score=clip_score,
        semantic_confidence=semantic_confidence,
        evidence_coverage=evidence_coverage,
        title=title,
        summary=summary,
        category=category,
        features_json=features_json,
        evidence_json=evidence_json,
        representative_danmaku_json=representative_danmaku_json,
        transcript_text=transcript_text,
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
        db.refresh(event)
        logger.info(
            "hotspot_created: hotspot_id={} session_id={} event_key={}",
            event.id,
            session_id,
            normalized_key[:32],
        )
        return event, True
    except IntegrityError:
        existing = db.exec(select(HotspotEvent).where(HotspotEvent.event_key == normalized_key)).first()
        if existing is None:
            raise
        _assert_same_session(existing, session_id)
        logger.info(
            "hotspot_conflict_resolved: hotspot_id={} event_key={}",
            existing.id,
            normalized_key[:32],
        )
        return existing, False


def get_hotspot_event(db: Session, hotspot_id: int) -> HotspotEvent | None:
    """按主键读取热点事件。"""
    return db.get(HotspotEvent, hotspot_id)


def list_session_hotspot_events(
    db: Session,
    session_id: int,
    *,
    statuses: Collection[str] | None = None,
) -> list[HotspotEvent]:
    """按峰值时间列出指定场次的热点事件。"""
    statement = select(HotspotEvent).where(HotspotEvent.session_id == session_id)
    if statuses is not None:
        normalized = set(statuses)
        unknown = normalized - HotspotStatus.ALL
        if unknown:
            raise ValueError(f"未知 HotspotEvent 状态: {sorted(unknown)}")
        if not normalized:
            return []
        statement = statement.where(HotspotEvent.status.in_(normalized))
    statement = statement.order_by(HotspotEvent.peak_ts, HotspotEvent.id)
    return list(db.exec(statement).all())


def _assert_same_session(event: HotspotEvent, session_id: int) -> None:
    """拒绝把同一稳定业务键静默复用到另一场录制。"""
    if event.session_id != session_id:
        raise ValueError(
            "event_key 已属于另一录制场次: "
            f"event_key={event.event_key!r} existing_session={event.session_id} requested_session={session_id}"
        )
