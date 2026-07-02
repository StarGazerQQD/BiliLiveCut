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
        cursor.close()


def _migrate_add_columns() -> None:
    """为旧表追加 V0.1.2 新增列(缺失则添加,幂等安全)。"""
    _migrations = [
        # 格式: (表名, 列名, SQL 类型, 默认值)
        ("live_rooms", "schedule_enabled", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "auto_threshold_enabled", "INTEGER NOT NULL DEFAULT 0", None),
        ("live_rooms", "danmaku_sentiment_enabled", "INTEGER NOT NULL DEFAULT 0", None),
        ("recording_sessions", "last_reconnected_at", "TEXT", None),
    ]
    with engine.connect() as conn:
        existing_lr = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(live_rooms)"
        ).fetchall()}
        existing_rs = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(recording_sessions)"
        ).fetchall()}
        for table, col, sql_type, _ in _migrations:
            existing = existing_lr if table == "live_rooms" else existing_rs
            if col not in existing:
                try:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")
                    conn.commit()
                except Exception:
                    pass  # 列已存在或其他兼容性问题,安全忽略


def init_db() -> None:
    """创建所有表(若不存在),并对旧表执行轻量迁移(追加缺失列)。

    导入模型模块以触发表注册,然后调用 ``SQLModel.metadata.create_all``。
    可安全重复调用。
    """
    # 必须先导入 models,SQLModel.metadata 才知道有哪些表。
    from app.db import models  # noqa: F401

    SQLModel.metadata.create_all(engine)

    # 轻量迁移:为已存在的 live_rooms 表补充 V0.1.2 新增列(SQLite 的 ALTER 语义)。
    _migrate_add_columns()


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
