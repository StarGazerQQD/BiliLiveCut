from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import psutil
import pytest

from app.core.process_spawn import spawn_owned, spawn_owned_async, terminate_owned_async, wait_process_exit


def assert_stopped(processes: list[psutil.Process]) -> None:
    deadline = time.monotonic() + 5
    while True:
        alive = []
        for process in processes:
            try:
                if process.is_running() and process.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    alive.append(process.pid)
            except psutil.NoSuchProcess:
                pass
        if not alive:
            return
        assert time.monotonic() < deadline, f"owned processes still active: {alive}"
        time.sleep(0.02)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_owned_spawn_preserves_missing_executable_error(tmp_path: Path, asynchronous: bool) -> None:
    command = [str(tmp_path / "missing-command")]
    with pytest.raises(FileNotFoundError) as error:
        if asynchronous:
            await spawn_owned_async(command)
        else:
            spawn_owned(command)
    assert error.value.errno == errno.ENOENT


@pytest.mark.parametrize("minimum_descriptor", [0] if os.name == "nt" else [0, 1100])
def test_repeated_owned_spawns_release_descriptors_above_select_limit(tmp_path: Path, minimum_descriptor: int) -> None:
    root = Path(__file__).resolve().parents[2]
    actor = """
import asyncio, errno, os, subprocess, sys
from contextlib import ExitStack
from pathlib import Path
import psutil
from app.core.process_spawn import spawn_owned, spawn_owned_async, terminate_owned_async

async def exercise():
    owner = psutil.Process()
    baseline = owner.num_fds() if os.name != 'nt' else None
    command = [sys.executable, '-c', 'print("owned", end="")']
    missing = [str(Path.cwd() / 'missing-command')]
    for _ in range(4):
        process, tree = spawn_owned(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, error = process.communicate(timeout=5)
            assert process.returncode == 0 and output == b'owned' and error == b''
        finally:
            tree.terminate()
            process.wait(timeout=5)
        process, tree = await spawn_owned_async(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, error = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 0 and output == b'owned' and error == b''
        finally:
            await terminate_owned_async(process, tree, drain_output=True)
        for asynchronous in (False, True):
            try:
                if asynchronous:
                    await spawn_owned_async(missing)
                else:
                    spawn_owned(missing)
            except FileNotFoundError as exc:
                assert exc.errno == errno.ENOENT
            else:
                raise AssertionError('missing command unexpectedly started')
        await asyncio.sleep(0)
        if baseline is not None:
            assert owner.num_fds() == baseline, (baseline, owner.num_fds())

with ExitStack() as reserved:
    minimum = int(sys.argv[1])
    if minimum:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < minimum + 128:
            resource.setrlimit(resource.RLIMIT_NOFILE, (minimum + 128, hard))
        while reserved.enter_context(open(os.devnull, 'rb')).fileno() < minimum:
            pass
    asyncio.run(exercise())
"""
    result = subprocess.run(
        [sys.executable, "-c", actor, str(minimum_descriptor)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_direct_process_exit_is_observed_before_descendant_pipe_closes(tmp_path: Path) -> None:
    marker = tmp_path / "descendant.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import subprocess,sys; from pathlib import Path; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            f"Path({str(marker)!r}).write_text(str(child.pid));sys.exit(7)"
        ),
    ]
    process, tree = await spawn_owned_async(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert await asyncio.wait_for(wait_process_exit(process), 5) == 7
        descendants = [psutil.Process(int(marker.read_text()))]
        await asyncio.wait_for(terminate_owned_async(process, tree, drain_output=True), 5)
        assert process.returncode == 7
        assert_stopped(descendants)
    finally:
        await terminate_owned_async(process, tree, drain_output=True)


@pytest.mark.parametrize("closed_standard_fd", [-1] if os.name == "nt" else [-1, 0, 1, 2])
def test_parent_abrupt_exit_reclaims_owned_tree_and_preserves_unrelated_process(
    tmp_path: Path, closed_standard_fd: int
) -> None:
    root = Path(__file__).resolve().parents[2]
    ready, descendant = tmp_path / "ready.json", tmp_path / "descendant.pid"
    actor = """
import json, os, subprocess, sys, time
from pathlib import Path
from app.core.process_spawn import spawn_owned
code = "import subprocess,sys,time;from pathlib import Path;assert sys.stdin.buffer.readline()==b'start'+bytes([10]);sys.stdout.buffer.write(b'ready'+bytes([10]));sys.stdout.buffer.flush();child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path(sys.argv[1]).write_text(str(child.pid));time.sleep(60)"
closed = int(sys.argv[3])
if closed >= 0:
    os.close(closed)
process, tree = spawn_owned([sys.executable, '-c', code, sys.argv[2]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
process.stdin.write(b'start\\n')
process.stdin.flush()
assert process.stdout.readline() == b'ready\\n'
Path(sys.argv[1]).write_text(json.dumps({'actor':os.getpid(),'child':process.pid}))
process.wait()
"""
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
    parent = subprocess.Popen(
        [sys.executable, "-c", actor, str(ready), str(descendant), str(closed_standard_fd)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root)},
    )
    owned: list[psutil.Process] = []
    actual_parent = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() or not descendant.exists():
            assert parent.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        details = json.loads(ready.read_text())
        actual_parent = psutil.Process(details["actor"])
        owned = actual_parent.children(recursive=True)
        assert any(process.pid == int(descendant.read_text()) for process in owned)
        actual_parent.kill()
        parent.wait(timeout=5)
        assert_stopped(owned)
        assert unrelated.poll() is None
    finally:
        for process in [*owned, *([actual_parent] if actual_parent is not None else [])]:
            try:
                if process.is_running():
                    process.kill()
            except psutil.NoSuchProcess:
                pass
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        unrelated.terminate()
        unrelated.wait(timeout=5)


@pytest.mark.parametrize("stderr_mode", ["file", "closed", "blocked", "thread_unavailable"])
def test_owned_backend_deadline_exits_even_with_blocked_executor(tmp_path: Path, stderr_mode: str) -> None:
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    actor = """
import asyncio, os, sys, threading, time
from contextlib import asynccontextmanager
from pathlib import Path
from app.web.main import app
from app.cli import main
if os.environ['STDERR_MODE'] == 'thread_unavailable':
    original_start = threading.Thread.start
    def start(thread):
        if thread.name == 'blc-exit-diagnostic':
            raise RuntimeError('fixture cannot start diagnostic thread')
        return original_start(thread)
    threading.Thread.start = start
original = app.router.lifespan_context
async def disrupt_stderr():
    root = Path(os.environ['BLC_APP_ROOT'])
    while not (root / 'disrupt-stderr').exists():
        await asyncio.sleep(0.02)
    mode = os.environ['STDERR_MODE']
    if mode == 'closed':
        os.close(2)
    elif mode == 'blocked':
        done = threading.Event()
        def fill():
            for _ in range(262144):
                os.write(2, b'x' * 64)
            done.set()
        threading.Thread(target=fill, daemon=True).start()
        await asyncio.sleep(0.2)
        assert not done.is_set(), 'stderr pipe did not block'
    (root / 'stderr-ready').write_text(mode)
@asynccontextmanager
async def blocked(application):
    async with original(application):
        asyncio.create_task(asyncio.to_thread(time.sleep, 60))
        asyncio.create_task(disrupt_stderr())
        yield
app.router.lifespan_context = blocked
main()
"""
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "PYTHONIOENCODING": "utf-8",
        "BLC_APP_ROOT": str(tmp_path),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "ADMIN_PASSWORD": "",
        "DOCKER_AUTH_ENABLED": "false",
        "ASR_NO_MODEL_DOWNLOAD": "1",
        "TREND_ENABLED": "false",
        "TREND_SCHEDULE_ENABLED": "false",
        "BILIBILI_COOKIE": "",
        "WORKER_SHUTDOWN_TIMEOUT_SECONDS": "1",
        "_BLC_PARENT_STDIN": "1",
        "STDERR_MODE": stderr_mode,
    }
    opener = build_opener(ProxyHandler({}))
    with (tmp_path / "backend.log").open("w+", encoding="utf-8") as log:
        process, tree = spawn_owned(
            [sys.executable, "-c", actor, "backend", "--port", str(port)],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log if stderr_mode == "file" else subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/auth/status", timeout=1) as response:
                        assert response.status == 200
                        break
                except (OSError, URLError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.1)
            (tmp_path / "disrupt-stderr").touch()
            deadline = time.monotonic() + 5
            while not (tmp_path / "stderr-ready").exists():
                assert process.poll() is None
                assert time.monotonic() < deadline, "stderr fault was not prepared"
                time.sleep(0.02)
            assert process.stdin is not None
            started = time.monotonic()
            process.stdin.close()
            assert process.wait(timeout=8) == 1
            assert time.monotonic() - started < 8
            log.seek(0)
            if stderr_mode == "file":
                assert "总期限到达仍未退出时将终止" in log.read()
            elif stderr_mode == "blocked":
                assert process.stderr is not None
                assert b"x" * 64 in process.stderr.read()
        finally:
            tree.terminate()
            process.wait(timeout=5)
            if process.stderr is not None:
                process.stderr.close()
