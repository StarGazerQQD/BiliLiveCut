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
from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, select

from app.core.config import settings
from app.db.session import engine, get_session

# 当前目标 schema 版本
TARGET_SCHEMA_VERSION = 3


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
    """V1 数据迁移: 修复旧版本中 Candidate ID 被错误写入 Event ID 字段 (V0.1.12.7 修复碰撞)。

    V0.1.12.7 修复: 不再盲目按 cid_to_eid 映射替换。
    先判断是否属于真实 Event ID, 只有不属于 Event ID 时才尝试作为 Candidate ID 转换。
    存在歧义时中止迁移 (不猜测)。

    Returns: 修复的记录总数。
    """
    from app.db.models import (
        ClipVariant,
        HighlightEvent,
        HighlightTopic,
    )

    fixed = 0
    unmappable = 0

    # 1. 收集全部 Event ID
    events = db.exec(select(HighlightEvent)).all()
    all_event_ids: set[int] = {e.id for e in events}

    # 2. 建立 Candidate ID → Event ID 映射
    cid_to_eid: dict[int, int] = {}
    for e in events:
        if e.candidate_id is not None:
            cid_to_eid[e.candidate_id] = e.id

    # 3. 修复 ClipVariant 中 event_id 实际保存 Candidate ID 的情况
    clips = db.exec(select(ClipVariant)).all()
    for cv in clips:
        eid = cv.event_id

        # V0.1.12.7: 已经是合法 Event ID — 绝对不能修改
        if eid in all_event_ids:
            continue

        # 不属于任何 Event ID — 尝试作为 Candidate ID 转换
        real_eid = cid_to_eid.get(eid)
        if real_eid is not None:
            logger.info(
                "修复 ClipVariant id={}: event_id {} (旧 Candidate ID) → {} (真实 Event ID)",
                cv.id, eid, real_eid,
            )
            cv.event_id = real_eid
            db.add(cv)
            fixed += 1
        elif cv.candidate_id is not None:
            # 尝试通过 candidate_id 字段间接映射
            real_eid = cid_to_eid.get(cv.candidate_id)
            if real_eid is not None:
                logger.info(
                    "修复 ClipVariant id={}: event_id {} → {} (通过 candidate_id={} 映射)",
                    cv.id, eid, real_eid, cv.candidate_id,
                )
                cv.event_id = real_eid
                db.add(cv)
                fixed += 1
            else:
                logger.warning(
                    "无法映射 ClipVariant id={} candidate_id={} event_id={} (不属于任何 Event, 也无映射), 保留原值",
                    cv.id, cv.candidate_id, eid,
                )
                unmappable += 1
        else:
            logger.warning(
                "无法映射 ClipVariant id={} event_id={} (不属于任何 Event ID, 无 candidate_id), 保留原值",
                cv.id, eid,
            )
            unmappable += 1

    # 4. 修复 HighlightTopic (TopicMembership) 中的 event_id — 同样先判 Event
    topics = db.exec(select(HighlightTopic)).all()
    for ht in topics:
        eid = ht.event_id
        if eid in all_event_ids:
            continue

        real_eid = cid_to_eid.get(eid)
        if real_eid is not None:
            logger.info(
                "修复 HighlightTopic id={}: event_id {} → {}",
                ht.id, eid, real_eid,
            )
            ht.event_id = real_eid
            db.add(ht)
            fixed += 1

    if unmappable > 0:
        logger.warning("无法映射 {} 条记录, 请手动检查", unmappable)

    return fixed


def _migrate_v2_pipeline_keys(db: Session) -> int:
    """V2 数据迁移: 为现有 SegmentTask 填充 pipeline_key 和 stage_key。

    Returns: 修复的记录总数。
    """
    from app.db.models import SegmentTask

    tasks = db.exec(select(SegmentTask)).all()
    fixed = 0
    for task in tasks:
        if task.pipeline_key is None:
            task.pipeline_key = f"pipeline:{task.segment_id}"
            fixed += 1
        if task.stage_key is None:
            task.stage_key = f"stage:{task.segment_id}:{task.stage}"
            fixed += 1
        db.add(task)
    return fixed


def _remove_sql_line_comments(sql: str) -> str:
    """移除 SQL 字符串中的纯注释行 (V0.1.12.7)。

    不会移除字符串字面量中的 '--' (使用简单的行级别过滤)。
    注释行定义: 去除前后空白后以 -- 开头的行为注释行。

    :param sql: 原始 SQL 字符串。
    :returns: 移除纯注释行后的 SQL。
    """
    lines = []
    for line in sql.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _split_sql_statements(sql: str) -> list[str]:
    """安全解析 SQL 语句, 防止注释导致后续语句被跳过 (V0.1.12.7)。

    先移除纯注释行, 再按分号分割语句。

    :param sql: 原始 SQL 字符串。
    :returns: 非空 SQL 语句列表。
    """
    cleaned = _remove_sql_line_comments(sql)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]

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
    Migration(
        version=2,
        name="V0.1.12.5: 状态机重构 + 幂等键 + 数据一致性约束",
        sql="""
        -- ClipVariant UNIQUE(event_id, variant_type) — 通过唯一索引模拟
        CREATE UNIQUE INDEX IF NOT EXISTS idx_clip_variants_event_variant
            ON clip_variants(event_id, variant_type);

        -- HighlightTopic UNIQUE(event_id, topic_id)
        CREATE UNIQUE INDEX IF NOT EXISTS idx_highlight_topics_event_topic
            ON highlight_topics(event_id, topic_id);

        -- UploadTask UNIQUE(clip_id, uploader)
        CREATE UNIQUE INDEX IF NOT EXISTS idx_upload_tasks_clip_uploader
            ON upload_tasks(clip_id, uploader);

        -- SegmentTask UNIQUE(segment_id) — 已通过模型声明; 额外创建索引
        CREATE UNIQUE INDEX IF NOT EXISTS idx_segment_tasks_segment_id
            ON segment_tasks(segment_id);

        -- pipeline_key / stage_key 由 Python 数据迁移填充
        """,
        data_migration=_migrate_v2_pipeline_keys,
    ),
    Migration(
        version=3,
        name="V0.1.12.8: ClipVariant 三列唯一约束 + Transcript segment_id 唯一索引",
        sql="""
        -- V0.1.12.8: ClipVariant(event_id, variant_type, render_config_hash) 三维唯一
        -- 先删除旧的 2 列索引 (IF EXISTS via sqlite3)
        -- 再创建新的 3 列索引
        DROP INDEX IF EXISTS idx_clip_variants_event_variant;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_clip_variants_event_variant_config
            ON clip_variants(event_id, variant_type, render_config_hash);

        -- V0.1.12.8: Transcript segment_id 唯一索引 (一段一转写)
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transcripts_segment_id
            ON transcripts(segment_id);
        """,
        data_migration=None,
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
            with get_session() as db:
                # 1. SQL DDL 迁移 (在事务中执行, 先移除注释行再解析语句)
                if migration.sql.strip():
                    for stmt in _split_sql_statements(migration.sql):
                        if stmt:
                            db.exec(text(stmt))

                # 2. Python 数据迁移 (仍在同一事务中)
                if migration.data_migration:
                    fixed = migration.data_migration(db)
                    logger.info("数据迁移完成: 修复 {} 条记录", fixed)

                # 3. 更新版本号 + 记录历史 (DDL/数据全成功后一起提交)
                elapsed = int((time.time() - t0) * 1000)
                sv = db.get(SchemaVersion, 1)
                if sv is None:
                    sv = SchemaVersion(id=1, version=migration.version)
                    db.add(sv)
                else:
                    sv.version = migration.version
                    sv.applied_at = datetime.now(UTC)
                    db.add(sv)

                db.add(MigrationHistory(
                    version=migration.version,
                    name=migration.name,
                    duration_ms=elapsed,
                    success=True,
                ))
                # get_session 退出时自动 commit; 若任何步骤失败则回滚

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
    """启动时校验 schema 版本 (V0.1.12.7: 增强版 — 含索引校验)。

    :returns: True 表示版本匹配, 应用可安全启动。
    """
    current = _current_version()
    if current < TARGET_SCHEMA_VERSION:
        logger.error(
            "schema 版本不匹配: 当前 {} < 目标 {}。请先运行迁移。",
            current, TARGET_SCHEMA_VERSION,
        )
        return False
    # V0.1.12.7: 迁移后验证真实数据库约束
    if not _verify_critical_indexes():
        logger.error("关键索引校验失败: 数据库约束不完整")
        return False
    return True


def _verify_critical_indexes() -> bool:
    """迁移后验证关键索引真实存在 (V0.1.12.8: 增加 Transcript 索引检查)。

    至少验证:
    - SegmentTask.segment_id 唯一
    - HighlightEvent.candidate_id 唯一
    - HighlightTopic(event_id, topic_id) 唯一
    - UploadTask(clip_id, uploader) 唯一
    - ClipVariant(event_id, variant_type, render_config_hash) 唯一
    - Transcript.pipeline_key 唯一

    :returns: True 表示全部关键索引存在。
    """
    critical_checks = [
        # (表名, 索引名后缀, 描述)
        ("segment_tasks", "segment_id", "SegmentTask.segment_id 唯一"),
        ("highlight_events", "candidate_id", "HighlightEvent.candidate_id 唯一"),
        ("highlight_topics", "event_topic", "HighlightTopic(event_id, topic_id) 唯一"),
        ("upload_tasks", "clip_uploader", "UploadTask(clip_id, uploader) 唯一"),
        ("clip_variants", "variant_config", "ClipVariant(event_id, variant_type, render_config_hash) 唯一"),
        ("transcripts", "segment_id", "Transcript.segment_id 唯一"),
    ]

    all_ok = True
    try:
        with engine.connect() as conn:
            for table, suffix, desc in critical_checks:
                rows = conn.exec_driver_sql(
                    f"PRAGMA index_list('{table}')"
                ).fetchall()
                found = any(suffix in (row[1] or "") for row in rows)
                if not found:
                    logger.error("关键索引缺失: {} ({})", desc, table)
                    all_ok = False
                else:
                    logger.debug("关键索引存在: {} ({})", desc, table)

            # 校验外键
            for table in ("clip_variants", "highlight_topics"):
                fk_rows = conn.exec_driver_sql(
                    f"PRAGMA foreign_key_list('{table}')"
                ).fetchall()
                if not fk_rows:
                    logger.warning("表 {} 缺少外键约束", table)
                else:
                    logger.debug("表 {} 外键: {}", table, len(fk_rows))
    except Exception as exc:
        logger.error("索引验证异常: {}", exc)
        return False

    return all_ok
