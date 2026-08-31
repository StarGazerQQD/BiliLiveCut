"""0.1.17.x Schema v4 到 0.1.18 Schema v5 的单向迁移。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from loguru import logger
from sqlalchemy.engine import Engine

from app.db.entities import HotspotEvent


class SchemaMigrationError(RuntimeError):
    """数据库迁移无法安全完成。"""


def migration_backup_path(db_path: Path) -> Path:
    """返回 v4 迁移备份的稳定路径。"""
    return db_path.with_name(f"{db_path.name}.schema-v4.bak")


def migrate_v4_to_v5(
    *,
    engine: Engine,
    db_path: Path,
    legacy_app_version: str,
    legacy_fingerprint: str,
    target_app_version: str,
    target_fingerprint: str,
) -> Path:
    """在备份保护下事务性新增 ``hotspot_events`` 并更新元数据。

    迁移失败时先由数据库事务回滚，再对 SQLite 可能自动提交的 DDL 执行
    精确补偿回滚；迁移前备份始终保留。已有且通过完整性及来源版本校验的
    备份会被复用，因此进程重启后可安全重试。

    :returns: 保留的迁移前备份路径。
    :raises SchemaMigrationError: 备份或数据库元数据不符合迁移前提。
    """
    backup_path = _ensure_v4_backup(db_path, legacy_app_version, legacy_fingerprint)
    try:
        with engine.begin() as connection:
            HotspotEvent.__table__.create(bind=connection, checkfirst=False)
            result = connection.exec_driver_sql(
                """
                UPDATE schema_meta
                   SET schema_version = ?, schema_fingerprint = ?, app_version = ?
                 WHERE id = 1
                   AND schema_version = 4
                   AND schema_fingerprint = ?
                   AND app_version = ?
                """,
                (5, target_fingerprint, target_app_version, legacy_fingerprint, legacy_app_version),
            )
            if result.rowcount != 1:
                raise SchemaMigrationError("迁移期间 schema_meta 已变化，拒绝提交")
    except Exception as exc:
        try:
            _compensate_failed_migration(
                engine=engine,
                legacy_app_version=legacy_app_version,
                legacy_fingerprint=legacy_fingerprint,
                target_app_version=target_app_version,
                target_fingerprint=target_fingerprint,
            )
        except Exception as rollback_exc:
            raise SchemaMigrationError(
                f"Schema v4 -> v5 迁移及补偿回滚均失败；请停止程序并使用迁移前备份恢复: {backup_path}"
            ) from rollback_exc
        raise SchemaMigrationError(
            f"Schema v4 -> v5 迁移失败；数据库已补偿回滚，迁移前备份保留于 {backup_path}"
        ) from exc

    logger.info(
        "Schema v4 -> v5 迁移完成: database={} backup={}",
        db_path,
        backup_path,
    )
    return backup_path


def _compensate_failed_migration(
    *,
    engine: Engine,
    legacy_app_version: str,
    legacy_fingerprint: str,
    target_app_version: str,
    target_fingerprint: str,
) -> None:
    """撤销 SQLite 可能在事务外提交的新增表，并恢复精确 v4 元数据。"""
    with engine.begin() as connection:
        meta = connection.exec_driver_sql(
            "SELECT schema_version, schema_fingerprint, app_version FROM schema_meta WHERE id = 1"
        ).fetchone()
        allowed_meta = {
            (4, legacy_fingerprint, legacy_app_version),
            (5, target_fingerprint, target_app_version),
        }
        if meta not in allowed_meta:
            raise SchemaMigrationError(f"补偿回滚前 schema_meta 出现并发变化: {meta!r}")
        connection.exec_driver_sql("DROP TABLE IF EXISTS hotspot_events")
        connection.exec_driver_sql(
            """
            UPDATE schema_meta
               SET schema_version = 4, schema_fingerprint = ?, app_version = ?
             WHERE id = 1
            """,
            (legacy_fingerprint, legacy_app_version),
        )

    with engine.connect() as connection:
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").fetchone()
        hotspot_table = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hotspot_events'"
        ).fetchone()
    if integrity is None or integrity[0] != "ok" or hotspot_table is not None:
        raise SchemaMigrationError(f"补偿回滚校验失败: integrity={integrity!r} hotspot_table={hotspot_table!r}")


def _ensure_v4_backup(db_path: Path, legacy_app_version: str, legacy_fingerprint: str) -> Path:
    """创建或校验可复用的 WAL 一致性迁移备份。"""
    backup_path = migration_backup_path(db_path)
    if backup_path.exists():
        _validate_v4_backup(backup_path, legacy_app_version, legacy_fingerprint)
        logger.info("复用已校验的 Schema v4 迁移备份: {}", backup_path)
        return backup_path

    temp_path = backup_path.with_name(f"{backup_path.name}.tmp")
    try:
        temp_path.unlink(missing_ok=True)
        source_uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as source,
            closing(sqlite3.connect(temp_path)) as destination,
        ):
            source.backup(destination)
            destination.commit()
        _validate_v4_backup(temp_path, legacy_app_version, legacy_fingerprint)
        temp_path.replace(backup_path)
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise SchemaMigrationError(f"创建 Schema v4 迁移备份失败: {backup_path}") from exc
    return backup_path


def _validate_v4_backup(
    backup_path: Path,
    legacy_app_version: str,
    legacy_fingerprint: str,
) -> None:
    """验证备份完整性、来源版本及未迁移状态。"""
    try:
        with closing(sqlite3.connect(f"file:{backup_path.resolve().as_posix()}?mode=ro", uri=True)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise SchemaMigrationError(f"迁移备份完整性校验失败: {integrity}")
            meta = connection.execute(
                "SELECT schema_version, schema_fingerprint, app_version FROM schema_meta WHERE id = 1"
            ).fetchone()
            hotspot_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='hotspot_events'"
            ).fetchone()
    except sqlite3.Error as exc:
        raise SchemaMigrationError(f"无法读取迁移备份: {backup_path}") from exc

    expected_meta = (4, legacy_fingerprint, legacy_app_version)
    if meta != expected_meta:
        raise SchemaMigrationError(
            f"已有迁移备份并非当前数据库对应的 v4 版本: backup_meta={meta!r} expected={expected_meta!r}"
        )
    if hotspot_table is not None:
        raise SchemaMigrationError("v4 迁移备份中意外存在 hotspot_events")
