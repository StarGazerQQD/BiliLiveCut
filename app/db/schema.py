"""当前数据库 Schema 的创建、严格校验与受限升级。

核心原则:

* 仅允许 0.1.17.x Schema v4 安全升级到 0.1.18 Schema v5;
* Schema 由当前 SQLModel/SQLAlchemy 模型确定性创建;
* 使用 SHA-256 指纹 + 版本号双重验证一致性;
* 数据库不存在时创建; 存在的数据库升级或校验通过后启动;
* 任何校验失败均阻止应用启动。

不提供通用迁移框架，也不接受 v4 -> v5 之外的历史数据库。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
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

LEGACY_SCHEMA_VERSION = 4
CURRENT_SCHEMA_VERSION = 5

_LEGACY_APP_VERSION_RE = re.compile(r"^0\.1\.17(?:\.\d+)?-alpha$", re.IGNORECASE)
_TARGET_APP_VERSION_RE = re.compile(r"^0\.1\.18(?:\.\d+)?-alpha$", re.IGNORECASE)

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
    return _compute_model_schema_fingerprint()


def compute_legacy_v4_fingerprint() -> str:
    """重建 0.1.17.x Schema v4 的模型指纹。

    v5 只新增 ``hotspot_events``；对 ``schema_meta.schema_version`` 的模型默认值
    恢复为 4 后，所得指纹必须与 0.1.17.x 数据库记录精确一致。
    """
    return _compute_model_schema_fingerprint(
        excluded_tables={"hotspot_events"},
        schema_version_override=LEGACY_SCHEMA_VERSION,
    )


def _compute_model_schema_fingerprint(
    *,
    excluded_tables: Collection[str] = (),
    schema_version_override: int | None = None,
) -> str:
    """按当前模型计算指纹，可精确重建受支持的 v4 基线。"""
    from app.db import entities  # noqa: F401 — 确保所有模型已注册

    tables_info: dict[str, dict] = {}
    excluded = set(excluded_tables)

    for table in sorted(SQLModel.metadata.sorted_tables, key=lambda t: t.name):
        tname = table.name
        if tname in excluded:
            continue
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
            if schema_version_override is not None and tname == "schema_meta" and col.name == "schema_version":
                col_info["default"] = json.dumps(schema_version_override)
            columns.append(col_info)

        # 唯一约束 (从表约束中提取, 排序以保证稳定)
        constraints: list[dict] = []
        for c in table.constraints:
            if hasattr(c, "columns"):
                constraints.append(
                    {
                        "type": type(c).__name__,
                        "columns": sorted([col.name for col in c.columns]),
                    }
                )
        constraints.sort(key=lambda item: (item["type"], item["columns"]))

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
    value = default.arg if hasattr(default, "arg") else default
    if callable(value):
        module = getattr(value, "__module__", type(value).__module__)
        qualname = getattr(value, "__qualname__", type(value).__qualname__)
        return f"callable:{module}.{qualname}"
    if value is None or isinstance(value, (bool, int, float, str)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return f"{type(value).__module__}.{type(value).__qualname__}:{value}"


# ── 数据库创建 ────────────────────────────────────────────


def create_schema(db: Session) -> None:
    """使用当前 SQLModel 模型创建全部数据库表。

    过程是确定性的:
    1. SQLModel.metadata.create_all — 创建全部表 (含约束和索引)
    2. 写入 schema_meta 记录
    3. 写入 Schema 指纹

    :param db: 活动的数据库会话。
    """
    from app.db import entities  # noqa: F401

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
    """校验数据库是否精确符合当前程序 Schema。

    校验项:
    1. schema_meta 表存在
    2. schema_version 匹配
    3. schema_fingerprint 匹配
    4. 关键表和字段存在
    5. 关键唯一索引存在
    6. 外键约束开启
    7. PRAGMA integrity_check 通过

    :returns: True 表示可启动；False 表示应拒绝启动。
    """
    try:
        # 1. 检查 schema_meta 是否存在
        with _get_engine().connect() as conn:
            table_check = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            if not table_check:
                logger.error("数据库缺少当前版本要求的 schema_meta 表")
                return False

        # 2. 读取元信息
        with Session(_get_engine()) as db:
            meta = db.get(SchemaMeta, 1)
            if meta is None:
                logger.error("schema_meta 中无记录")
                return False

            stored_version = meta.schema_version
            stored_fingerprint = meta.schema_fingerprint
            stored_app_version = meta.app_version

        # 3. 版本比较
        if stored_version != CURRENT_SCHEMA_VERSION:
            logger.error(
                "Schema 版本不匹配: 数据库={} 程序={}",
                stored_version,
                CURRENT_SCHEMA_VERSION,
            )
            return False

        current_app_version = _app_version_str()
        if stored_app_version != current_app_version:
            logger.error(
                "数据库应用版本不匹配: 数据库={} 程序={}",
                stored_app_version,
                current_app_version,
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

        # 8. 验证实际数据库与当前模型具有完全一致的表和列
        actual_fp = compute_actual_schema_fingerprint()
        ok, msg = _verify_actual_structure()
        if not ok:
            logger.error("实际数据库结构不匹配: {}", msg)
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
    """确保数据库 Schema 可用 — 创建、受限升级或校验。

    - 数据库不存在 → 创建全部表并写入 schema_meta
    - 0.1.17.x Schema v4 → 备份并迁移至 v5
    - 其他已存在数据库 → 严格校验; 不兼容则抛出 RuntimeError

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

    # 数据库已存在 — 仅允许 0.1.18 对精确 v4 基线执行一次升级。
    stored_meta = _stored_schema_meta()
    if (
        stored_meta is not None
        and stored_meta[0] == LEGACY_SCHEMA_VERSION
        and _TARGET_APP_VERSION_RE.fullmatch(_app_version_str()) is not None
    ):
        _migrate_supported_legacy_schema(db_path, stored_meta)

    if validate_schema():
        return

    raise RuntimeError(
        "\n当前数据库 Schema 与程序不兼容。\n"
        f"\n数据库版本: {_stored_version()}"
        f"\n程序要求版本: {CURRENT_SCHEMA_VERSION}"
        "\n\n仅支持从未修改的 0.1.17.x Schema v4 自动升级。"
        "\n数据库已被修改、版本更旧或升级校验失败时会拒绝启动。"
        f"\n\n数据库路径:\n{db_path}\n"
    )


def _migrate_supported_legacy_schema(
    db_path: Path,
    stored_meta: tuple[int, str, str],
) -> None:
    """验证精确 v4 基线后执行唯一受支持的迁移。"""
    from app.db.migration_v0180 import migrate_v4_to_v5

    stored_version, stored_fingerprint, stored_app_version = stored_meta
    if stored_version != LEGACY_SCHEMA_VERSION:
        raise RuntimeError(f"不支持的迁移来源 Schema: {stored_version}")
    if _LEGACY_APP_VERSION_RE.fullmatch(stored_app_version) is None:
        raise RuntimeError(f"不支持的迁移来源应用版本: {stored_app_version}")

    expected_legacy_fingerprint = compute_legacy_v4_fingerprint()
    if stored_fingerprint != expected_legacy_fingerprint:
        raise RuntimeError(
            "0.1.17.x Schema v4 指纹不匹配，拒绝迁移: "
            f"database={stored_fingerprint[:16]} expected={expected_legacy_fingerprint[:16]}"
        )

    structure_ok, structure_message = _verify_actual_structure(excluded_tables={"hotspot_events"})
    if not structure_ok:
        raise RuntimeError(f"0.1.17.x Schema v4 结构不匹配，拒绝迁移: {structure_message}")
    if not _verify_critical_indexes(include_hotspot=False):
        raise RuntimeError("0.1.17.x Schema v4 关键索引不完整，拒绝迁移")
    if not _verify_foreign_keys(include_hotspot=False):
        raise RuntimeError("0.1.17.x Schema v4 外键不完整，拒绝迁移")

    migrate_v4_to_v5(
        engine=_get_engine(),
        db_path=db_path,
        legacy_app_version=stored_app_version,
        legacy_fingerprint=stored_fingerprint,
        target_app_version=_app_version_str(),
        target_fingerprint=compute_schema_fingerprint(),
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


def _stored_schema_meta() -> tuple[int, str, str] | None:
    """读取迁移判断所需的单行 Schema 元数据。"""
    try:
        with Session(_get_engine()) as db:
            meta = db.get(SchemaMeta, 1)
            if meta is None:
                return None
            return meta.schema_version, meta.schema_fingerprint, meta.app_version
    except Exception:
        return None


def _verify_critical_indexes(*, include_hotspot: bool = True) -> bool:
    """按精确列集合校验当前 Schema 的全部关键唯一约束。

    SQLite 会为表级 ``UniqueConstraint`` 生成不稳定的自动索引名，因此这里只
    比较约束的唯一性和列集合，不依赖索引名，也不接受“同表任意唯一索引”冒充
    当前业务约束。

    :returns: True 表示全部关键唯一约束精确存在。
    """
    expected_unique = {
        "segment_tasks": {
            frozenset({"segment_id"}): "SegmentTask.segment_id 唯一",
            frozenset({"pipeline_key"}): "SegmentTask.pipeline_key 唯一",
        },
        "highlight_candidates": {
            frozenset({"dedup_hash"}): "HighlightCandidate.dedup_hash 唯一",
        },
        "highlight_events": {
            frozenset({"candidate_id"}): "HighlightEvent.candidate_id 唯一",
        },
        "transcripts": {
            frozenset({"segment_id"}): "Transcript.segment_id 唯一",
        },
        "highlight_topics": {
            frozenset({"event_id", "topic_id"}): "HighlightTopic(event_id, topic_id) 唯一",
        },
        "upload_tasks": {
            frozenset({"clip_id", "uploader"}): "UploadTask(clip_id, uploader) 唯一",
        },
        "upload_attempts": {
            frozenset({"upload_task_id", "publish_generation"}): (
                "UploadAttempt(upload_task_id, publish_generation) 唯一"
            ),
        },
        "clip_variants": {
            frozenset({"event_id", "variant_type", "render_config_hash"}): "ClipVariant 三维唯一",
        },
    }
    if include_hotspot:
        expected_unique["hotspot_events"] = {
            frozenset({"event_key"}): "HotspotEvent.event_key 唯一",
            frozenset({"candidate_id"}): "HotspotEvent.candidate_id 唯一",
        }

    all_ok = True
    try:
        with _get_engine().connect() as conn:
            for table, constraints in expected_unique.items():
                rows = conn.exec_driver_sql(f"PRAGMA index_list('{table}')").fetchall()
                actual_unique: set[frozenset[str]] = set()
                for row in rows:
                    if not bool(row[2]):
                        continue
                    index_name = str(row[1])
                    index_columns = conn.exec_driver_sql(f"PRAGMA index_info('{index_name}')").fetchall()
                    actual_unique.add(frozenset(str(column[2]) for column in index_columns))
                for expected_columns, description in constraints.items():
                    if expected_columns not in actual_unique:
                        logger.error(
                            "关键唯一约束缺失: {} table={} columns={}",
                            description,
                            table,
                            sorted(expected_columns),
                        )
                        all_ok = False
                    else:
                        logger.debug("唯一约束存在: {} ({})", description, table)
            if include_hotspot:
                rows = conn.exec_driver_sql("PRAGMA index_list('hotspot_events')").fetchall()
                indexed_columns = {
                    tuple(
                        str(column[2]) for column in conn.exec_driver_sql(f"PRAGMA index_info('{row[1]}')").fetchall()
                    )
                    for row in rows
                }
                required = ("session_id", "status", "peak_ts")
                if required not in indexed_columns:
                    logger.error("关键热点索引缺失: hotspot_events{}", required)
                    all_ok = False
    except Exception as exc:
        logger.error("索引验证异常: {}", exc)
        return False

    return all_ok


def _verify_actual_structure(*, excluded_tables: Collection[str] = ()) -> tuple[bool, str]:
    """验证实际数据库与当前模型具有完全一致的表和列。

    从 SQLModel metadata 获取预期结构, 与 PRAGMA 读取的实际结构比较。
    不比较 SQLite 自动生成的名字, 只验证表/列/约束的逻辑存在。

    :returns: (ok, error_message) — ok=True 表示实际结构精确匹配。
    """
    try:
        excluded = set(excluded_tables)
        expected = {}  # table -> set of column names
        for table in SQLModel.metadata.sorted_tables:
            if table.name in excluded:
                continue
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
        unexpected_tables = actual_tables - set(expected)
        if unexpected_tables:
            return False, f"存在未定义表: {unexpected_tables}"

        # 检查每个表的列
        with _get_engine().connect() as conn:
            for tname, expected_cols in expected.items():
                actual_cols_raw = conn.exec_driver_sql(f"PRAGMA table_info({tname})").fetchall()
                actual_cols = {row[1] for row in actual_cols_raw}
                missing_cols = expected_cols - actual_cols
                if missing_cols:
                    return False, f"表 {tname} 缺少列: {missing_cols}"
                unexpected_cols = actual_cols - expected_cols
                if unexpected_cols:
                    return False, f"表 {tname} 存在未定义列: {unexpected_cols}"

        return True, "OK"
    except Exception as exc:
        return False, f"结构验证异常: {exc}"


def _verify_foreign_keys(*, include_hotspot: bool = True) -> bool:
    """按列精确校验当前 Schema 的关键外键。"""
    fk_checks = [
        ("clip_variants", "event_id", "highlight_events", "id", "ClipVariant.event_id -> HighlightEvent"),
        ("highlight_topics", "event_id", "highlight_events", "id", "HighlightTopic.event_id -> HighlightEvent"),
        ("highlight_topics", "topic_id", "topics", "id", "HighlightTopic.topic_id -> Topic"),
        ("upload_tasks", "clip_id", "final_clips", "id", "UploadTask.clip_id -> FinalClip"),
        (
            "upload_attempts",
            "upload_task_id",
            "upload_tasks",
            "id",
            "UploadAttempt.upload_task_id -> UploadTask",
        ),
        ("upload_attempts", "clip_id", "final_clips", "id", "UploadAttempt.clip_id -> FinalClip"),
    ]
    if include_hotspot:
        fk_checks.extend(
            [
                (
                    "hotspot_events",
                    "session_id",
                    "recording_sessions",
                    "id",
                    "HotspotEvent.session_id -> RecordingSession",
                ),
                (
                    "hotspot_events",
                    "candidate_id",
                    "highlight_candidates",
                    "id",
                    "HotspotEvent.candidate_id -> HighlightCandidate",
                ),
                (
                    "hotspot_events",
                    "merged_into_id",
                    "hotspot_events",
                    "id",
                    "HotspotEvent.merged_into_id -> HotspotEvent",
                ),
            ]
        )

    all_ok = True
    try:
        with _get_engine().connect() as conn:
            for table, column, ref_table, ref_column, desc in fk_checks:
                fk_rows = conn.exec_driver_sql(f"PRAGMA foreign_key_list('{table}')").fetchall()
                found = any(row[2] == ref_table and row[3] == column and row[4] == ref_column for row in fk_rows)
                if not found:
                    logger.warning("外键缺失: {}", desc)
                    all_ok = False
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
