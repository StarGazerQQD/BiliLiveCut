"""CLI commands behavioral tests — validates ALL_COMMANDS structure and Click app wiring."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from tests.source_fixtures import bind_bilibili_room


def test_all_commands_count_and_structure() -> None:
    """ALL_COMMANDS list has expected entries; each entry has (name, func, help)."""
    from app.commands import ALL_COMMANDS

    assert len(ALL_COMMANDS) >= 4
    for cmd_name, cmd_func, _help_text in ALL_COMMANDS:
        assert isinstance(cmd_name, str)
        assert callable(cmd_func)


def test_cli_app_version_present() -> None:
    """app.cli has version command and __version__."""
    from app import __version__

    assert __version__.startswith("0.1")


def test_cli_module_entrypoint_dispatches_commands() -> None:
    """``python -m app.cli`` 必须执行 Click，而不是静默退出。"""
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "BiliLiveCut 0.1.20.1-alpha" in result.stdout


def test_cli_help_preserves_dependency_hints_and_current_doctor_text() -> None:
    """Click/Rich 不得吞掉可选依赖名称或显示过期 Doctor 版本。"""
    from app.cli import app

    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "asr 可选依赖" in result.output
    assert "web 可选依赖" in result.output
    assert "V0.1.13" not in result.output


def test_click_entrypoint_preserves_all_historical_commands() -> None:
    from app.cli import app

    assert isinstance(app, click.Group)
    historical = {
        "init",
        "add-room",
        "list-rooms",
        "check",
        "record",
        "transcribe",
        "score",
        "process",
        "list-candidates",
        "clip",
        "copywrite",
        "produce",
        "list-clips",
        "serve",
        "console",
        "doctor",
        "db-reset",
        "trends-purge",
        "set-upload",
        "llm-list",
        "llm-test",
        "schedule",
        "upload",
        "trends-collect",
        "trends-list",
        "trends-keywords",
        "version",
    }
    assert historical | {"backend", "frontend", "start"} == set(app.commands)


def test_every_command_help_works_without_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import cli
    from app.db import session

    def forbidden() -> None:
        raise AssertionError("help must not initialize the database")

    monkeypatch.setattr(session, "init_db", forbidden)
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    for name in cli.app.commands:
        result = CliRunner().invoke(cli.app, [name, "--help"])
        assert result.exit_code == 0, (name, result.output)
        assert ":param" not in result.output


@pytest.mark.parametrize(
    "command,arguments,expected",
    [
        ("record", ["7"], {"db_id": 7, "pipeline": None, "produce": False}),
        ("record", ["7", "--pipeline", "--produce"], {"db_id": 7, "pipeline": True, "produce": True}),
        ("record", ["7", "--no-pipeline"], {"db_id": 7, "pipeline": False, "produce": False}),
        ("set-upload", [], {"biliup": None, "auto": None}),
        ("set-upload", ["--no-biliup", "--auto"], {"biliup": False, "auto": True}),
        ("set-upload", ["--biliup", "--no-auto"], {"biliup": True, "auto": False}),
        ("schedule", ["7", "--at", "20:30", "--daily"], {"room_id": 7, "at_time": "20:30", "daily": True}),
        ("trends-purge", [], {"days": None}),
        ("trends-purge", ["--days", "14"], {"days": 14}),
        ("db-reset", ["-y"], {"yes": True}),
        ("list-clips", ["--limit", "10"], {"limit": 10}),
        (
            "add-room",
            ["123", "--authorize", "--platform", "bilibili"],
            {"url": "123", "authorize": True, "platform": "bilibili"},
        ),
        ("serve", ["--port", "8002", "--reload"], {"host": "127.0.0.1", "port": 8002, "reload": True}),
        (
            "console",
            ["--port", "8003", "--frontend-dir", "dist"],
            {"host": "127.0.0.1", "port": 8003, "frontend_dir": "dist"},
        ),
    ],
)
def test_command_argument_compatibility(
    monkeypatch: pytest.MonkeyPatch, command: str, arguments: list[str], expected: dict[str, object]
) -> None:
    from app import cli
    from app.db import session

    captured = {}
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr(session, "init_db", lambda: None)
    monkeypatch.setattr(cli.app.commands[command], "callback", lambda **kwargs: captured.update(kwargs))
    result = CliRunner().invoke(cli.app, [command, *arguments])
    assert result.exit_code == 0, result.output
    assert captured == expected


@pytest.mark.parametrize(
    "arguments",
    [["record"], ["record", "abc"], ["schedule", "7"], ["backend", "--port", "0"], ["frontend", "--port", "65536"]],
)
def test_invalid_arguments_fail_before_database_initialization(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    from app import cli
    from app.db import session

    def forbidden() -> None:
        raise AssertionError("invalid arguments must not initialize the database")

    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr(session, "init_db", forbidden)
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize("command", ["frontend", "start", "console"])
def test_built_frontend_required_before_starting_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from app import cli

    def forbidden(*args, **kwargs):
        raise AssertionError("missing frontend must not start a process")

    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = CliRunner().invoke(cli.app, [command, "--frontend-dir", str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "未找到前端构建" in result.output and "npm --prefix frontend run build" in result.output


def test_installed_frontend_selection_keeps_explicit_directory_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.commands import serve

    monkeypatch.delenv("BLC_SOURCE_DIR", raising=False)
    monkeypatch.delenv("BLC_FRONTEND_DIR", raising=False)
    monkeypatch.setattr(serve, "__file__", str(tmp_path / "app/commands/serve.py"))
    packaged = tmp_path / "app/web/ui"
    packaged.mkdir(parents=True)
    (packaged / "index.html").write_text("wheel frontend")
    assert serve._frontend_directory(None) == packaged
    # 显式目录缺失时不能静默改用 wheel 自带资源。
    with pytest.raises(click.BadParameter, match="未找到前端构建"):
        serve._frontend_directory(str(tmp_path / "missing"))
    portable = tmp_path / "portable/frontend/dist"
    portable.mkdir(parents=True)
    (portable / "index.html").write_text("portable frontend")
    monkeypatch.setenv("BLC_SOURCE_DIR", str(tmp_path / "portable"))
    assert serve._frontend_directory(None) == portable
    monkeypatch.setenv("BLC_FRONTEND_DIR", str(packaged))
    assert serve._frontend_directory(None) == packaged


@pytest.mark.parametrize(
    "backend",
    [
        "https://localhost:8000",
        "http://localhost/api",
        "http://user:secret@localhost",
        "http://localhost:abc",
        "http://localhost:0",
    ],
)
def test_frontend_rejects_invalid_upstream_before_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    from app import cli
    from app.web import frontend_server

    (tmp_path / "index.html").write_text("<main>built app</main>")
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr(frontend_server, "serve_frontend", lambda *args: pytest.fail("must not start frontend"))
    result = CliRunner().invoke(cli.app, ["frontend", "--frontend-dir", str(tmp_path), "--backend", backend])
    assert result.exit_code == 2, result.output
    assert "secret" not in result.output


@pytest.mark.parametrize("entrypoint", ["module", "installed"])
@pytest.mark.parametrize("arguments,exit_code", [(["--help"], 0), (["未知命令"], 2)])
def test_cli_entrypoints_handle_chinese_with_cp1252_streams(
    entrypoint: str, arguments: list[str], exit_code: int
) -> None:
    command = [sys.executable, "-m", "app.cli"]
    if entrypoint == "installed":
        command = [
            sys.executable,
            "-c",
            "from importlib.metadata import distribution; "
            "next(e for e in distribution('bili-live-cut').entry_points "
            "if e.group == 'console_scripts' and e.name == 'blc').load()()",
        ]
    result = subprocess.run(
        [*command, *arguments],
        env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252", "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        timeout=60,
    )
    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == exit_code, output
    assert "UnicodeEncodeError" not in output
    assert ("直播实时切片系统" if exit_code == 0 else "未知命令") in output


def test_serve_exports_actual_cli_port_to_application(monkeypatch) -> None:  # noqa: ANN001
    """直接 CLI 的显式端口必须成为设置 API 看到的本次实际端口。"""
    import uvicorn

    from app.commands.serve import cmd_serve

    captured: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.delenv("BLC_APP_ROOT", raising=False)
    monkeypatch.delenv("BLC_WEB_PORT", raising=False)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    cmd_serve(host="127.0.0.1", port=8080, reload=False)

    assert captured == {
        "app": "app.web.main:app",
        "host": "127.0.0.1",
        "port": 8080,
        "reload": False,
        "log_config": None,
        "timeout_graceful_shutdown": 10,
    }
    assert os.environ["BLC_WEB_PORT"] == "8080"
    assert os.environ["BLC_APP_ROOT"] == os.getcwd()


def test_backend_reload_watches_application_sources_independent_of_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """开发重载只监听业务和配置源码，不把测试、打包目录作为监听根。"""
    import uvicorn

    from app.commands import serve

    captured: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    serve.cmd_serve(host="127.0.0.1", port=8000, reload=True)

    root = Path(serve.__file__).resolve().parents[2]
    assert captured["reload_dirs"] == [str(root / "app"), str(root / "config")]
    assert captured["reload"] is True
    assert captured["timeout_graceful_shutdown"] == 10


def test_record_pipeline_default_persists_scheduler_switches(
    temp_db: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CLI 未显式传参时应读取全局默认值并把 db_id 传给回调。"""
    from app.commands import record as record_cmd
    from app.db.entities import LiveRoom
    from app.db.session import get_session

    with get_session() as db:
        room = LiveRoom(input_url="https://live.bilibili.com/1", room_id=1, authorized=True)
        db.add(room)
        db.flush()
        bind_bilibili_room(db, room)
        db_id = room.id

    callback_args: dict[str, object] = {}

    def fake_callback(**kwargs):  # noqa: ANN003, ANN202
        callback_args.update(kwargs)

        async def _callback(_segment) -> None:  # noqa: ANN001
            return None

        return _callback

    async def fake_record(self: record_cmd.Recorder) -> None:
        assert self.source_room.platform == "bilibili" and self.source_room.source_id == "1"

    from app.plugins.manager import PluginManager
    from app.sources.registry import source_registry

    monkeypatch.setattr(
        "app.plugins.runtime.plugin_manager", PluginManager(tmp_path / "plugin", registry=source_registry)
    )

    monkeypatch.setattr("app.pipeline.orchestrator.make_pipeline_callback", fake_callback)
    monkeypatch.setattr("app.core.settings_store.recording_pipeline_enabled", lambda: True)
    monkeypatch.setattr(record_cmd.Recorder, "run", fake_record)

    record_cmd.cmd_record(db_id, pipeline=None, produce=True)

    with get_session() as db:
        updated = db.get(LiveRoom, db_id)
        assert updated is not None
        assert updated.auto_analyze is True
        assert updated.auto_render is True
    assert callback_args == {"produce": True, "room_id": db_id}


def test_record_rejects_produce_without_pipeline(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """避免接受永远不会生效的 ``--produce`` 组合。"""
    from app.commands.record import cmd_record
    from app.plugins.manager import PluginManager

    monkeypatch.setattr("app.plugins.runtime.plugin_manager", PluginManager(tmp_path / "plugin"))
    try:
        cmd_record(1, pipeline=False, produce=True)
    except click.exceptions.Exit as exc:
        assert exc.exit_code == 1
    else:  # pragma: no cover - 明确表达必须抛错
        raise AssertionError("--produce without --pipeline must fail")


def test_console_isolates_terminal_signals_and_waits_for_backend_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import time
    import urllib.request
    from contextlib import nullcontext
    from types import SimpleNamespace

    from app.analysis.transcription import process_tree
    from app.commands.serve import cmd_console

    (tmp_path / "index.html").write_text("frontend")
    processes = []
    options = []
    ownership = []
    original_signals = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

    class Process:
        returncode = None

        def __init__(self) -> None:
            self.pid = len(processes) + 1
            self.stopping = False
            self.waits = 0
            self.stdin = None

        def poll(self):
            if self.stopping:
                self.waits += 1
                if self.waits > 5:
                    self.returncode = 0
            return self.returncode

        def send_signal(self, number):
            assert number == signal.SIGINT
            self.stopping = True

        def terminate(self):
            assert self.stdin is None, "Windows 后端须通过管道正常退出"
            self.stopping = True

        def kill(self):
            pytest.fail("正常关闭不能由启动器提前强杀后端")

        def wait(self, timeout=None):
            assert timeout is None, "服务必须有机会完成各自配置的关闭宽限"
            assert self.returncode == 0
            return 0

    def spawn(*args, **kwargs):
        process = Process()
        if kwargs.get("stdin") == subprocess.PIPE:
            process.stdin = SimpleNamespace(close=lambda: setattr(process, "stopping", True))
        processes.append(process)
        options.append(kwargs)
        return process

    def sleep(seconds):
        if seconds == 0.25:
            raise KeyboardInterrupt

    class SystemOwnership:
        # 本测试控制外部进程与 Job 边界，真实进程树清理由集成测试验证。
        def __init__(self, pid: int) -> None:
            assert pid == processes[-1].pid
            ownership.append("attach")

        def resume_suspended(self) -> None:
            assert options[-1]["creationflags"] & 0x00000004
            ownership.append("resume")

        def terminate(self) -> None:
            ownership.append("terminate")

    monkeypatch.setattr(process_tree, "ProcessTree", SystemOwnership)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: nullcontext(SimpleNamespace(status=200))),
    )
    cmd_console(frontend_dir=str(tmp_path), port=8080)
    assert len(processes) == 2
    assert ownership == (["attach", "resume", "terminate"] if os.name == "nt" else [])
    for kwargs in options:
        if os.name == "nt":
            assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            assert kwargs["start_new_session"] is True
    assert all(signal.getsignal(number) == handler for number, handler in original_signals.items())


def test_windows_console_stops_without_console_signals() -> None:
    from types import SimpleNamespace

    from app.commands.serve import _request_console_stop

    stopped = []
    backend = SimpleNamespace(poll=lambda: None, stdin=SimpleNamespace(close=lambda: stopped.append("backend-eof")))
    frontend = SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append("frontend"))
    _request_console_stop([backend, frontend], windows=True)
    assert stopped == ["backend-eof", "frontend"]
