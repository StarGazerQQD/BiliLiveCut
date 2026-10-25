from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import psutil
import pytest

from app.core import metrics
from app.core.process_control import run_cancellable
from app.pipeline.lifecycle import shutdown_event


def _prepare_lifespan(
    monkeypatch: pytest.MonkeyPatch, *, fail_start: str | None = None, fail_stop: str | None = None
) -> list[str]:
    from app.pipeline import live_monitor, task_worker
    from app.plugins import manager as plugins
    from app.trends import scheduler
    from app.web import main
    from app.web.services import background_jobs, rooms

    stopped: list[str] = []

    def service(name: str) -> SimpleNamespace:
        async def start() -> None:
            if name == fail_start:
                raise RuntimeError(f"start {name} failed")

        async def stop() -> None:
            assert shutdown_event.is_set()
            stopped.append(name)
            if name == fail_stop:
                raise RuntimeError(f"stop {name} failed")

        return SimpleNamespace(start=start, stop=stop)

    monkeypatch.setattr(main, "setup_logging", lambda: None)
    monkeypatch.setattr(main, "_setup_proxy_env", lambda: None)
    monkeypatch.setattr(metrics, "start_metrics_collector", lambda **kwargs: None)
    monkeypatch.setattr(metrics, "stop_metrics_collector", lambda: stopped.append("metrics"))
    trends = service("trends")
    trends.start = lambda **kwargs: None
    monkeypatch.setattr(scheduler, "trend_scheduler", trends)
    monkeypatch.setattr(plugins, "plugin_manager", service("plugins"))
    worker = service("worker")
    monkeypatch.setattr(task_worker, "get_worker", lambda: worker)
    monkeypatch.setattr(background_jobs, "web_job_manager", service("web_jobs"))
    monkeypatch.setattr(live_monitor, "live_monitor", service("live_monitor"))
    recorder = service("recorders")
    recorder.stop_all = recorder.stop
    recorder.request_stop_all = lambda: None
    recorder.running_ids = lambda: []
    monkeypatch.setattr(rooms, "recorder_manager", recorder)

    async def recover() -> int:
        return 0

    monkeypatch.setattr(rooms, "auto_recover_interrupted_sessions", recover)
    return stopped


async def test_lifespan_signals_recorders_before_waiting_for_live_monitor(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import live_monitor
    from app.web import main
    from app.web.services import rooms

    _prepare_lifespan(monkeypatch)
    signalled = Event()
    monkeypatch.setattr(rooms.recorder_manager, "request_stop_all", signalled.set)

    async def stop_monitor() -> None:
        assert shutdown_event.is_set() and signalled.is_set()
        await asyncio.sleep(0)

    monkeypatch.setattr(live_monitor.live_monitor, "stop", stop_monitor)
    async with main.lifespan(main.app):
        assert not signalled.is_set()


@pytest.mark.parametrize("stage", ["fetch", "record"])
async def test_shutdown_during_recording_does_not_refetch_or_report_ffmpeg_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from app.pipeline import storage_lifecycle
    from app.plugins.live_source import SourceRoom, StreamSpec
    from app.recording import recorder as module

    recorder = module.Recorder(SourceRoom(platform="test", source_id="1", canonical_url="https://example.invalid/1"), 1)
    fetched, recorded, exits = [], [], []

    async def start_danmaku() -> None:
        pass

    async def fetch() -> StreamSpec:
        fetched.append(1)
        if stage == "fetch":
            shutdown_event.set()
        return StreamSpec(url="https://example.invalid/live", transport="hls", container="ts")

    async def record(*args: object) -> int:
        recorded.append(1)
        shutdown_event.set()
        return 255

    monkeypatch.setattr(module, "session_raw_dir", lambda _: tmp_path)
    monkeypatch.setattr(storage_lifecycle, "should_stop_recording", lambda: False)
    monkeypatch.setattr(recorder, "_start_danmaku", start_danmaku)
    monkeypatch.setattr(recorder, "_fetch_stream", fetch)
    monkeypatch.setattr(recorder, "_record_once", record)
    monkeypatch.setattr(recorder, "_update_session", lambda **kwargs: None)
    monkeypatch.setattr(recorder, "_save_stream_selection", lambda stream: None)
    monkeypatch.setattr(recorder, "_log_recording_exit", lambda *args: exits.append(args))
    await asyncio.wait_for(recorder._run_session(), timeout=2)
    assert fetched == [1] and recorded == ([1] if stage == "record" else [])
    assert not exits and not recorder._last_exit_abnormal


async def test_global_shutdown_interrupts_recorder_backoff() -> None:
    from app.plugins.live_source import SourceRoom
    from app.recording.recorder import Recorder

    recorder = Recorder(SourceRoom(platform="test", source_id="1", canonical_url="https://example.invalid/1"), 1)
    waiting = asyncio.create_task(recorder._sleep_or_stop(60))
    await asyncio.sleep(0)
    shutdown_event.set()
    await asyncio.wait_for(waiting, timeout=1)
    assert recorder._stop.is_set()


async def test_shutdown_notifies_all_recorders_and_cleans_others_after_one_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web.services.rooms import RecorderManager

    manager = RecorderManager()
    stopped: set[int] = set()
    manager._recorders = {room: SimpleNamespace(stop=lambda room=room: stopped.add(room)) for room in (1, 2)}
    manager._tasks = {1: None, 2: None}
    cleaned = []

    async def stop(room: int) -> None:
        assert stopped == {1, 2}
        cleaned.append(room)
        if room == 1:
            raise RuntimeError("one room failed")

    monkeypatch.setattr(manager, "stop", stop)
    with pytest.raises(ExceptionGroup, match="录制关闭期间"):
        await manager.stop_all()
    assert cleaned == [1, 2]


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        ([], []),
        ([(socket.AF_INET, "127.0.0.2"), (socket.AF_INET6, "::1")], []),
        ([(socket.AF_INET, "192.0.2.5"), (socket.AF_INET, "192.0.2.5")], ["192.0.2.5"]),
        ([(socket.AF_INET6, "fe80::1%en0"), (socket.AF_INET6, "2001:db8::1")], ["2001:db8::1", "fe80::1%en0"]),
        ([(psutil.AF_LINK, "01:02:03:04:05:06")], []),
    ],
)
def test_startup_interface_scan_filters_local_addresses_without_dns(
    monkeypatch: pytest.MonkeyPatch, addresses: list[tuple[int, str]], expected: list[str]
) -> None:
    from app.web import main

    def interfaces() -> dict[str, list[SimpleNamespace]]:
        return {"test0": [SimpleNamespace(family=family, address=address) for family, address in addresses]}

    def unavailable_dns(*args: object, **kwargs: object) -> None:
        raise AssertionError("startup must not resolve the hostname")

    monkeypatch.setattr(psutil, "net_if_addrs", interfaces)
    monkeypatch.setattr(socket, "getaddrinfo", unavailable_dns)
    assert main._non_loopback_interface_addresses() == expected


@pytest.mark.parametrize("probe_fails", [False, True])
async def test_lifespan_interface_probe_keeps_event_loop_responsive_and_warns(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, probe_fails: bool
) -> None:
    from app.core.config import settings
    from app.web import main

    _prepare_lifespan(monkeypatch)
    monkeypatch.setattr(settings, "admin_password", "")
    loop_responded = Event()
    loop = asyncio.get_running_loop()
    messages: list[str] = []

    def interfaces() -> dict[str, list[SimpleNamespace]]:
        loop.call_soon_threadsafe(loop_responded.set)
        assert loop_responded.wait(2), "interface inspection blocked the event loop"
        if probe_fails:
            raise OSError("interface enumeration unavailable")
        return {"test0": [SimpleNamespace(family=socket.AF_INET, address="192.0.2.5")]}

    def unavailable_dns(*args: object, **kwargs: object) -> None:
        raise AssertionError("startup must not resolve the hostname")

    monkeypatch.setattr(psutil, "net_if_addrs", interfaces)
    monkeypatch.setattr(socket, "getaddrinfo", unavailable_dns)
    sink = main.logger.add(messages.append, format="{message}", level="WARNING")
    try:
        async with main.lifespan(main.app):
            assert loop_responded.is_set()
    finally:
        main.logger.remove(sink)
    logged = "".join(messages)
    assert "Web 将禁止非本机请求访问 /api 路由" in logged
    assert ("无法枚举本机网络接口" if probe_fails else "192.0.2.5") in logged


@pytest.mark.parametrize("failed_service", ["trends", "plugins", "worker", "web_jobs", "recorders", "live_monitor"])
async def test_lifespan_cleanup_failure_still_stops_every_service(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, failed_service: str
) -> None:
    from app.web.main import app, lifespan

    stopped = _prepare_lifespan(monkeypatch, fail_stop=failed_service)
    with pytest.raises(RuntimeError, match=f"stop {failed_service} failed"):
        async with lifespan(app):
            pass
    assert stopped == ["live_monitor", "recorders", "web_jobs", "worker", "plugins", "trends", "metrics"]


@pytest.mark.parametrize(
    ("failed_service", "expected"),
    [
        ("plugins", ["plugins", "trends", "metrics"]),
        ("worker", ["worker", "plugins", "trends", "metrics"]),
        ("web_jobs", ["web_jobs", "worker", "plugins", "trends", "metrics"]),
        ("live_monitor", ["live_monitor", "recorders", "web_jobs", "worker", "plugins", "trends", "metrics"]),
    ],
)
async def test_partial_startup_failure_releases_registered_services(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, failed_service: str, expected: list[str]
) -> None:
    from app.web.main import app, lifespan

    stopped = _prepare_lifespan(monkeypatch, fail_start=failed_service)
    with pytest.raises(RuntimeError, match=f"start {failed_service} failed"):
        async with lifespan(app):
            pytest.fail("startup failure cannot reach serving state")
    assert stopped == expected


def test_metrics_collector_reuses_thread_and_joins_before_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    sampled = Event()
    monkeypatch.setattr(metrics, "snapshot", lambda: sampled.set())
    metrics.stop_metrics_collector()
    try:
        metrics.start_metrics_collector(interval_s=60)
        first = metrics._collector_thread
        assert first is not None and sampled.wait(2)
        metrics.start_metrics_collector(interval_s=60)
        assert metrics._collector_thread is first
        metrics.stop_metrics_collector()
        assert not first.is_alive()
        metrics.start_metrics_collector(interval_s=60)
        assert metrics._collector_thread is not first
    finally:
        metrics.stop_metrics_collector()


def test_metrics_shutdown_failure_preserves_live_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = Event()
    release = Event()

    def snapshot() -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(metrics, "snapshot", snapshot)
    metrics.stop_metrics_collector()
    try:
        metrics.start_metrics_collector()
        assert entered.wait(2)
        first = metrics._collector_thread
        with pytest.raises(RuntimeError, match="未在关闭期限内退出"):
            metrics.stop_metrics_collector(timeout_s=0.01)
        metrics.start_metrics_collector()
        assert metrics._collector_thread is first
    finally:
        release.set()
        metrics.stop_metrics_collector()


async def test_worker_loop_failure_still_releases_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.analysis.transcription import process_pool
    from app.pipeline import task_worker

    cleaned = Event()
    pool_stopped = Event()
    monkeypatch.setattr(
        process_pool, "get_process_pool", lambda: SimpleNamespace(shutdown=lambda grace: pool_stopped.set())
    )
    monkeypatch.setattr(task_worker, "cleanup_subprocesses", cleaned.set)

    async def failed() -> None:
        raise OSError("loop failed")

    worker = task_worker.TaskWorker()
    worker._main_task = asyncio.create_task(failed())
    await asyncio.sleep(0)
    with pytest.raises(ExceptionGroup, match="Worker 关闭期间发现异常"):
        await worker.stop()
    assert cleaned.is_set() and pool_stopped.is_set()


async def test_worker_shutdown_timeout_keeps_execution_and_rejects_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings
    from app.core.execution import run_sync
    from app.pipeline.task_worker import TaskWorker

    entered, release = Event(), Event()

    def compute() -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(settings, "worker_shutdown_timeout_seconds", 1)
    worker = TaskWorker()
    execution = asyncio.create_task(run_sync(compute))
    worker._analyzing.add(execution)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(ExceptionGroup, match="关闭期间发现异常") as raised:
            await worker.stop()
        assert "尚未物理退出" in str(raised.value.exceptions[0])
        assert not execution.done() and shutdown_event.is_set()
        with pytest.raises(RuntimeError, match="不能重新启动"):
            await worker.start()
        assert shutdown_event.is_set()
    finally:
        release.set()
        await execution
        await worker.stop()


def test_real_uvicorn_sigint_exits_with_active_media_job(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    env = dict(os.environ)
    env.update(
        DATABASE_URL=f"sqlite:///{(tmp_path / 'probe.db').as_posix()}",
        STORAGE_ROOT=str(tmp_path / "storage"),
        ASR_PRELOAD_ON_START="false",
        ADMIN_PASSWORD="",
        PYTHONUTF8="1",
    )
    script = r"""
import asyncio,signal,sys,time
from pathlib import Path
import psutil,uvicorn
from app.core.process_control import run_cancellable
from app.web.main import app
from app.web.services.background_jobs import web_job_manager
pid_file=Path(sys.argv[1])
child_code=(
    "import subprocess,sys,time;from pathlib import Path;"
    "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
    f"Path({str(pid_file)!r}).write_text(str(child.pid));time.sleep(300)"
)
def handler(context,payload):
    run_cancellable([sys.executable,'-c',child_code],capture_output=True,
                    timeout=30,cancel_check=context.cancelled)
web_job_manager.register('exit_probe',handler)
async def main():
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=0,log_level='warning'))
    async def request_shutdown():
        deadline=time.monotonic()+15
        while not server.started:
            if time.monotonic()>deadline: raise RuntimeError('server did not start')
            await asyncio.sleep(.02)
        await web_job_manager.enqueue('exit_probe',{},label='exit probe',owner='test')
        while not pid_file.exists():
            if time.monotonic()>deadline: raise RuntimeError('media job did not start')
            await asyncio.sleep(.02)
        signal.raise_signal(signal.SIGINT)
    await asyncio.gather(server.serve(),request_shutdown())
try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
assert pid_file.is_file()
pid=int(pid_file.read_text())
if psutil.pid_exists(pid):
    assert psutil.Process(pid).status()==psutil.STATUS_ZOMBIE, f'descendant still alive: {pid}'
print('EXIT_PROBE_COMPLETE')
"""
    try:
        result = run_cancellable(
            [sys.executable, "-c", script, str(pid_file)], env=env, capture_output=True, encoding="utf-8", timeout=35
        )
    except subprocess.TimeoutExpired as exc:
        exc.add_note(f"Uvicorn stdout: {exc.stdout!r}\nUvicorn stderr: {exc.stderr!r}")
        raise
    assert result.returncode == 0, result.stderr
    assert "EXIT_PROBE_COMPLETE" in str(result.stdout)


@pytest.mark.parametrize("initialized", [False, True])
async def test_docker_startup_reports_real_auth_mode(
    temp_db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initialized: bool
) -> None:
    from app.core.config import settings
    from app.core.docker_auth import AuthStore
    from app.web import main

    _prepare_lifespan(monkeypatch)
    monkeypatch.setattr(settings, "docker_auth_enabled", True)
    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setenv("BLC_APP_ROOT", str(tmp_path))
    store = AuthStore(tmp_path)
    code = store.boot("")
    if initialized:
        store.initialize(code, "administrator-test-password", "local")
    messages: list[str] = []
    sink = main.logger.add(messages.append, format="{message}", level="INFO")
    try:
        async with main.lifespan(main.app):
            pass
    finally:
        main.logger.remove(sink)
    text = "".join(messages)
    assert "Web 将禁止非本机请求" not in text
    assert ("网页登录已启用" if initialized else "等待使用一次性初始化码") in text
