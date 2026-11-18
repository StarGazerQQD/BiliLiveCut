# ruff: noqa: D100
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import URL
from sqlmodel import Session, create_engine

from app.db.schema import create_schema
from app.recording.library_rebuild_files import index_files
from app.recording.library_rebuild_rows import import_library, put_setting
from app.recording.storage_check import _open_offline_database


class LibrarySource(BaseModel):
    """一个停写副本的数据库、存储根以及可选的外置目录。"""

    model_config = ConfigDict(extra="forbid")
    storage: Path
    database: Path | None = None
    original_storage: Path | None = None
    directory_maps: dict[str, Path] = Field(default_factory=dict)
    raw: Path | None = None
    danmaku: Path | None = None


class LibraryRebuild(BaseModel):
    """命令行和设置页共用的重建清单。"""

    model_config = ConfigDict(extra="forbid")
    sources: list[LibrarySource] = Field(min_length=1)


class RebuildResult(BaseModel):
    """供操作者核对的新库位置、记录数量和缺失文件数。"""

    database: str
    sources: int
    sessions: int
    segments: int
    messages: int
    missing_files: int


def rebuild_library(
    request: LibraryRebuild, output: Path, *, check_cancelled: Callable[[], None] | None = None
) -> RebuildResult:
    """在独占的新目录中构建当前格式数据库，完整性检查成功后发布。"""
    from app.db.schema import _db_path

    destination = output.expanduser().absolute()
    if destination.resolve() != destination:
        raise ValueError("新库输出目录不能经过链接")
    sources = []
    seen = set()
    active_db = _db_path().resolve()
    for source in request.sources:
        root = source.storage.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("来源 storage 必须是目录")
        database = source.database.resolve(strict=True) if source.database else None
        if database and (not database.is_file() or (active_db.exists() and database.samefile(active_db))):
            raise ValueError("请选择独立的离线数据库副本")
        raw = source.raw.resolve(strict=True) if source.raw else None
        danmaku = source.danmaku.resolve(strict=True) if source.danmaku else None
        for path in (raw, danmaku):
            if path is not None and not path.is_dir():
                raise ValueError("原片和弹幕位置必须是目录")
        identity = (
            (database.stat().st_dev, database.stat().st_ino)
            if database
            else (raw or root / "raw", danmaku or root / "raw")
        )
        if identity in seen:
            raise ValueError("同一来源不能重复导入")
        seen.add(identity)
        mappings = {Path(key): path.resolve(strict=True) for key, path in source.directory_maps.items()}
        if source.original_storage:
            mappings[source.original_storage] = root
        if any(not old.is_absolute() or not new.is_dir() for old, new in mappings.items()):
            raise ValueError("目录映射需要原绝对路径及有效的副本目录")
        sources.append((root, database, raw, danmaku, mappings))

    destination.mkdir(exist_ok=False)
    completed = False
    try:
        with TemporaryDirectory(dir=destination) as temporary:
            staging = Path(temporary) / "blc.db"
            engine = create_engine(URL.create("sqlite", database=str(staging)))
            try:
                with Session(engine) as session:
                    create_schema(session)
                    session.commit()
            finally:
                engine.dispose()
            missing = 0
            with closing(sqlite3.connect(staging)) as target, target:
                target.execute("PRAGMA foreign_keys=ON")
                target.execute("BEGIN")
                target.execute("PRAGMA defer_foreign_keys=ON")
                for number, (root, database, raw, danmaku, mappings) in enumerate(sources, start=1):
                    if check_cancelled:
                        check_cancelled()
                    if database:
                        with _open_offline_database(database) as original:
                            original.row_factory = sqlite3.Row
                            report = import_library(
                                original, target, number, root, raw, danmaku, mappings, check_cancelled
                            )
                    else:
                        report = index_files(
                            target, root, raw or root / "raw", danmaku or root / "raw", check_cancelled
                        )
                    put_setting(
                        target,
                        f"library_rebuild:source:{number}",
                        json.dumps(
                            {"storage": str(root), "database": str(database) if database else None, **report},
                            ensure_ascii=False,
                        ),
                    )
                    missing += int(report.get("missing_files", 0))
                if target.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("重建库的外键检查失败")
                if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("重建库的 SQLite 完整性检查失败")
                counts = [
                    target.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("recording_sessions", "raw_segments", "danmaku")
                ]
                if not counts[0]:
                    raise ValueError("所选来源中没有可重建的场次")
            if check_cancelled:
                check_cancelled()
            result = RebuildResult(
                database=str(destination / "blc.db"),
                sources=len(sources),
                sessions=counts[0],
                segments=counts[1],
                messages=counts[2],
                missing_files=missing,
            )
            # Windows 的 rename 排他发布，同样支持没有硬链接的外置卷。
            if os.name == "nt":
                staging.rename(result.database)
            else:
                Path(result.database).hardlink_to(staging)
            completed = True
            return result
    finally:
        if not completed and not any(destination.iterdir()):
            destination.rmdir()
