from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import psutil
import pytest

from tests.integration.test_frontend_console import _service_leaves, _service_snapshot, _service_tree, _stop_console

if TYPE_CHECKING:
    from typing import Literal

    from pytest import MonkeyPatch


def _active_members(members: list[psutil.Process]) -> list[tuple[int, str]]:
    active: list[tuple[int, str]] = []
    for member in members:
        try:
            if member.is_running():
                status = member.status()
                if status not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    active.append((member.pid, status))
        except psutil.NoSuchProcess:
            pass  # 外部父进程可能在检查状态期间完成回收。
    return active


@pytest.mark.parametrize(
    "supervisor,stalled,action,snapshot_failure",
    [
        ("console", "backend", "recover", "none"),
        ("console", "frontend", "recover", "none"),
        ("launcher", "backend", "recover", "none"),
        ("launcher", "console", "recover", "none"),
        ("console", "backend", "transient", "none"),
        ("launcher", "backend", "transient", "none"),
        ("console", "backend", "stop", "none"),
        ("console-signal", "backend", "stop", "none"),
        ("launcher", "console", "stop", "none"),
        ("console", "backend", "recover", "once"),
        ("launcher", "backend", "recover", "once"),
        ("console", "backend", "recover", "persistent"),
        ("console", "backend", "recover", "active"),
    ],
)
def test_alive_service_http_outage_recovers_without_overlapping_generations(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    supervisor: str,
    stalled: str,
    action: str,
    snapshot_failure: Literal["none", "once", "persistent", "active"],
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
                denied_reads = 0
                original_cmdline = psutil.Process.cmdline
                if snapshot_failure != "none":

                    def interrupted_cmdline(child: psutil.Process) -> list[str]:
                        nonlocal denied_reads
                        command = original_cmdline(child)
                        if (
                            (denied_reads == 0 or snapshot_failure == "persistent")
                            and command[1:3] == ["-m", "app.cli"]
                            and command[3:4] in (["backend"], ["frontend"])
                            and (snapshot_failure == "active" or not _active_members(owned))
                        ):
                            denied_reads += 1
                            raise psutil.AccessDenied(pid=child.pid)
                        return command

                    monkeypatch.setattr(psutil.Process, "cmdline", interrupted_cmdline)
                deadline = time.monotonic() + 35
                snapshot_error: psutil.AccessDenied | None = None
                if snapshot_failure == "persistent":
                    expected_failure = pytest.raises(
                        AssertionError, match=r"最后一次进程快照读取异常：psutil\.AccessDenied"
                    )
                elif snapshot_failure == "active":
                    expected_failure = pytest.raises(psutil.AccessDenied)
                else:
                    expected_failure = nullcontext()
                with expected_failure:
                    while True:
                        assert process.poll() is None, log_path.read_text(encoding="utf-8")
                        try:
                            after = _service_snapshot(parent)
                        except psutil.AccessDenied as exc:
                            # 旧树已停止后才允许重读，避免缺失观测掩盖代际重叠。
                            if _active_members(owned):
                                raise
                            snapshot_error = exc
                        else:
                            new_members = set(after) - set(before)
                            if new_members:
                                assert not _active_members(owned), "新一代出现时旧所属树仍在执行"
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
                        assert time.monotonic() < deadline, (
                            f"最后一次进程快照读取异常：{snapshot_error!r}\n{log_path.read_text(encoding='utf-8')}"
                        )
                        time.sleep(0.1)
                if snapshot_failure == "active":
                    assert denied_reads == 1 and _active_members(owned)
                    assert sentinel.poll() is None
                    return
                if snapshot_failure == "persistent":
                    assert denied_reads > 1
                    monkeypatch.setattr(psutil.Process, "cmdline", original_cmdline)
                    owned.extend(_service_tree(_service_snapshot(parent)))
                else:
                    assert denied_reads == int(snapshot_failure == "once")
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
            active = _active_members(alive)
            assert not active, active
            assert sentinel.returncode is not None
