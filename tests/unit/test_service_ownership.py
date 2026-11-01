from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.analysis.transcription.process_tree import ProcessTree
from app.db.service_ownership import ServiceOwnership


def test_service_ownership_protects_root_and_canonical_database(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'shared.db').as_posix()}")
    other = create_engine(f"sqlite:///{(tmp_path / 'other.db').as_posix()}")
    root = tmp_path / "first"
    owner = ServiceOwnership(root, engine)
    try:
        with pytest.raises(TimeoutError):
            ServiceOwnership(root, other)
        with pytest.raises(TimeoutError):
            ServiceOwnership(tmp_path / "second", engine)
    finally:
        owner.release()
    replacement = ServiceOwnership(tmp_path / "second", engine)
    replacement.release()
    replacement.release()
    assert (root / ".service.lock").exists()
    assert (tmp_path / "shared.db.service.lock").exists()
    assert not (tmp_path / "shared.db").exists()


def test_failed_shutdown_keeps_instance_lock_until_real_process_exit(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    root = tmp_path / "app"
    database = tmp_path / "shared.db"
    script = """
import asyncio, gc, sys, time
from pathlib import Path
from sqlalchemy import create_engine
from app.core.supervisor import Service, Supervisor
from app.db.service_ownership import ServiceOwnership
root, database, ready = map(Path, sys.argv[1:])
async def failed_stop():
    raise RuntimeError('controlled external cleanup failure')
async def exercise():
    owner = ServiceOwnership(root, create_engine('sqlite:///' + database.as_posix()))
    owner.supervisor = Supervisor([Service('held', stop=failed_stop)], lambda: None)
    try:
        async with owner.supervisor.running():
            pass
    except RuntimeError:
        pass
    owner.release()
asyncio.run(exercise())
gc.collect()
ready.write_text('held', encoding='utf-8')
time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root), str(database), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **({"creationflags": 0x00000004} if os.name == "nt" else {"start_new_session": True}),
    )
    tree = ProcessTree(process.pid)
    tree.resume_suspended()
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists() and process.poll() is None
        with pytest.raises(TimeoutError):
            ServiceOwnership(root, engine)
    finally:
        tree.terminate()
        process.wait(timeout=5)
    owner = ServiceOwnership(root, engine)
    owner.release()
