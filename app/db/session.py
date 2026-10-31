"""数据库引擎与会话管理。

提供:

* :data:`engine` —— 全局 SQLModel/SQLAlchemy 引擎(SQLite);
* :func:`init_db` —— 建表或校验当前 Schema;
* :func:`get_session` —— 上下文管理器,自动提交/回滚/关闭。

SQLite 在多线程访问时需要 ``check_same_thread=False``;录制与下游任务
运行在不同线程/进程时由各自获取独立连接。

Schema 创建与严格校验由 :mod:`app.db.schema` 统一管理。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4

from loguru import logger
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from app.core.config import settings
from app.db.telemetry import install_database_telemetry, observe_session_telemetry

# 确保 SQLite 文件所在目录存在(sqlite:///./storage/blc.db)。
_db_url = settings.database_url
if _db_url.startswith("sqlite:///"):
    _db_path = Path(_db_url.replace("sqlite:///", "", 1))
    _db_path.parent.mkdir(parents=True, exist_ok=True)

# echo=False 避免污染日志;connect_args 仅对 SQLite 生效。
# timeout:遇到锁时等待秒数,缓解多任务(录制+分析+Web)并发写的 "database is locked"。
_connect_args = {"check_same_thread": False, "timeout": 30} if _db_url.startswith("sqlite") else {}
engine = create_engine(_db_url, echo=False, connect_args=_connect_args)
install_database_telemetry(engine)
_initialization_lock = Lock()
_database_log_lock = RLock()
_validated_database: tuple[Engine, str] | None = None

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
    """初始化数据库：创建新数据库或校验当前 Schema。

    流程:
    1. 数据库文件不存在 → app.db.schema.assure_schema() 创建全部表
    2. 数据库文件存在   → 校验 schema_meta / version / fingerprint / 关键约束
    3. 不兼容 → RuntimeError, 阻止启动

    :raises RuntimeError: Schema 不兼容或校验失败时。
    """
    from app.db import entities  # noqa: F401
    from app.db.schema import assure_schema

    global _validated_database
    with _initialization_lock:
        # 等待正在提交的日志，再撤销旧代次；校验失败时保持禁止写库。
        with _database_log_lock:
            _validated_database = None
        assure_schema()
        from app.db.runtime_logs import ensure_runtime_log_indexes

        ensure_runtime_log_indexes(engine)
        from app.core.configuration import reload_configuration

        reload_configuration()
        with _database_log_lock:
            _validated_database = (engine, uuid4().hex)
    logger.info("数据库初始化完成: {}", settings.database_url)


def database_log_generation() -> str | None:
    """返回当前已通过完整初始化的日志代次，未通过时禁止入队。"""
    validated = _validated_database
    return validated[1] if validated is not None and validated[0] is engine else None


@contextmanager
def get_database_log_session(generation: str | None) -> Iterator[Session | None]:
    """仅在原数据库及校验代次仍有效时提交日志，避免旧队列跨库写入。"""
    with _database_log_lock:
        if generation is None or generation != database_log_generation():
            yield None
            return
        with get_session() as session:
            yield session


@contextmanager
def get_session(*, operation: str = "database", task_id: int | None = None) -> Iterator[Session]:
    """提供一个自动管理生命周期的数据库会话。

    成功则提交,异常则回滚,最终关闭连接。

    使用 ``expire_on_commit=False``,使提交后仍可安全读取已加载对象的属性
    (例如把新建对象返回给调用方或回调),避免 ``DetachedInstanceError``。

    :yields: 一个可用的 :class:`~sqlmodel.Session`。
    """
    from app.db.notifications import observe_commits

    session = Session(engine, expire_on_commit=False)
    observe_commits(session)
    observe_session_telemetry(session, operation=operation, task_id=task_id)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
