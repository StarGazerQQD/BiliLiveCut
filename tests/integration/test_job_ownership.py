from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from threading import Event

import pytest
from sqlmodel import create_engine

from app.db import job_store, session
from app.jobs.manager import JobManager


async def test_live_manager_cannot_be_recovered_by_another_manager(temp_db: None) -> None:
    first, second = JobManager(), JobManager()
    entered, release = Event(), Event()
    calls: list[str] = []

    def handler(context, payload):
        calls.append(context.job_id)
        entered.set()
        assert release.wait(10)
        return {"ok": True}

    first.register("test", handler)
    second.register("test", handler)
    try:
        job = await first.enqueue("test", {}, label="ownership", owner="alice")
        assert await asyncio.to_thread(entered.wait, 5)
        before = job_store.get_job(job["id"])
        with pytest.raises(RuntimeError, match="已有 Web 作业执行实例"):
            await second.start()
        assert job_store.get_job(job["id"]) == before
        assert calls == [job["id"]]
        release.set()
        await asyncio.gather(*tuple(first._tasks.values()))
        await first.stop()
        await second.start()
        assert job_store.get_job(job["id"])["status"] == "succeeded"
        assert calls == [job["id"]]
    finally:
        release.set()
        await first.stop()
        await second.stop()


async def test_shutdown_timeout_keeps_exclusive_ownership(temp_db: None) -> None:
    first, second = JobManager(), JobManager()
    entered, release = Event(), Event()

    def handler(context, payload):
        entered.set()
        assert release.wait(10)
        return {}

    first.register("test", handler)
    try:
        await first.enqueue("test", {}, label="ownership", owner="alice")
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(RuntimeError, match="未确认退出"):
            await first.stop(timeout_s=0.01)
        with pytest.raises(RuntimeError, match="已有 Web 作业执行实例"):
            await second.start()
        release.set()
        await first.stop()
        await second.start()
    finally:
        release.set()
        await first.stop()
        await second.stop()


async def test_failed_start_releases_ownership(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = JobManager()

    def broken():
        raise OSError("cannot scan jobs")

    with monkeypatch.context() as patch:
        patch.setattr(job_store, "iter_active_jobs", broken)
        with pytest.raises(OSError, match="cannot scan"):
            await manager.start()
    other = JobManager()
    try:
        await other.start()
    finally:
        await other.stop()


def test_database_path_alias_cannot_bypass_ownership(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = session.engine
    database = Path(original.url.database)
    if sys.platform == "win32":
        # Windows 默认文件系统不区分大小写，不需要创建符号链接的额外权限。
        alias = database.with_name(database.name.swapcase())
    else:
        alias = tmp_path / "alias.db"
        alias.symlink_to(database)
    release = job_store.acquire_execution()
    alternate = create_engine(f"sqlite:///{alias}")
    try:
        monkeypatch.setattr(session, "engine", alternate)
        with pytest.raises(RuntimeError, match="已有 Web 作业执行实例"):
            job_store.acquire_execution()
    finally:
        release()
        alternate.dispose()


async def test_crashed_process_releases_owner_before_recovery(temp_db: None, tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    program = """
import asyncio, sys
from pathlib import Path
from threading import Event
from app.db.session import init_db
from app.jobs.manager import JobManager
init_db()
async def main():
    manager = JobManager()
    def handler(context, payload):
        Path(sys.argv[1]).write_text(context.job_id)
        Event().wait(60)
        return {}
    manager.register("test", handler)
    await manager.enqueue("test", {}, label="crash", owner="alice")
    await asyncio.Event().wait()
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        program,
        str(ready),
        env=os.environ.copy(),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    contender = JobManager()
    calls: list[str] = []
    contender.register("test", lambda context, payload: calls.append(context.job_id) or {"recovered": True})
    try:
        async with asyncio.timeout(15):
            while True:
                assert child.returncode is None
                if ready.exists() and len(job_id := ready.read_text()) == 32:
                    break
                await asyncio.sleep(0.02)
        before = job_store.get_job(job_id)
        assert before["status"] == "running" and before["attempt"] == 1
        with pytest.raises(RuntimeError, match="已有 Web 作业执行实例"):
            await contender.start()
        assert job_store.get_job(job_id) == before and calls == []
        child.kill()
        await child.wait()
        await contender.start()
        await asyncio.gather(*tuple(contender._tasks.values()))
        after = job_store.get_job(job_id)
        assert after["status"] == "succeeded" and after["attempt"] == 2 and after["recovered"]
        assert calls == [job_id]
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()
        await contender.stop()


async def test_cancelled_recovery_keeps_owner_until_database_work_finishes(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    first, second = JobManager(), JobManager()
    original = job_store.iter_active_jobs

    def blocked_scan():
        entered.set()
        assert release.wait(10)
        return original()

    monkeypatch.setattr(job_store, "iter_active_jobs", blocked_scan)
    starting = asyncio.create_task(first.start())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        starting.cancel()
        await asyncio.sleep(0)
        assert not starting.done()
        with pytest.raises(RuntimeError, match="已有 Web 作业执行实例"):
            await second.start()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starting
        await second.start()
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await first.stop()
        await second.stop()


def test_file_uri_cannot_bypass_database_owner(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    original = session.engine
    release = job_store.acquire_execution()
    alternate = create_engine(f"sqlite:///file:{Path(original.url.database).as_posix()}?uri=true")
    try:
        monkeypatch.setattr(session, "engine", alternate)
        with pytest.raises(RuntimeError, match="已有 Web 作业执行实例"):
            job_store.acquire_execution()
    finally:
        release()
        alternate.dispose()


def test_in_memory_database_owner_is_scoped_to_engine(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = create_engine("sqlite://"), create_engine("sqlite://")
    monkeypatch.setattr(session, "engine", first)
    release = job_store.acquire_execution()
    try:
        with pytest.raises(RuntimeError, match="已有 Web 作业执行实例"):
            job_store.acquire_execution()
        monkeypatch.setattr(session, "engine", second)
        other_release = job_store.acquire_execution()
        other_release()
    finally:
        release()
        first.dispose()
        second.dispose()
