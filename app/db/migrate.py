"""版本化数据库迁移系统 (V0.1.12.2)。

特性:
- schema_version 表记录当前版本
- migration_history 表记录每次迁移
- 每个迁移有唯一版本号
- 按顺序执行
- 迁移前自动备份 SQLite 数据库
- 单个迁移使用事务
- 失败时中止, 不允许半升级状态
- 启动时校验 schema
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlmodel import Field, Session, SQLModel, select

from app.core.config import settings
from app.db.session import engine, get_session

# 当前目标 schema 版本
TARGET_SCHEMA_VERSION = 1


# ── 迁移历史表 ───────────────────────────────────────────────

class SchemaVersion(SQLModel, table=True):
    """schema 版本记录 (``schema_version``): 单行表记录当前版本。"""

    __tablename__ = "schema_version"

    id: int = Field(default=1, primary_key=True)
    version: int = Field(default=0, description="当前 schema 版本号")
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checksum: str | None = Field(default=None, description="schema 校验和")


class MigrationHistory(SQLModel, table=True):
    """迁移历史 (``migration_history``): 记录每次已执行的迁移。"""

    __tablename__ = "migration_history"

    id: int | None = Field(default=None, primary_key=True)
    version: int = Field(index=True, description="迁移版本号")
    name: str = Field(description="迁移名称")
    applied_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: int | None = Field(default=None)
    success: bool = Field(default=True)
    error: str | None = Field(default=None)


@dataclass
class Migration:
    """一次迁移的定义。"""

    version: int
    name: str
    sql: str
    data_migration: callable | None = None  # Python 函数, 接收 Session


# ── 迁移注册表 ───────────────────────────────────────────────

def _migrate_v1_old_data(db: Session) -> int:
    """V1 数据迁移: 修复旧版本中 Candidate ID 被错误写入 Event ID 字段。

    Returns: 修复的记录总数。
    """
    from app.db.models import (
        ClipVariant,
        HighlightEvent,
        HighlightTopic,
    )

    fixed = 0
    unmappable = 0

    # 1. 建立 Candidate ID → Event ID 映射
    events = db.exec(select(HighlightEvent)).all()
    cid_to_eid: dict[int, int] = {}
    for e in events:
        if e.candidate_id is not None:
            cid_to_eid[e.candidate_id] = e.id

    # 2. 修复 ClipVariant 中 event_id 实际保存 Candidate ID 的情况
    clips = db.exec(select(ClipVariant)).all()
    for cv in clips:
        eid = cv.event_id
        # 如果 event_id 数字恰好在 candidate_id 映射中存在, 说明是旧数据
        if eid in cid_to_eid and cid_to_eid[eid] != eid:
            logger.info(
                "修复 ClipVariant id={}: event_id {} → {} (真实 Event ID)",
                cv.id, eid, cid_to_eid[eid],
            )
            cv.event_id = cid_to_eid[eid]
            db.add(cv)
            fixed += 1
        # 如果 event_id 不在任何 Event 中, 尝试通过 candidate_id 映射
        elif eid not in {e.id for e in events} and cv.candidate_id is not None:
            real_eid = cid_to_eid.get(cv.candidate_id)
            if real_eid is not None:
                logger.info(
                    "修复 ClipVariant id={}: event_id {} (疑似 Candidate ID) → {}",
                    cv.id, eid, real_eid,
                )
                cv.event_id = real_eid
                db.add(cv)
                fixed += 1
            else:
                logger.warning(
                    "无法映射 ClipVariant id={} candidate_id={} event_id={}, 保留原值",
                    cv.id, cv.candidate_id, eid,
                )
                unmappable += 1

    # 3. 修复 HighlightTopic (TopicMembership) 中的 event_id
    topics = db.exec(select(HighlightTopic)).all()
    for ht in topics:
        eid = ht.event_id
        if eid in cid_to_eid and cid_to_eid[eid] != eid:
            logger.info(
                "修复 HighlightTopic id={}: event_id {} → {}",
                ht.id, eid, cid_to_eid[eid],
            )
            ht.event_id = cid_to_eid[eid]
            db.add(ht)
            fixed += 1

    if unmappable > 0:
        logger.warning("无法映射 {} 条记录, 请手动检查", unmappable)

    return fixed


# ── 迁移列表 (按版本顺序) ────────────────────────────────────

_MIGRATIONS: list[Migration] = [
    Migration(
        version=1,
        name="V0.1.12.2: ASR追踪字段 + 数据关系修复",
        sql="""
        -- schema_version 和 migration_history 由 SQLModel 自动创建

        -- HighlightEvent UNIQUE(candidate_id)
        CREATE UNIQUE INDEX IF NOT EXISTS idx_highlight_events_candidate_id
            ON highlight_events(candidate_id);

        -- 旧数据迁移在 Python 函数中执行
        """,
        data_migration=_migrate_v1_old_data,
    ),
]


# ── 迁移执行 ──────────────────────────────────────────────────

def _get_db_path() -> Path:
    db_url = settings.database_url
    if db_url.startswith("sqlite:///"):
        return Path(db_url.replace("sqlite:///", "", 1))
    return Path("storage/blc.db")


def _backup_database() -> Path | None:
    """备份当前数据库。"""
    db_path = _get_db_path()
    if not db_path.exists():
        return None
    backup_path = db_path.with_name(f"{db_path.stem}_v{int(time.time())}.bak")
    try:
        shutil.copy2(db_path, backup_path)
        logger.info("数据库已备份至: {}", backup_path)
        return backup_path
    except OSError as exc:
        logger.error("数据库备份失败: {}", exc)
        raise


def _current_version() -> int:
    """读取当前 schema 版本 (表不存在时返回 0)。"""
    try:
        with get_session() as db:
            sv = db.get(SchemaVersion, 1)
            return sv.version if sv else 0
    except Exception:
        return 0


def run_migrations() -> bool:
    """执行所有待运行的迁移。

    流程:
    1. 创建 schema_version / migration_history 表 (如果不存在)
    2. 读取当前版本
    3. 对每个未执行的迁移: 备份 → 执行 SQL → 执行数据迁移 → 记录
    4. 任何迁移失败时中止

    :returns: True 表示所有迁移成功; False 表示需要人工干预。
    """
    # 确保基础表存在
    from app.db import models  # noqa: F401
    SQLModel.metadata.create_all(engine, tables=[
        SchemaVersion.__table__,
        MigrationHistory.__table__,
    ])

    current = _current_version()
    logger.info("当前 schema 版本: {}, 目标: {}", current, TARGET_SCHEMA_VERSION)

    if current >= TARGET_SCHEMA_VERSION:
        logger.info("schema 已是最新, 无需迁移")
        return True

    pending = [m for m in _MIGRATIONS if m.version > current]
    if not pending:
        return True

    # 迁移前备份
    _backup_database()

    for migration in pending:
        logger.info("执行迁移 v{}: {}", migration.version, migration.name)
        t0 = time.time()

        try:
            with engine.connect() as conn:
                # 在事务中执行 SQL
                trans = conn.begin()
                try:
                    if migration.sql.strip():
                        for stmt in migration.sql.split(";"):
                            stmt = stmt.strip()
                            if stmt and not stmt.startswith("--"):
                                conn.exec_driver_sql(stmt)
                    trans.commit()
                except Exception:
                    trans.rollback()
                    raise

            # 执行数据迁移 (Python)
            if migration.data_migration:
                with get_session() as db:
                    fixed = migration.data_migration(db)
                    logger.info("数据迁移完成: 修复 {} 条记录", fixed)

            # 记录迁移历史
            elapsed = int((time.time() - t0) * 1000)
            with get_session() as db:
                # 更新版本
                sv = db.get(SchemaVersion, 1)
                if sv is None:
                    sv = SchemaVersion(id=1, version=migration.version)
                    db.add(sv)
                else:
                    sv.version = migration.version
                    sv.applied_at = datetime.now(UTC)
                    db.add(sv)

                # 记录历史
                db.add(MigrationHistory(
                    version=migration.version,
                    name=migration.name,
                    duration_ms=elapsed,
                    success=True,
                ))

            logger.info("迁移 v{} 完成, 耗时 {}ms", migration.version, elapsed)

        except Exception as exc:
            logger.error("迁移 v{} 失败: {}", migration.version, exc)
            # 记录失败
            try:
                with get_session() as db:
                    db.add(MigrationHistory(
                        version=migration.version,
                        name=migration.name,
                        success=False,
                        error=str(exc)[:500],
                    ))
            except Exception:
                pass
            return False

    # 最终校验
    final = _current_version()
    if final != TARGET_SCHEMA_VERSION:
        logger.error("迁移未完成: 当前版本 {} ≠ 目标版本 {}", final, TARGET_SCHEMA_VERSION)
        return False

    logger.info("全部迁移完成: v{} → v{}", current, TARGET_SCHEMA_VERSION)
    return True


def check_schema() -> bool:
    """启动时校验 schema 版本。

    :returns: True 表示版本匹配, 应用可安全启动。
    """
    current = _current_version()
    if current < TARGET_SCHEMA_VERSION:
        logger.error(
            "schema 版本不匹配: 当前 {} < 目标 {}。请先运行迁移。",
            current, TARGET_SCHEMA_VERSION,
        )
        return False
    return True
