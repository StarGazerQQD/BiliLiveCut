from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from loguru import logger

if TYPE_CHECKING:
    from loguru import Message


@pytest.fixture
def database_log_sink(temp_db: None) -> Iterator[None]:
    from app.core import logging as app_logging

    logger.complete()
    handler = (
        None
        if app_logging._CONFIGURED
        else logger.add(app_logging._db_sink, level="WARNING", enqueue=True, filter=app_logging._filter_db_sink)
    )
    try:
        yield
    finally:
        logger.complete()
        if handler is not None:
            logger.remove(handler)


def _dump(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


@pytest.mark.parametrize("version", ["0.1.20.0-alpha", "0.1.20.1-alpha", "0.1.20.2-alpha", "9.0.0-alpha"])
def test_application_upgrade_opens_same_format_without_rewriting_data(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    import app
    from app.core.config import settings
    from app.db import job_store
    from app.db.entities import AppSetting, LiveRoom
    from app.db.schema import SchemaMeta, _db_path, compute_schema_fingerprint
    from app.db.session import get_session, init_db
    from tests.integration.test_job_transactions import _job

    with get_session() as db:
        room = LiveRoom(room_id=991, input_url="https://live.bilibili.com/991", title="保留的房间")
        db.add(room)
        db.add(AppSetting(key="compatibility-fixture", value="保留的配置"))
        meta = db.get(SchemaMeta, 1)
        assert meta is not None
        created_by, fingerprint = meta.app_version, meta.schema_fingerprint
    job = job_store.register(_job())
    path = _db_path()
    before = _dump(path)
    url = settings.database_url

    monkeypatch.setattr(app, "__version__", version)
    init_db()
    init_db()

    assert settings.database_url == url and _db_path() == path
    assert _dump(path) == before
    assert job_store.get_job(job["id"]) == job
    assert compute_schema_fingerprint() == fingerprint
    with get_session() as db:
        meta = db.get(SchemaMeta, 1)
        assert meta is not None and meta.app_version == created_by
        assert db.get(LiveRoom, room.id).title == "保留的房间"


@pytest.mark.parametrize("damage", ["version", "fingerprint", "table", "column"])
def test_upgrade_rejects_incompatible_database_and_preserves_all_contents(
    database_log_sink: None, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    import app
    from app.db.schema import _db_path
    from app.db.session import engine, init_db

    with engine.begin() as connection:
        if damage == "version":
            connection.exec_driver_sql("UPDATE schema_meta SET schema_version=5")
        elif damage == "fingerprint":
            connection.exec_driver_sql("UPDATE schema_meta SET schema_fingerprint='incompatible'")
        elif damage == "table":
            connection.exec_driver_sql("CREATE TABLE retained_history (id INTEGER PRIMARY KEY)")
        else:
            connection.exec_driver_sql("ALTER TABLE live_rooms ADD COLUMN retained_field TEXT")
    path = _db_path()
    before = _dump(path)
    monkeypatch.setattr(app, "__version__", "0.1.20.0-alpha")

    with pytest.raises(RuntimeError, match="不会自动迁移、删除或覆盖"):
        init_db()
    logger.error("初始化失败后仍保留文件诊断")
    logger.complete()

    assert _db_path() == path and _dump(path) == before
    assert not list(path.parent.glob("*.bak"))
    assert list(path.parent.glob("*.db")) == [path]


def test_database_logs_resume_after_successful_revalidation(database_log_sink: None) -> None:
    from sqlmodel import select

    from app.db.entities import SystemLog
    from app.db.session import get_session, init_db

    init_db()
    logger.warning("校验成功后可以保存日志")
    logger.complete()
    with get_session() as db:
        rows = db.exec(select(SystemLog).where(SystemLog.message == "校验成功后可以保存日志")).all()
        assert len(rows) == 1


@pytest.mark.parametrize("replace_engine", [False, True])
def test_queued_database_log_cannot_cross_initialization_or_engine(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, replace_engine: bool
) -> None:
    from sqlmodel import create_engine

    from app.core.logging import _db_sink, _filter_db_sink
    from app.db import session
    from app.db.schema import _db_path

    messages: list[Message] = []
    handler = logger.add(messages.append, filter=_filter_db_sink, level="WARNING")
    try:
        logger.warning("旧数据库代次的排队日志")
    finally:
        logger.remove(handler)
    logger.complete()
    assert len(messages) == 1
    before = _dump(_db_path())
    replacement = create_engine(session.engine.url) if replace_engine else None
    try:
        if replacement is not None:
            monkeypatch.setattr(session, "engine", replacement)
            assert session.database_log_generation() is None
        else:
            session.init_db()
        _db_sink(messages[0])
        assert _dump(_db_path()) == before
    finally:
        if replacement is not None:
            replacement.dispose()
