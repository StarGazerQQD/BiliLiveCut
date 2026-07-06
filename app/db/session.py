"""数据库引擎与会话管理。

提供:

* :data:`engine` —— 全局 SQLModel/SQLAlchemy 引擎(SQLite);
* :func:`init_db` —— 建表(幂等);
* :func:`get_session` —— 上下文管理器,自动提交/回滚/关闭。

SQLite 在多线程访问时需要 ``check_same_thread=False``;录制与下游任务
运行在不同线程/进程时由各自获取独立连接。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings

# 确保 SQLite 文件所在目录存在(sqlite:///./storage/blc.db)。
_db_url = settings.database_url
if _db_url.startswith("sqlite:///"):
    _db_path = Path(_db_url.replace("sqlite:///", "", 1))
    _db_path.parent.mkdir(parents=True, exist_ok=True)

# echo=False 避免污染日志;connect_args 仅对 SQLite 生效。
# timeout:遇到锁时等待秒数,缓解多任务(录制+分析+Web)并发写的 "database is locked"。
_connect_args = (
    {"check_same_thread": False, "timeout": 30} if _db_url.startswith("sqlite") else {}
)
engine = create_engine(_db_url, echo=False, connect_args=_connect_args)


if _db_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _conn_record) -> None:  # noqa: ANN001
        """为每个 SQLite 连接启用 WAL 等并发友好的 PRAGMA。

        * ``journal_mode=WAL``:读写并发更好(读不阻塞写);
        * ``busy_timeout``:遇锁时自动重试等待,减少报错;
        * ``synchronous=NORMAL``:在 WAL 下兼顾安全与性能。
        """
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # V0.1.11-alpha:启用外键约束(SQLite 默认关闭)。
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _migrate_add_columns() -> None:
    """为旧表追加新增列(缺失则添加,幂等安全)。"""
    _migrations = [
        # 格式: (表名, 列名, SQL 类型, 默认值)
        ("live_rooms", "schedule_enabled", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "auto_threshold_enabled", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "danmaku_sentiment_enabled", "INTEGER NOT NULL DEFAULT 0", None),
        ("recording_sessions", "last_reconnected_at", "TEXT", None),
        # V0.1.6: 自动化开关拆分。
        ("live_rooms", "auto_record", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "auto_analyze", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "auto_render", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "auto_approve", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "auto_upload", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "auto_approve_threshold", "REAL NOT NULL DEFAULT 0.82", None),
        ("live_rooms", "review_threshold", "REAL NOT NULL DEFAULT 0.50", None),
        # V0.1.6 P2: 房间配置。
        ("live_rooms", "room_config_json", "TEXT", None),
        # V0.1.8: 合集章节标题持久化。
        ("highlight_topics", "chapter_title", "TEXT", None),
        # V0.1.11-alpha: 任务并发控制与崩溃恢复。
        ("segment_tasks", "event_id", "INTEGER", None),
        ("segment_tasks", "failed_stage", "TEXT", None),
        ("segment_tasks", "claimed_by", "TEXT", None),
        ("segment_tasks", "claimed_at", "TEXT", None),
        ("segment_tasks", "heartbeat_at", "TEXT", None),
        # V0.1.11-alpha: 主题确认标记。
        ("highlight_topics", "confirmed_by_user", "INTEGER NOT NULL DEFAULT 0", None),
        # V0.1.12: ASR 多引擎流水线 — 辅助特征字段。
        ("transcripts", "auxiliary_json", "TEXT", None),
        # V0.1.12.2: ASR 追踪、复核与引擎信息。
        ("transcripts", "base_text", "TEXT", None),
        ("transcripts", "final_text", "TEXT", None),
        ("transcripts", "primary_backend", "TEXT", None),
        ("transcripts", "primary_model_id", "TEXT", None),
        ("transcripts", "primary_model_revision", "TEXT", None),
        ("transcripts", "review_backend", "TEXT", None),
        ("transcripts", "fallback_backend", "TEXT", None),
        ("transcripts", "review_triggered", "INTEGER NOT NULL DEFAULT 0", None),
        ("transcripts", "review_risk_score", "REAL", None),
        ("transcripts", "review_reasons", "TEXT", None),
        ("transcripts", "final_text_source", "TEXT", None),
        ("transcripts", "inference_duration", "REAL", None),
    ]
    with engine.connect() as conn:
        existing_lr = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(live_rooms)"
        ).fetchall()}
        existing_rs = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(recording_sessions)"
        ).fetchall()}
        existing_ht = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(highlight_topics)"
        ).fetchall()} if conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='highlight_topics'"
        ).fetchone() else set()
        existing_st = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(segment_tasks)"
        ).fetchall()} if conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='segment_tasks'"
        ).fetchone() else set()
        existing_tr = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(transcripts)"
        ).fetchall()}
        for table, col, sql_type, _ in _migrations:
            if table == "live_rooms":
                existing = existing_lr
            elif table == "recording_sessions":
                existing = existing_rs
            elif table == "highlight_topics":
                existing = existing_ht
            elif table == "segment_tasks":
                existing = existing_st
            elif table == "transcripts":
                existing = existing_tr
            else:
                existing = set()
            if col not in existing:
                try:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")
                    conn.commit()
                except Exception as exc:
                    logger.warning("迁移失败(可能列已存在或被并发修改): {}.{} {} ({})", table, col, sql_type, exc)

    # V0.1.6: 迁移旧 mode → 新 auto_* 开关(仅对尚未设置过开关的行生效)。
    _migrate_old_mode_to_switches()


def _migrate_old_mode_to_switches() -> None:
    """将旧的 ``manual/semi/auto`` 模式映射为新的独立自动化开关。

    - manual: 全部关闭,仅人工。
    - semi:   自动录制+分析+渲染;批准和上传人工。
    - auto:   全自动。
    """
    from app.db.models import LiveRoom, RoomMode

    with get_session() as db:
        from sqlmodel import select

        rooms = db.exec(select(LiveRoom)).all()
        updated = 0
        for room in rooms:
            # 仅在 5 个开关全为 False 时才做迁移(即首次升级)。
            any_auto_set = (
                room.auto_record or room.auto_analyze or room.auto_render
                or room.auto_approve or room.auto_upload
            )
            if any_auto_set:
                continue
            mode = room.mode
            if mode == RoomMode.MANUAL:
                room.auto_record = False
                room.auto_analyze = False
                room.auto_render = False
                room.auto_approve = False
                room.auto_upload = False
                updated += 1
                db.add(room)
            elif mode == RoomMode.SEMI:
                room.auto_record = True
                room.auto_analyze = True
                room.auto_render = True
                room.auto_approve = False
                room.auto_upload = False
                updated += 1
                db.add(room)
            elif mode == RoomMode.AUTO:
                room.auto_record = True
                room.auto_analyze = True
                room.auto_render = True
                room.auto_approve = True
                room.auto_upload = False  # 上传始终需人工确认
                updated += 1
                db.add(room)
        if updated:
            logger.info("已迁移 {} 个房间的旧 mode→新 auto_* 开关。", updated)


def init_db() -> None:
    """创建所有表(若不存在),执行迁移。

    1. SQLModel.metadata.create_all — 新建表
    2. _migrate_add_columns — 为旧表追加缺失列 (V0.1.2 ~ V0.1.12.2 历史迁移, 幂等)
    3. run_migrations — V0.1.12.2 起版本化迁移 (含备份 + 数据修复)
    """
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)

    # 历史列迁移 (幂等安全)
    _migrate_add_columns()

    # V0.1.12.2: 版本化迁移
    from app.db.migrate import run_migrations
    run_migrations()


@contextmanager
def get_session() -> Iterator[Session]:
    """提供一个自动管理生命周期的数据库会话。

    成功则提交,异常则回滚,最终关闭连接。

    使用 ``expire_on_commit=False``,使提交后仍可安全读取已加载对象的属性
    (例如把新建对象返回给调用方或回调),避免 ``DetachedInstanceError``。

    :yields: 一个可用的 :class:`~sqlmodel.Session`。
    """
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
