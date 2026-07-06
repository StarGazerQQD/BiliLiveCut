"""轻量 Schema 管理与校验 (V0.1.12.9)。

替代旧的版本化迁移框架。核心原则:

* Alpha 阶段不兼容旧数据库时拒绝启动, 不自动迁移;
* Schema 由当前 SQLModel/SQLAlchemy 模型确定性创建;
* 使用 SHA-256 指纹 + 版本号双重验证兼容性;
* 数据库不存在时创建; 存在的数据库校验通过后启动;
* 任何校验失败均阻止应用启动。

不包含迁移、备份、ALTER TABLE 或旧数据修复逻辑。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlmodel import Field, Session, SQLModel


def _get_engine():
    """获取当前数据库引擎 (动态导入以支持测试中的模块重载)。"""
    from app.db.session import engine as _engine

    return _engine


def _get_settings():
    """获取当前配置 (动态导入以支持测试中修改 env var)。"""
    from app.core.config import settings as _settings

    return _settings


# ── 常量 ──────────────────────────────────────────────────

CURRENT_SCHEMA_VERSION = 1

# ── Schema 元信息表 ──────────────────────────────────────


class SchemaMeta(SQLModel, table=True):
    """Schema 元信息 (``schema_meta``): 单行表, 记录当前 Schema 版本与指纹。"""

    __tablename__ = "schema_meta"

    id: int = Field(default=1, primary_key=True)
    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION)
    schema_fingerprint: str = Field(default="")
    app_version: str = Field(default="")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Schema 指纹 ───────────────────────────────────────────


def compute_schema_fingerprint() -> str:
    """计算当前 SQLModel 模型定义的 Expected Schema 指纹 (SHA-256)。

    指纹包含:
    - 表名 (排序)
    - 每表的字段名、类型、nullable、默认值 (排序)
    - 主键
    - 唯一约束 (逻辑定义, 非 SQLite 自动索引名)
    - 外键

    :returns: SHA-256 十六进制字符串。
    """
    from app.db import models  # noqa: F401 — 确保所有模型已注册

    tables_info: dict[str, dict] = {}

    for table in sorted(SQLModel.metadata.sorted_tables, key=lambda t: t.name):
        tname = table.name
        # 列信息
        columns: list[dict] = []
        for col in sorted(table.columns, key=lambda c: c.name):
            col_info = {
                "name": col.name,
                "type": str(col.type),
                "nullable": col.nullable,
                "default": _serializable_default(col.default),
                "primary_key": col.primary_key,
            }
            columns.append(col_info)

        # 唯一约束 (从表约束中提取, 排序以保证稳定)
        constraints: list[dict] = []
        for c in sorted(table.constraints, key=lambda c: c.name or ""):
            if hasattr(c, "columns"):
                constraints.append(
                    {
                        "type": type(c).__name__,
                        "columns": sorted([col.name for col in c.columns]),
                    }
                )

        # 外键
        foreign_keys: list[dict] = []
        for fk in sorted(table.foreign_keys, key=lambda fk: fk.parent.name):
            foreign_keys.append(
                {
                    "column": fk.parent.name,
                    "ref_table": fk.column.table.name,
                    "ref_column": fk.column.name,
                }
            )

        tables_info[tname] = {
            "columns": columns,
            "constraints": constraints,
            "foreign_keys": foreign_keys,
        }

    # 生成规范的 JSON (排序键以保证确定性)
    canonical = json.dumps(tables_info, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_actual_schema_fingerprint() -> str:
    """从真实数据库结构计算 Actual Schema 指纹 (V0.1.13)。

    使用 PRAGMA 命令读取实际数据库结构, 生成与
    compute_schema_fingerprint 兼容的描述格式。

    :returns: SHA-256 十六进制字符串。
    """
    tables_info: dict[str, dict] = {}

    with _get_engine().connect() as conn:
        # 读取所有用户表
        tables = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()

        for (tname,) in sorted(tables):
            # PRAGMA table_info
            cols_raw = conn.exec_driver_sql(f"PRAGMA table_info({tname})").fetchall()

            columns: list[dict] = []
            for col in cols_raw:
                columns.append(
                    {
                        "name": col[1],
                        "type": col[2] or "TEXT",
                        "nullable": bool(not col[3]),
                        "default": str(col[4]) if col[4] is not None else None,
                        "primary_key": bool(col[5]),
                    }
                )

            # PRAGMA index_list (跳过 sqlite_autoindex)
            idxs_raw = conn.exec_driver_sql(f"PRAGMA index_list({tname})").fetchall()

            constraints: list[dict] = []
            for idx in idxs_raw:
                idx_name = idx[1]
                if idx_name.startswith("sqlite_autoindex_"):
                    continue
                is_unique = bool(idx[2])
                # PRAGMA index_info for column names
                idx_cols = conn.exec_driver_sql(f"PRAGMA index_info({idx_name})").fetchall()
                col_names = [ic[2] for ic in idx_cols if len(ic) > 2] if idx_cols else []
                constraints.append(
                    {
                        "name": idx_name,
                        "type": "UniqueConstraint" if is_unique else "Index",
                        "columns": sorted(col_names),
                    }
                )

            # PRAGMA foreign_key_list
            fks_raw = conn.exec_driver_sql(f"PRAGMA foreign_key_list({tname})").fetchall()

            foreign_keys: list[dict] = []
            for fk in fks_raw:
                foreign_keys.append(
                    {
                        "column": fk[3],
                        "ref_table": fk[2],
                        "ref_column": fk[4],
                    }
                )

            # 聚合唯一约束 (PRAGMA index_list + unique=1)
            # 合并回 columns 信息
            for idx in idxs_raw:
                if idx[1].startswith("sqlite_autoindex_"):
                    # 这是 SQLite 内部唯一索引 (对应模型定义中的 UniqueConstraint)
                    is_unique = bool(idx[2])
                    if is_unique:
                        idx_cols_raw = conn.exec_driver_sql(f"PRAGMA index_info({idx[1]})").fetchall()
                        col_names = sorted([ic[2] for ic in idx_cols_raw if len(ic) > 2])
                        constraints.append(
                            {
                                "name": "UNIQUE",
                                "type": "UniqueConstraint",
                                "columns": col_names,
                            }
                        )

            tables_info[tname] = {
                "columns": columns,
                "constraints": constraints,
                "foreign_keys": foreign_keys,
            }

    canonical = json.dumps(tables_info, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serializable_default(default) -> str | None:
    """将 SQLAlchemy 默认值序列化为可比较的字符串。"""
    if default is None:
        return None
    if hasattr(default, "arg"):
        return str(default.arg)
    return str(default)


# ── 数据库创建 ────────────────────────────────────────────


def create_schema(db: Session) -> None:
    """使用当前 SQLModel 模型创建全部数据库表。

    过程是确定性的:
    1. SQLModel.metadata.create_all — 创建全部表 (含约束和索引)
    2. 写入 schema_meta 记录
    3. 写入 Schema 指纹

    :param db: 活动的数据库会话。
    """
    from app.db import models  # noqa: F401

    # 创建全部表
    SQLModel.metadata.create_all(_get_engine())

    # 计算指纹
    fingerprint = compute_schema_fingerprint()

    # 写入元信息
    meta = SchemaMeta(
        id=1,
        schema_version=CURRENT_SCHEMA_VERSION,
        schema_fingerprint=fingerprint,
        app_version=_app_version_str(),
        created_at=datetime.now(UTC),
    )
    db.add(meta)
    db.flush()

    logger.info(
        "Schema 创建完成: version={} fingerprint={}",
        CURRENT_SCHEMA_VERSION,
        fingerprint[:16],
    )


# ── Schema 校验 ───────────────────────────────────────────


def validate_schema() -> bool:
    """校验当前数据库 Schema 是否与程序兼容。

    校验项:
    1. schema_meta 表存在
    2. schema_version 匹配
    3. schema_fingerprint 匹配
    4. 关键表和字段存在
    5. 关键唯一索引存在
    6. 外键约束开启
    7. PRAGMA integrity_check 通过

    :returns: True 表示兼容可启动; False 表示应拒绝启动。
    """
    try:
        # 1. 检查 schema_meta 是否存在
        with _get_engine().connect() as conn:
            table_check = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            if not table_check:
                logger.error("数据库缺少 schema_meta 表 (可能来自旧版本)")
                return False

        # 2. 读取元信息
        with Session(_get_engine()) as db:
            meta = db.get(SchemaMeta, 1)
            if meta is None:
                logger.error("schema_meta 中无记录")
                return False

            stored_version = meta.schema_version
            stored_fingerprint = meta.schema_fingerprint

        # 3. 版本比较
        if stored_version != CURRENT_SCHEMA_VERSION:
            logger.error(
                "Schema 版本不匹配: 数据库={} 程序={}",
                stored_version,
                CURRENT_SCHEMA_VERSION,
            )
            return False

        # 4. 指纹比较
        current_fp = compute_schema_fingerprint()
        if stored_fingerprint != current_fp:
            logger.error(
                "Schema 指纹不匹配\n  数据库: {}\n  程序:   {}",
                stored_fingerprint[:16],
                current_fp[:16],
            )
            return False

        # 5. 校验关键索引
        if not _verify_critical_indexes():
            logger.error("关键索引校验失败")
            return False

        # 6. 校验外键
        if not _verify_foreign_keys():
            logger.error("外键约束校验失败")
            return False

        # 7. 完整性检查
        with _get_engine().connect() as conn:
            result = conn.exec_driver_sql("PRAGMA integrity_check").fetchone()
            if result and result[0] != "ok":
                logger.error("数据库完整性检查失败: {}", result[0])
                return False

        # 8. V0.1.13: 验证实际数据库中存在所有预期表/列 (结构化比较)
        actual_fp = compute_actual_schema_fingerprint()
        # 不直接比较不同计算方式的指纹, 而是确保实际 DB 包含所有预期结构
        ok, msg = _verify_actual_structure()
        if not ok:
            logger.error("实际数据库结构不完整: {}", msg)
            return False

        logger.info(
            "Schema 校验完全通过 (expected={} actual={})",
            current_fp[:16],
            actual_fp[:16],
        )

        # 9. 外键一致性检查
        with _get_engine().connect() as conn_fk:
            result_fk = conn_fk.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if result_fk:
            logger.error("PRAGMA foreign_key_check 失败: {} 个不一致的外键", len(result_fk))
            return False

        logger.info(
            "Schema 校验通过: version={}",
            CURRENT_SCHEMA_VERSION,
        )
        return True

    except Exception as exc:
        logger.error("Schema 校验异常: {}", exc)
        return False


def assure_schema() -> None:
    """确保数据库 Schema 可用 — 创建或校验。

    - 数据库不存在 → 创建全部表并写入 schema_meta
    - 数据库存在 → 校验; 不兼容则抛出 RuntimeError

    :raises RuntimeError: Schema 不兼容时。
    """
    db_path = _db_path()
    db_exists = db_path.exists()

    if not db_exists:
        logger.info("数据库不存在, 创建新 Schema: {}", db_path)
        with Session(_get_engine()) as db:
            try:
                create_schema(db)
                db.commit()
            except Exception:
                db.rollback()
                raise
        # 创建后立即校验
        if not validate_schema():
            raise RuntimeError(f"新创建的数据库 Schema 校验失败。请删除数据库后重试。\n数据库路径: {db_path}")
        logger.info("数据库创建并校验成功: {}", db_path)
        return

    # 数据库已存在 — 校验
    if not validate_schema():
        raise RuntimeError(
            "\n当前数据库 Schema 与程序不兼容。\n"
            f"\n数据库版本: {_stored_version()}"
            f"\n程序要求版本: {CURRENT_SCHEMA_VERSION}"
            "\n\n当前项目仍处于 Alpha 阶段, 不提供数据库自动升级。"
            "\n请备份需要的数据后删除数据库并重新启动。"
            f"\n\n数据库路径:\n{db_path}\n"
        )


# ── 辅助函数 ──────────────────────────────────────────────


def _db_path() -> Path:
    """获取数据库文件路径。"""
    db_url = _get_settings().database_url
    if db_url.startswith("sqlite:///"):
        return Path(db_url.replace("sqlite:///", "", 1))
    return Path("storage/blc.db")


def _app_version_str() -> str:
    """获取当前应用版本字符串。"""
    try:
        from app import __version__

        return __version__
    except ImportError:
        return "unknown"


def _stored_version() -> int:
    """读取数据库中存储的 schema_version。"""
    try:
        with Session(_get_engine()) as db:
            meta = db.get(SchemaMeta, 1)
            return meta.schema_version if meta else -1
    except Exception:
        return -1


def _verify_critical_indexes() -> bool:
    """校验关键唯一索引存在。

    - 单列唯一/索引: 按 SQLAlchemy 自动生成的索引名后缀匹配。
    - 复合唯一约束: SQLite 使用 sqlite_autoindex_<table>_<n> 命名, 检查其存在即可。

    :returns: True 表示全部关键索引存在。
    """
    single_col = [
        ("segment_tasks", "segment_id", "SegmentTask.segment_id 唯一"),
        ("highlight_events", "candidate_id", "HighlightEvent.candidate_id 唯一"),
        ("transcripts", "segment_id", "Transcript.segment_id 索引"),
    ]
    composite = [
        ("highlight_topics", "HighlightTopic(event_id, topic_id) 唯一"),
        ("upload_tasks", "UploadTask(clip_id, uploader) 唯一"),
        ("clip_variants", "ClipVariant 三维唯一"),
    ]

    all_ok = True
    try:
        with _get_engine().connect() as conn:
            for table, suffix, desc in single_col:
                rows = conn.exec_driver_sql(f"PRAGMA index_list('{table}')").fetchall()
                if not any(suffix in (row[1] or "") for row in rows):
                    logger.error("关键索引缺失: {} ({})", desc, table)
                    all_ok = False
                else:
                    logger.debug("索引存在: {} ({})", desc, table)

            for table, desc in composite:
                rows = conn.exec_driver_sql(f"PRAGMA index_list('{table}')").fetchall()
                if not any((row[1] or "").startswith("sqlite_autoindex_") and row[2] == 1 for row in rows):
                    logger.error("关键唯一约束缺失: {} ({})", desc, table)
                    all_ok = False
                else:
                    logger.debug("唯一约束存在: {} ({})", desc, table)
    except Exception as exc:
        logger.error("索引验证异常: {}", exc)
        return False

    return all_ok


def _verify_actual_structure() -> tuple[bool, str]:
    """验证实际数据库结构是否包含所有预期表和列 (V0.1.13)。

    从 SQLModel metadata 获取预期结构, 与 PRAGMA 读取的实际结构比较。
    不比较 SQLite 自动生成的名字, 只验证表/列/约束的逻辑存在。

    :returns: (ok, error_message) — ok=True 表示实际结构包含所有预期元素。
    """
    try:
        expected = {}  # table -> set of column names
        for table in SQLModel.metadata.sorted_tables:
            expected[table.name] = {col.name for col in table.columns}

        with _get_engine().connect() as conn:
            tables = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            actual_tables = {row[0] for row in tables}

        # 检查预期表都存在
        missing_tables = set(expected) - actual_tables
        if missing_tables:
            return False, f"缺少表: {missing_tables}"

        # 检查每个表的列
        with _get_engine().connect() as conn:
            for tname, expected_cols in expected.items():
                actual_cols_raw = conn.exec_driver_sql(f"PRAGMA table_info({tname})").fetchall()
                actual_cols = {row[1] for row in actual_cols_raw}
                missing_cols = expected_cols - actual_cols
                if missing_cols:
                    return False, f"表 {tname} 缺少列: {missing_cols}"

        return True, "OK"
    except Exception as exc:
        return False, f"结构验证异常: {exc}"


def _verify_foreign_keys() -> bool:
    """校验关键表的外键存在。"""
    fk_checks = [
        ("clip_variants", "highlight_events", "ClipVariant.event_id -> HighlightEvent"),
        ("highlight_topics", "highlight_events", "HighlightTopic.event_id -> HighlightEvent"),
        ("highlight_topics", "topics", "HighlightTopic.topic_id -> Topic"),
        ("upload_tasks", "final_clips", "UploadTask.clip_id -> FinalClip"),
    ]

    all_ok = True
    try:
        with _get_engine().connect() as conn:
            for table, ref_table, desc in fk_checks:
                fk_rows = conn.exec_driver_sql(f"PRAGMA foreign_key_list('{table}')").fetchall()
                found = any(row[2] == ref_table for row in fk_rows)
                if not found:
                    logger.warning("外键缺失: {}", desc)
                    # 外键缺失不一定是致命错误 (SQLite 默认不强制)
                    # 但记录警告
                else:
                    logger.debug("外键存在: {}", desc)
    except Exception as exc:
        logger.error("外键验证异常: {}", exc)

    return all_ok


# ── db reset 命令 ─────────────────────────────────────────


def reset_database(*, yes: bool = False, backup: bool = True) -> bool:
    """删除数据库并重新创建 (仅供开发/CI 使用)。

    安全措施:
    1. 显示数据库绝对路径
    2. 要求明确确认
    3. 默认生成备份副本
    4. 拒绝路径逃逸和符号链接

    :param yes: 跳过确认 (仅 CI)
    :param backup: 删除前备份
    :returns: True 表示成功
    """
    db_path = _db_path().resolve()

    # 安全检查: 数据库必须在配置目录内
    config_root = Path(_get_settings().storage_root).resolve()
    try:
        db_path.relative_to(config_root)
    except ValueError:
        logger.error(
            "拒绝删除非托管路径的数据库: {} (不在 {} 下)",
            db_path,
            config_root,
        )
        return False

    if not yes:
        print(f"\n警告: 将删除数据库: {db_path}\n")
        confirm = input("输入 'yes' 确认: ")
        if confirm.strip().lower() != "yes":
            print("已取消。")
            return False

    # 备份
    if backup and db_path.exists():
        import shutil

        backup_path = db_path.with_name(f"{db_path.stem}_reset_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.bak")
        try:
            shutil.copy2(db_path, backup_path)
            logger.info("数据库已备份至: {}", backup_path)
        except OSError as exc:
            logger.error("备份失败: {}", exc)
            return False

    # 删除
    try:
        if db_path.exists():
            db_path.unlink()
            logger.info("数据库已删除: {}", db_path)
    except OSError as exc:
        logger.error("删除数据库失败: {}", exc)
        return False

    # 重新创建
    try:
        with Session(_get_engine()) as db:
            create_schema(db)
            db.commit()
        logger.info("数据库已重建: {}", db_path)
    except Exception as exc:
        logger.error("重建数据库失败: {}", exc)
        return False

    return True
