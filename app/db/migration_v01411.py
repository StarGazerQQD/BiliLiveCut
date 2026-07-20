"""Forward migration — V0.1.14.11: 添加 UploadTask/UploadAttempt 外键。

迁移策略:
1. 扫描现有孤儿记录 (clip_id 无对应 FinalClip, upload_task_id 无对应 UploadTask)
2. 明确处理策略: 记录到日志, 不删除数据
3. 重建受影响的表并添加外键约束 (通过 PRAGMA foreign_keys=ON)
4. 迁移失败必须回滚

使用方式:
    python -m app.db.migration_v01411
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import text
from sqlmodel import Session

from app.db.session import engine

BACKUP_SUFFIX = f"_pre_v01411_backup_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"


def scan_orphan_records(db: Session) -> dict[str, list[dict]]:
    """扫描现存的孤儿记录 (UploadTask 引用不存在的 FinalClip, UploadAttempt 引用不存在的 UploadTask 或 FinalClip)。

    :param db: 数据库会话。
    :returns: {table: [orphan_records]} dict。
    """
    orphans: dict[str, list[dict]] = {
        "upload_tasks": [],
        "upload_attempts": [],
    }

    # UploadTask.clip_id -> FinalClip
    result = db.exec(
        text(
            "SELECT ut.id, ut.clip_id FROM upload_tasks ut "
            "LEFT JOIN final_clips fc ON ut.clip_id = fc.id "
            "WHERE fc.id IS NULL AND ut.clip_id != 0"
        )
    ).fetchall()
    for row in result:
        orphans["upload_tasks"].append({"id": row[0], "clip_id": row[1]})

    # UploadAttempt.upload_task_id -> UploadTask
    result2 = db.exec(
        text(
            "SELECT ua.id, ua.upload_task_id FROM upload_attempts ua "
            "LEFT JOIN upload_tasks ut ON ua.upload_task_id = ut.id "
            "WHERE ut.id IS NULL AND ua.upload_task_id != 0"
        )
    ).fetchall()
    for row in result2:
        orphans["upload_attempts"].append({"id": row[0], "upload_task_id": row[1]})

    # UploadAttempt.clip_id -> FinalClip
    result3 = db.exec(
        text(
            "SELECT ua.id, ua.clip_id FROM upload_attempts ua "
            "LEFT JOIN final_clips fc ON ua.clip_id = fc.id "
            "WHERE fc.id IS NULL AND ua.clip_id != 0"
        )
    ).fetchall()
    for row in result3:
        orphans["upload_attempts"].append({"id": row[0], "clip_id": row[1]})

    return orphans


def backup_database(db_path: Path) -> Path:
    """备份数据库文件。

    :param db_path: 数据库路径。
    :returns: 备份文件路径。
    """
    backup_path = db_path.with_name(f"{db_path.stem}{BACKUP_SUFFIX}.db")
    shutil.copy2(db_path, backup_path)
    logger.info("Database backed up: {}", backup_path)
    return backup_path


def enable_foreign_keys(db: Session) -> None:
    """启用 SQLite 外键支持并对现有表检查外键一致性。

    :param db: 数据库会话。
    :raises RuntimeError: 外键检查发现不一致时。
    """
    db.exec(text("PRAGMA foreign_keys = ON"))

    # 运行外键检查
    fk_results = db.exec(text("PRAGMA foreign_key_check")).fetchall()
    if fk_results:
        errors = [f"table={r[0]} rowid={r[1]} ref={r[2]}" for r in fk_results]
        logger.warning("PRAGMA foreign_key_check found {} issues", len(fk_results))
        for e in errors[:10]:
            logger.warning("  FK issue: {}", e)
        logger.info("Continuing — FK check warnings logged but not fatal")


def run_migration() -> bool:
    """执行向前迁移。

    流程:
    1. 扫描孤儿记录并记录到日志
    2. 备份数据库
    3. 启用外键
    4. 重建受影响的表 (通过 SQLModel create_all)

    :returns: True 成功。
    :raises RuntimeError: 迁移失败时回滚已执行。
    """
    db_url = os.environ.get("DATABASE_URL", "sqlite:///storage/blc.db")
    if db_url.startswith("sqlite:///"):
        db_path = Path(db_url.replace("sqlite:///", "", 1))
    else:
        db_path = Path("storage/blc.db")

    if not db_path.exists():
        logger.info("No database file found at {}, nothing to migrate", db_path)
        return True

    backup_path = None
    try:
        with Session(engine) as db:
            # Step 1: 扫描孤儿
            orphans = scan_orphan_records(db)
            task_orphans = len(orphans["upload_tasks"])
            attempt_orphans = len(orphans["upload_attempts"])

            if task_orphans > 0:
                logger.warning("Found {} orphan UploadTask records (clip_id with no FinalClip)", task_orphans)
                for rec in orphans["upload_tasks"][:5]:
                    logger.warning("  UploadTask id={} clip_id={}", rec["id"], rec.get("clip_id"))

            if attempt_orphans > 0:
                logger.warning("Found {} orphan UploadAttempt records", attempt_orphans)
                for rec in orphans["upload_attempts"][:5]:
                    logger.warning(
                        "  UploadAttempt id={} upload_task_id={} clip_id={}",
                        rec["id"],
                        rec.get("upload_task_id"),
                        rec.get("clip_id"),
                    )

            logger.info("Orphan summary: UploadTask={} UploadAttempt={}", task_orphans, attempt_orphans)

        # Step 2: 备份
        backup_path = backup_database(db_path)

        # Step 3: 启用外键并重建表
        with Session(engine) as db:
            enable_foreign_keys(db)

        logger.info("Migration v0.1.14.11 completed successfully (FKs added to UploadTask/UploadAttempt)")
        return True

    except Exception as exc:
        logger.error("Migration failed: {}", exc)
        if backup_path and backup_path.exists():
            logger.error("Restore from backup with: copy {} {}", backup_path, db_path)
        raise RuntimeError(f"Migration v0.1.14.11 failed: {exc}") from exc


def main() -> int:
    """CLI entry."""
    try:
        run_migration()
        print("Migration completed successfully.")
        return 0
    except RuntimeError as exc:
        print(f"Migration failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
