from __future__ import annotations

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

from tests.integration.test_frontend_console import _service_leaves, _service_snapshot, _service_tree, _stop_console


@pytest.mark.parametrize(
    "supervisor,stalled,action",
    [
        ("console", "backend", "recover"),
        ("console", "frontend", "recover"),
        ("launcher", "backend", "recover"),
        ("launcher", "console", "recover"),
        ("console", "backend", "transient"),
        ("launcher", "backend", "transient"),
        ("console", "backend", "stop"),
        ("console-signal", "backend", "stop"),
        ("launcher", "console", "stop"),
    ],
)
def test_alive_service_http_outage_recovers_without_overlapping_generations(
    tmp_path: Path, supervisor: str, stalled: str, action: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>HTTP supervision</main>", encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(root), str(root / "packaging/portable/src")]),
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "_BLC_PARENT_STDIN": "1",
        "BLC_WEB_PORT": str(port),
        "BLC_APP_ROOT": str(tmp_path),
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
        "ADMIN_PASSWORD": "",
        "DOCKER_AUTH_ENABLED": "false",
        "BILIBILI_COOKIE": "",
        "ASR_NO_MODEL_DOWNLOAD": "1",
        "TREND_ENABLED": "false",
        "TREND_SCHEDULE_ENABLED": "false",
    }
    log_path = tmp_path / "supervisor.log"
    opener = build_opener(ProxyHandler({}))
    suspended: psutil.Process | None = None
    owned: list[psutil.Process] = []
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(180)"], cwd=tmp_path)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(root / "tests/integration/http_supervision_actor.py"),
                supervisor,
                str(port),
                str(frontend),
            ],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
        )
        parent = psutil.Process(process.pid)
        try:
            deadline = time.monotonic() + 45
            while "前后端已就绪" not in log_path.read_text(encoding="utf-8"):
                assert process.poll() is None and time.monotonic() < deadline, log_path.read_text(encoding="utf-8")
                time.sleep(0.1)
            with opener.open(f"http://127.0.0.1:{port}/auth/status", timeout=2) as response:
                assert response.status == 200
            # 等外层监督也完成首次成功观测，随后注入真实 OS 挂起。
            time.sleep(1.3)
            before = _service_snapshot(parent)
            leaves = _service_leaves(before)
            assert all(len(members) == 1 for members in leaves.values()), before
            owned = _service_tree(before)
            if stalled == "console":
                service_pids = {member.pid for member, _, _ in before.values()}
                parents = {parent_pid for _, _, parent_pid in before.values()} - service_pids
                assert len(parents) == 1, before
                console_pid = parents.pop()
                suspended = psutil.Process(console_pid)
                assert suspended.cmdline()[1:4] == ["-m", "app.cli", "start"]
                owned.append(suspended)
            else:
                suspended = leaves[stalled][0]
            suspended.suspend()
            # console 挂起时子服务仍会响应；同时挂起后端使外层观察真实 HTTP 故障。
            stalled_backend = leaves["backend"][0] if stalled == "console" else None
            if stalled_backend is not None:
                stalled_backend.suspend()
            if action == "transient":
                with pytest.raises((OSError, URLError)):
                    opener.open(f"http://127.0.0.1:{port}/auth/status", timeout=0.5)
                time.sleep(0.5)
                suspended.resume()
                suspended = None
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/auth/status", timeout=1) as response:
                            assert response.status == 200
                            break
                    except (OSError, URLError):
                        assert time.monotonic() < deadline
                time.sleep(4)
                assert set(_service_snapshot(parent)) == set(before)
                assert "开始有界回收" not in log_path.read_text(encoding="utf-8")
            elif action == "stop":
                deadline = time.monotonic() + 15
                while "开始有界回收" not in log_path.read_text(encoding="utf-8"):
                    assert process.poll() is None and time.monotonic() < deadline, log_path.read_text(encoding="utf-8")
                    time.sleep(0.05)
                _stop_console(process)
                assert process.wait(timeout=15) in (0, 1, 130)
                output = log_path.read_text(encoding="utf-8")
                assert output.count("API 启动中") == 1, output
                assert "秒后恢复" not in output, output
            else:
                deadline = time.monotonic() + 35
                while True:
                    assert process.poll() is None, log_path.read_text(encoding="utf-8")
                    after = _service_snapshot(parent)
                    new_members = set(after) - set(before)
                    if new_members:
                        assert not [
                            member.pid
                            for member in owned
                            if member.is_running() and member.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
                        ], "新一代出现时旧所属树仍在执行"
                    new_leaves = _service_leaves(after)
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/auth/status", timeout=0.5) as response:
                            if (
                                response.status == 200
                                and all(len(members) == 1 for members in new_leaves.values())
                                and set(before).isdisjoint(after)
                            ):
                                owned.extend(_service_tree(after))
                                break
                    except (OSError, URLError):
                        pass
                    assert time.monotonic() < deadline, log_path.read_text(encoding="utf-8")
                    time.sleep(0.1)
                output = log_path.read_text(encoding="utf-8")
                assert output.count("API 启动中") == 2, output
                assert output.count("秒后恢复") == 1, output
            assert sentinel.poll() is None
        finally:
            for member in owned:
                try:
                    member.resume()
                except psutil.NoSuchProcess:
                    pass
            if process.poll() is None:
                _stop_console(process)
                process.wait(timeout=30)
            _, alive = psutil.wait_procs(owned, timeout=5)
            sentinel.terminate()
            sentinel.wait(timeout=5)
            assert not alive, [(member.pid, member.status()) for member in alive]
            assert sentinel.returncode is not None
