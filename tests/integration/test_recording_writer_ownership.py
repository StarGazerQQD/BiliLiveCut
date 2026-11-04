from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import pytest

from app.core.process_spawn import spawn_owned, spawn_owned_async, terminate_owned_async
from app.recording.writer_ownership import SessionWriter

if TYPE_CHECKING:
    from app.analysis.transcription.process_tree import ProcessTree


def test_last_actual_writer_keeps_ownership_after_parent_handle_closes(tmp_path: Path) -> None:
    writer = SessionWriter.create(tmp_path)
    identity = writer.identity
    processes: list[tuple[subprocess.Popen[bytes], ProcessTree]] = []
    writers: list[psutil.Process] = []
    try:
        for _ in range(3):
            with writer.spawn_options() as options:
                process, tree = spawn_owned(
                    [sys.executable, "-c", "import os,time;print(os.getpid(),flush=True);time.sleep(60)"],
                    stdout=subprocess.PIPE,
                    **options,
                )
                processes.append((process, tree))
                assert process.stdout is not None
                writers.append(psutil.Process(int(process.stdout.readline())))
                writers[-1].create_time()
        writer.close()
        for (process, tree), actual_writer in zip(processes, writers, strict=True):
            with pytest.raises(OSError):
                SessionWriter.recover(identity)
            # Windows venv 启动器可能另建真实解释器；须确认完整写入进程树退出。
            tree.terminate()
            process.wait(timeout=5)
            actual_writer.wait(timeout=5)
        with SessionWriter.recover(identity) as recovered:
            assert recovered.identity == identity
    finally:
        writer.close()
        for process, tree in processes:
            tree.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
    assert Path(identity.path).is_file()


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_owned_spawn_inherits_writer_until_actual_process_cleanup(tmp_path: Path, asynchronous: bool) -> None:
    writer = SessionWriter.create(tmp_path)
    identity = writer.identity
    with writer.spawn_options() as options:
        command = [sys.executable, "-c", "import time;time.sleep(60)"]
        if asynchronous:
            process, tree = await spawn_owned_async(command, **options)
        else:
            process, tree = spawn_owned(command, **options)
    writer.close()
    try:
        with pytest.raises(OSError):
            SessionWriter.recover(identity)
    finally:
        if asynchronous:
            await terminate_owned_async(process, tree)
        else:
            tree.terminate()
            process.wait(timeout=5)
    with SessionWriter.recover(identity):
        pass


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_failed_spawn_does_not_leave_extra_writer_handle(tmp_path: Path, asynchronous: bool) -> None:
    writer = SessionWriter.create(tmp_path)
    identity = writer.identity
    try:
        with writer.spawn_options() as options, pytest.raises(FileNotFoundError):
            if asynchronous:
                await spawn_owned_async([str(tmp_path / "missing-command")], **options)
            else:
                spawn_owned([str(tmp_path / "missing-command")], **options)
    finally:
        writer.close()
    with SessionWriter.recover(identity):
        pass


def test_missing_or_replaced_proof_never_becomes_false_exit_confirmation(tmp_path: Path) -> None:
    writer = SessionWriter.create(tmp_path)
    identity = writer.identity
    writer.close()
    path = Path(identity.path)
    retained = path.with_suffix(".retained")
    path.rename(retained)
    with pytest.raises(FileNotFoundError):
        SessionWriter.recover(identity)
    path.write_bytes(b"different file")
    with pytest.raises(ValueError, match="已被替换"):
        SessionWriter.recover(identity)
    assert retained.is_file() and path.read_bytes() == b"different file"
