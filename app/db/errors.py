# ruff: noqa: D100
from __future__ import annotations

import sqlite3

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout


def temporary_database_error(error: SQLAlchemyError) -> bool:
    """只重试连接池等待和 SQLite 忙锁，结构、权限及数据错误原样暴露。"""
    if isinstance(error, PoolTimeout):
        return True
    if not isinstance(error, OperationalError) or not isinstance(error.orig, sqlite3.OperationalError):
        return False
    code = getattr(error.orig, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return str(error.orig) in {"database is locked", "database table is locked", "database schema is locked"}
