# ruff: noqa: D100
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from sqlalchemy import String, cast, func
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select, Subquery
from sqlmodel import Session, select

from app.analysis.source_policy import danmaku_view_from_metadata
from app.db.entities import AppSetting, Danmaku, LiveRoom, RecordingSession


class _OrderedWeightSum:
    def __init__(self) -> None:
        self.total = 0.0

    def step(self, value: float) -> None:
        self.total += float(value)

    def finalize(self) -> float:
        return self.total


@dataclass(frozen=True)
class DanmakuPage:
    """场次分页详情与整个筛选范围的计数、可用性分别保存。"""

    session_ids: tuple[int, ...]
    total_sessions: int
    total_comments: int
    available: bool
    counts: dict[int, tuple[int, float]]
    evidence: dict[int, dict[str, object]]


def _metadata_query(source: Subquery) -> Select[tuple[int, str | None, str | None, str | None]]:
    evidence = aliased(AppSetting)
    local = aliased(AppSetting)
    return (
        select(source.c.session_id, LiveRoom.platform, evidence.value, local.value)
        .select_from(source)
        .outerjoin(RecordingSession, RecordingSession.id == source.c.session_id)
        .outerjoin(LiveRoom, LiveRoom.id == RecordingSession.room_id)
        .outerjoin(evidence, evidence.key == "session_danmaku:" + cast(source.c.session_id, String))
        .outerjoin(local, local.key == "local_source:" + cast(source.c.session_id, String))
    )


def _available_in_scope(db: Session, source: Subquery) -> bool:
    # 全局可用性仍覆盖未显示的旧场次；同一查询流式读取，不借用逐场连接。
    rows = db.execute(_metadata_query(source)).yield_per(128)
    try:
        for session_id, platform, evidence, local in rows:
            view = danmaku_view_from_metadata(
                session_id, evidence_value=evidence, is_local=platform == "local", local_value=local
            )
            if view["available"]:
                return True
        return False
    finally:
        rows.close()


def _page_counts(db: Session, session_ids: tuple[int, ...]) -> dict[int, tuple[int, float]]:
    if not session_ids:
        return {}
    connection = db.connection().connection.dbapi_connection
    if not isinstance(connection, sqlite3.Connection):
        raise RuntimeError("弹幕精确热度聚合要求当前受支持的 SQLite 数据库")
    connection.create_aggregate("blc_ordered_weight_sum", 1, _OrderedWeightSum)
    # 保留原查询按主键读取时的逐值 float 左折叠，不能改用补偿 SUM 或分批合计。
    # 排序子查询兼容 SQLite 3.44 以前的版本，不使用新聚合 ORDER BY 语法。
    ordered = (
        select(Danmaku.session_id, Danmaku.value)
        .where(Danmaku.session_id.in_(session_ids))
        .order_by(Danmaku.id)
        .subquery()
    )
    statement = select(ordered.c.session_id, func.count(), func.blc_ordered_weight_sum(ordered.c.value)).group_by(
        ordered.c.session_id
    )
    return {sid: (count, float(weight)) for sid, count, weight in db.execute(statement)}


def danmaku_page(db: Session, *, session_id: int | None, limit: int, offset: int) -> DanmakuPage:
    """分页解析场次，SQL 内扫描历史并仅返回当前页的精确聚合结果。"""
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("弹幕场次分页范围无效")
    recordings = select(RecordingSession.id.label("session_id"))
    comments = select(Danmaku.session_id)
    comment_count = select(func.count()).select_from(Danmaku)
    if session_id is not None:
        recordings = recordings.where(RecordingSession.id == session_id)
        comments = comments.where(Danmaku.session_id == session_id)
        comment_count = comment_count.where(Danmaku.session_id == session_id)
    source = recordings.union(comments).subquery()
    total_sessions = int(db.scalar(select(func.count()).select_from(source)) or 0)
    total_comments = int(db.scalar(comment_count) or 0)
    ids = tuple(
        db.scalars(select(source.c.session_id).order_by(source.c.session_id.desc()).offset(offset).limit(limit))
    )
    selected = select(source.c.session_id).where(source.c.session_id.in_(ids)).subquery()
    evidence = {
        sid: danmaku_view_from_metadata(sid, evidence_value=value, is_local=platform == "local", local_value=local)
        for sid, platform, value, local in db.execute(_metadata_query(selected))
    }
    available = any(item["available"] for item in evidence.values()) or _available_in_scope(db, source)
    return DanmakuPage(ids, total_sessions, total_comments, available, _page_counts(db, ids), evidence)
