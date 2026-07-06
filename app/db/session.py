"""数据库引擎与会话管理 (V0.1.12.9)。

提供:

* :data:`engine` —— 全局 SQLModel/SQLAlchemy 引擎(SQLite);
* :func:`init_db` —— 建表或校验 Schema (V0.1.12.9: 使用 Schema 系统, 移除迁移框架);
* :func:`get_session` —— 上下文管理器,自动提交/回滚/关闭。

SQLite 在多线程访问时需要 ``check_same_thread=False``;录制与下游任务
运行在不同线程/进程时由各自获取独立连接。

V0.1.12.9: 移除 _migrate_add_columns、_migrate_old_mode_to_switches
和所有迁移相关逻辑。Schema 创建由 app.db.schema 统一管理。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import event
from sqlmodel import Session, create_engine

from app.core.config import settings

# 确保 SQLite 文件所在目录存在(sqlite:///./storage/blc.db)。
_db_url = settings.database_url
if _db_url.startswith("sqlite:///"):
    _db_path = Path(_db_url.replace("sqlite:///", "", 1))
    _db_path.parent.mkdir(parents=True, exist_ok=True)

# echo=False 避免污染日志;connect_args 仅对 SQLite 生效。
# timeout:遇到锁时等待秒数,缓解多任务(录制+分析+Web)并发写的 "database is locked"。
_connect_args = {"check_same_thread": False, "timeout": 30} if _db_url.startswith("sqlite") else {}
engine = create_engine(_db_url, echo=False, connect_args=_connect_args)


if _db_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _conn_record) -> None:  # noqa: ANN001
        """为每个 SQLite 连接启用 WAL 等并发友好的 PRAGMA。

        * ``journal_mode=WAL``:读写并发更好(读不阻塞写);
        * ``busy_timeout``:遇锁时自动重试等待,减少报错;
        * ``synchronous=NORMAL``:在 WAL 下兼顾安全与性能;
        * ``foreign_keys=ON``:强制外键约束。
        """
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db() -> None:
    """初始化数据库 — 创建新数据库或校验已有数据库 (V0.1.12.9)。

    流程:
    1. 数据库文件不存在 → app.db.schema.assure_schema() 创建全部表
    2. 数据库文件存在   → 校验 schema_meta / version / fingerprint / 关键约束
    3. 不兼容 → RuntimeError, 阻止启动
    4. 不执行任何迁移、ALTER TABLE 或旧数据修复

    :raises RuntimeError: Schema 不兼容或校验失败时。
    """
    from app.db import models  # noqa: F401
    from app.db.schema import assure_schema

    assure_schema()
    logger.info("数据库初始化完成: {}", settings.database_url)


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
