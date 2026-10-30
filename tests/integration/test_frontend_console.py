"""使用真实子进程验证前后端分离启动与退出，不接触用户运行目录。"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import psutil
import pytest


def _stop_console(process: subprocess.Popen) -> None:
    if process.stdin is not None:
        process.stdin.close()
    elif os.name == "nt":
        process.terminate()
    else:
        process.send_signal(signal.SIGTERM)


@pytest.mark.parametrize(
    "close_during_startup,active_sse",
    [(False, False), (True, False), (False, True)],
    ids=["started", "starting", "active-sse"],
)
def test_supervised_backend_drains_lifespan_when_parent_pipe_closes(
    tmp_path: Path, close_during_startup: bool, active_sse: bool
) -> None:
    """无控制台的宿主关闭管道后，真实后端必须执行正常 lifespan 清理。"""
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "PYTHONIOENCODING": "utf-8",
        "BLC_APP_ROOT": str(tmp_path),
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
        "ADMIN_PASSWORD": "",
        "DOCKER_AUTH_ENABLED": "false",
        "BILIBILI_COOKIE": "",
        "ASR_NO_MODEL_DOWNLOAD": "1",
        "TREND_ENABLED": "false",
        "TREND_SCHEDULE_ENABLED": "false",
        "_BLC_PARENT_STDIN": "1",
    }
    opener = build_opener(ProxyHandler({}))
    with (tmp_path / "backend.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import faulthandler, runpy; "
                "faulthandler.dump_traceback_later(20, repeat=True); "
                "runpy.run_module('app.cli', run_name='__main__', alter_sys=True)",
                "backend",
                "--port",
                str(port),
            ],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
        )
        stream = None
        try:
            if close_during_startup:
                process.stdin.close()
            deadline = time.monotonic() + 30
            while not close_during_startup:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/api/dashboard", timeout=1) as response:
                        assert response.status == 200
                        break
                except (OSError, URLError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.1)
            if active_sse:
                stream = opener.open(f"http://127.0.0.1:{port}/api/events", timeout=3)
                assert stream.headers["Content-Type"].startswith("text/event-stream")
                assert stream.readline().startswith(b"id: ")
            process.stdin.close()
            assert process.wait(timeout=25) == 0
            log.seek(0)
            assert "Web 服务资源已关闭" in log.read()
        finally:
            if stream is not None:
                stream.close()
            if process.poll() is None:
                process.stdin.close()
                process.terminate()
                process.wait(timeout=5)


@pytest.mark.parametrize(
    "command,crash,host",
    [(command, crash, "127.0.0.1") for command in ("start", "console") for crash in (None, "backend", "frontend")]
    + [("start", None, "127.0.0.2")],
)
def test_console_starts_independent_frontend_and_api_and_cleans_up(
    tmp_path: Path,
    command: str,
    crash: str | None,
    host: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    dist = tmp_path / "frontend"
    dist.mkdir()
    (dist / "index.html").write_text("<main>independent frontend</main>")
    diagnostics = tmp_path / "startup-diagnostics"
    diagnostics.mkdir()
    # 子进程启动卡住时保留真实线程堆栈，前端和后端沿用相同的诊断环境。
    (diagnostics / "sitecustomize.py").write_text(
        "import faulthandler\nfaulthandler.dump_traceback_later(20, repeat=True)\n", encoding="utf-8"
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(diagnostics), str(root)]),
        "_BLC_PARENT_STDIN": "1",
        "PYTHONIOENCODING": "utf-8",
        "BLC_APP_ROOT": str(tmp_path),
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
        "ADMIN_PASSWORD": "controlled-local-password" if host != "127.0.0.1" else "",
        "DOCKER_AUTH_ENABLED": "false",
        "BILIBILI_COOKIE": "",
        "ASR_NO_MODEL_DOWNLOAD": "1",
        "TREND_ENABLED": "false",
        "TREND_SCHEDULE_ENABLED": "false",
    }
    opener = build_opener(ProxyHandler({}))
    with (tmp_path / "console.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.cli",
                command,
                "--host",
                host,
                "--port",
                str(port),
                "--frontend-dir",
                str(dist),
            ],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with opener.open(f"http://{host}:{port}/", timeout=1) as response:
                        assert response.read() == b"<main>independent frontend</main>"
                        break
                except (OSError, URLError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.1)
            with opener.open(f"http://{host}:{port}/auth/status", timeout=3) as response:
                assert json.loads(response.read()) == {"enabled": False}
            if env["ADMIN_PASSWORD"]:
                from base64 import b64encode

                credentials = b64encode(f"admin:{env['ADMIN_PASSWORD']}".encode()).decode()
                opener.addheaders = [("Authorization", f"Basic {credentials}")]
            with opener.open(f"http://{host}:{port}/api/dashboard", timeout=3) as response:
                assert b'"rooms":[]' in response.read()
            with opener.open(f"http://{host}:{port}/api/settings", timeout=3) as response:
                assert json.loads(response.read())["current_web_port"] == port
            deadline = time.monotonic() + 5
            while "前后端已就绪" not in (tmp_path / "console.log").read_text(encoding="utf-8"):
                assert time.monotonic() < deadline, "监管者没有确认实际绑定地址已就绪"
                time.sleep(0.1)
            children = psutil.Process(process.pid).children(recursive=True)
            if crash is not None:
                # Windows venv 的常驻 Python 转发进程也在 descendants 中，只回收服务代次。
                services = [
                    child
                    for child in children
                    if "app.cli" in child.cmdline() and any(name in child.cmdline() for name in ("backend", "frontend"))
                ]
                assert services
                children = services
                old_pids = {child.pid for child in children}
                target = next(child for child in children if child.cmdline()[1:4] == ["-m", "app.cli", crash])
                target.kill()
                deadline = time.monotonic() + 40
                while True:
                    assert process.poll() is None, "单个服务异常退出后监管进程不应退出"
                    new_children = [
                        child
                        for child in psutil.Process(process.pid).children(recursive=True)
                        if "app.cli" in child.cmdline()
                        and any(name in child.cmdline() for name in ("backend", "frontend"))
                    ]
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/auth/status", timeout=1) as response:
                            if (
                                response.status == 200
                                and new_children
                                and all(child.pid not in old_pids for child in new_children)
                            ):
                                break
                    except (OSError, URLError):
                        pass
                    if time.monotonic() >= deadline:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.1)
                _, alive = psutil.wait_procs(children, timeout=5)
                assert not alive, "恢复新一代前必须回收旧所属树"
                children = psutil.Process(process.pid).children(recursive=True)
            _stop_console(process)
            exit_code = process.wait(timeout=25)
            assert exit_code == 0
            _, alive = psutil.wait_procs(children, timeout=5)
            assert not alive, "停止后不能遗留前端或后端进程"
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0
        finally:
            if process.poll() is None:
                _stop_console(process)
                process.wait(timeout=25)


def test_frontend_alone_serves_build_without_opening_database(tmp_path: Path):
    """独立前端不依赖数据库兼容性；没有后端时页面可用、API 明确返回 502。"""
    from urllib.error import HTTPError

    root = Path(__file__).resolve().parents[2]
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>frontend only</main>")
    database = tmp_path / "incompatible.db"
    database.write_bytes(b"existing database must remain untouched")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "PYTHONIOENCODING": "utf-8",
        "BLC_APP_ROOT": str(tmp_path),
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "DATABASE_URL": f"sqlite:///{database}",
        "ADMIN_PASSWORD": "",
        "DOCKER_AUTH_ENABLED": "false",
    }
    opener = build_opener(ProxyHandler({}))
    with (tmp_path / "frontend.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.cli",
                "frontend",
                "--port",
                str(port),
                "--frontend-dir",
                str(dist),
                "--backend",
                "http://127.0.0.1:1",
            ],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        assert response.read() == b"<main>frontend only</main>"
                        break
                except (OSError, URLError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.1)
            with pytest.raises(HTTPError) as failed:
                opener.open(f"http://127.0.0.1:{port}/api/dashboard", timeout=3)
            assert failed.value.code == 502
            assert database.read_bytes() == b"existing database must remain untouched"
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=20)


def test_start_cleans_up_api_when_frontend_port_is_occupied(tmp_path: Path):
    import re

    root = Path(__file__).resolve().parents[2]
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<main>built frontend</main>")
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "_BLC_PARENT_STDIN": "1",
        "PYTHONIOENCODING": "utf-8",
        "BLC_APP_ROOT": str(tmp_path),
        "STORAGE_ROOT": str(tmp_path / "storage"),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'test.db'}",
        "ADMIN_PASSWORD": "",
        "DOCKER_AUTH_ENABLED": "false",
        "BILIBILI_COOKIE": "",
        "TREND_ENABLED": "false",
        "TREND_SCHEDULE_ENABLED": "false",
        "ASR_NO_MODEL_DOWNLOAD": "1",
    }
    with socket.socket() as occupied:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        with (tmp_path / "start.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.cli", "start", "--port", str(port), "--frontend-dir", str(dist)],
                cwd=tmp_path,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
            )
            try:
                assert process.wait(timeout=30) == 1
                log.seek(0)
                output = log.read()
                api_ports = {int(value) for value in re.findall(r"http://127\.0\.0\.1:(\d+)", output)} - {port}
                assert api_ports, output
                for api_port in api_ports:
                    with socket.socket() as probe:
                        assert probe.connect_ex(("127.0.0.1", api_port)) != 0, output
                assert any(
                    message in output.lower()
                    for message in ("address already in use", "winerror 10048", "winerror 10013")
                ), output
            finally:
                if process.poll() is None:
                    _stop_console(process)
                    process.wait(timeout=25)
