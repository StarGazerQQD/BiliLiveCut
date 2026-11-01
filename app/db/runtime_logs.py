# ruff: noqa: D100
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import Index, delete
from sqlalchemy.engine import Engine
from sqlmodel import select

from app.db.entities import SystemLog

RUNTIME_LOG_CONTEXT = json.dumps({"source": "runtime-diagnostic-v1"}, separators=(",", ":"))
_RUNTIME_FILTER = (SystemLog.context_json == RUNTIME_LOG_CONTEXT) & (SystemLog.level == "WARNING")
_INDEXES = (
    Index("ix_system_logs_created_at_id", SystemLog.created_at, SystemLog.id),
    Index("ix_system_logs_level_created_at_id", SystemLog.level, SystemLog.created_at, SystemLog.id),
    Index(
        "ix_system_logs_runtime_retention",
        SystemLog.created_at,
        SystemLog.id,
        sqlite_where=_RUNTIME_FILTER,
        postgresql_where=_RUNTIME_FILTER,
    ),
)


def ensure_runtime_log_indexes(engine: Engine) -> None:
    """在完整 Schema 校验后补建普通查询索引，不改变列或唯一性契约。"""
    for index in _INDEXES:
        index.create(engine, checkfirst=True)


def prune_runtime_logs(
    *, limit: int = 500, retention_days: int = 14, max_rows: int = 20_000, generation: str | None = None
) -> int:
    """有界清理新标记的普通诊断，保留历史行、业务审计与 ERROR 故障记录。"""
    from app.db.session import database_log_generation, get_database_log_session

    if not 1 <= limit <= 1000 or retention_days < 1 or max_rows < 1:
        raise ValueError("日志清理容量与保留时间无效")
    expected = generation if generation is not None else database_log_generation()
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention_days)
    with get_database_log_session(expected) as db:
        if db is None:
            return 0
        expired = db.exec(
            select(SystemLog.id)
            .where(_RUNTIME_FILTER, SystemLog.created_at < cutoff)
            .order_by(SystemLog.created_at, SystemLog.id)
            .limit(limit)
        ).all()
        overflow = db.exec(
            select(SystemLog.id)
            .where(_RUNTIME_FILTER)
            .order_by(SystemLog.created_at.desc(), SystemLog.id.desc())
            .offset(max_rows)
            .limit(limit)
        ).all()
    identities = list(dict.fromkeys([*expired, *overflow]))[:limit]
    if not identities:
        return 0
    with get_database_log_session(expected) as db:
        if db is None:
            return 0
        result = db.exec(delete(SystemLog).where(_RUNTIME_FILTER, SystemLog.id.in_(identities)))
        return result.rowcount
