"""诊断安全、根因保留及受控进程的真实失败回归。"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from loguru import logger
from sqlalchemy.exc import OperationalError
from sqlmodel import select

from app.core import logging as app_logging
from app.core.logging import _ApplicationLogHandler, _safe_record
from app.core.process_control import run_cancellable
from app.core.sanitize import safe_exception_summary, sanitize_diagnostic


def _nested_error() -> RuntimeError:
    try:
        raise TimeoutError("connect https://user:password@model.example/private?token=private-query")
    except TimeoutError as cause:
        try:
            raise RuntimeError("Authorization: Bearer private-header") from cause
        except RuntimeError as error:
            return error


def test_summary_retains_root_host_and_no_credentials() -> None:
    summary = safe_exception_summary(_nested_error())
    assert "RuntimeError" in summary and "TimeoutError" in summary and "model.example" in summary
    assert "private-" not in summary and "user:" not in summary
    assert sanitize_diagnostic(summary) == summary


def test_long_chain_keeps_innermost_root_and_limits_size() -> None:
    error: BaseException = PermissionError("model cache read denied")
    for _ in range(5):
        wrapper = RuntimeError("wrapper " + "x" * 3000)
        wrapper.__cause__ = error
        error = wrapper
    summary = safe_exception_summary(error)
    assert "PermissionError: model cache read denied" in summary
    assert len(summary) <= 2048


def test_database_error_does_not_render_bind_parameters() -> None:
    error = OperationalError("INSERT INTO settings VALUES (?)", ("arbitrary-private-key",), OSError("database locked"))
    summary = safe_exception_summary(error)
    assert "database locked" in summary and "OperationalError" in summary
    assert "arbitrary-private-key" not in summary and "INSERT" not in summary


def test_process_error_summary_omits_argv_preserves_stderr_tail() -> None:
    error = subprocess.CalledProcessError(
        17, ["uploader", "private-argv"], stderr="x" * 3000 + "\nPermission denied token=secret-value"
    )
    summary = safe_exception_summary(error)
    assert "exit_code=17" in summary and "Permission denied" in summary
    assert "private-argv" not in summary and "secret-value" not in summary


@pytest.mark.parametrize("configure", [False, True])
def test_loguru_file_and_database_keep_one_context_and_safe_trace(
    temp_db: None, tmp_path: Path, configure: bool
) -> None:
    from app.db.entities import SystemLog
    from app.db.session import get_session

    logger.complete()
    if configure:
        app_logging.setup_logging()
    database = None
    if not app_logging._CONFIGURED:
        database = logger.add(
            app_logging._DatabaseLogSink(), level="WARNING", enqueue=True, filter=app_logging._filter_db_sink
        )
    messages: list[str] = []
    handler = logger.add(messages.append, format="{message}", level="WARNING", diagnose=False)
    file_handler = logger.add(tmp_path / "diagnostic.log", format="{message}", level="WARNING", diagnose=False)
    try:
        logger.patch(_safe_record).bind(task_id=765, session_id=123).opt(exception=_nested_error()).warning(
            "load failed"
        )
    finally:
        logger.complete()
        logger.remove(handler)
        logger.remove(file_handler)
        if database is not None:
            logger.remove(database)
    with get_session() as db:
        row = db.exec(select(SystemLog).where(SystemLog.message.contains("task_id=765"))).one()
    for text in (messages[-1], row.message, (tmp_path / "diagnostic.log").read_text(encoding="utf-8")):
        assert text.count("task_id=765") == 1
        assert "TimeoutError" in text and "test_diagnostics.py:" in text
        assert "private-" not in text


def test_standard_logger_bridge_preserves_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[dict] = []
    monkeypatch.setattr(logger._core, "patcher", _safe_record)
    handler = logger.add(lambda message: records.append(message.record), level="WARNING")
    error = _nested_error()
    record = logging.LogRecord(
        "app.pipeline.example",
        logging.ERROR,
        __file__,
        97,
        "failure %s",
        ("operation",),
        (type(error), error, error.__traceback__),
        func="execute_task",
    )
    try:
        _ApplicationLogHandler().emit(record)
    finally:
        logger.remove(handler)
    assert records[-1]["name"] == "app.pipeline.example"
    assert records[-1]["function"] == "execute_task"
    assert records[-1]["line"] == 97
    assert "private-" not in record.getMessage()


def test_process_timeout_carries_partial_stderr() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_cancellable(
            [sys.executable, "-u", "-c", "import sys,time; print('decoder waiting',file=sys.stderr); time.sleep(30)"],
            capture_output=True,
            text=True,
            timeout=1,
            poll_interval_s=0.05,
        )
    assert "decoder waiting" in caught.value.stderr


@pytest.mark.asyncio
async def test_uvicorn_protocol_errors_remain_safe_after_logging_reconfiguration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from unittest.mock import Mock

    import h11
    import uvicorn
    from uvicorn._types import ASGIReceiveCallable, ASGISendCallable, Scope
    from uvicorn.protocols.http.flow_control import FlowControl
    from uvicorn.protocols.http.h11_impl import RequestResponseCycle

    from app.core import logging as log_module

    application_logger = logging.getLogger("app.pipeline.diagnostic_regression")
    names = set(logging.Logger.manager.loggerDict) | {"app", "uvicorn", "uvicorn.error", "uvicorn.access"}
    for name in names:
        standard_logger = logging.getLogger(name)
        monkeypatch.setattr(standard_logger, "handlers", list(standard_logger.handlers))
        monkeypatch.setattr(standard_logger, "level", standard_logger.level)
        monkeypatch.setattr(standard_logger, "disabled", standard_logger.disabled)
        monkeypatch.setattr(standard_logger, "propagate", standard_logger.propagate)
    monkeypatch.setattr(log_module, "_CONFIGURED", True)
    monkeypatch.setattr(logger._core, "patcher", _safe_record)
    log_module.setup_logging()  # CLI 已经初始化过日志。
    uvicorn.Config("app.web.main:app")  # 默认配置重新装入原始错误 handler。
    log_module.setup_logging()  # 应用 lifespan 必须恢复安全通道。
    assert not logging.getLogger("app").disabled
    assert not application_logger.disabled
    output = io.StringIO()
    sink = logger.add(output, format="{name}: {message}")
    transport = Mock(spec=asyncio.Transport)
    connection = h11.Connection(h11.SERVER)
    connection.receive_data(b"GET /diagnostic HTTP/1.1\r\nHost: localhost\r\n\r\n")
    connection.next_event()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/diagnostic",
        "query_string": b"",
        "headers": [],
        "http_version": "1.1",
    }
    cycle = RequestResponseCycle(
        scope=scope,
        conn=connection,
        transport=transport,
        flow=FlowControl(transport),
        logger=logging.getLogger("uvicorn.error"),
        access_logger=logging.getLogger("uvicorn.access"),
        access_log=False,
        default_headers=[],
        message_event=asyncio.Event(),
        on_response=lambda: None,
    )

    async def failing_app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        raise OperationalError("SELECT private-query", ("private-binding",), _nested_error())

    try:
        application_logger.warning("application logger survived reconfiguration")
        await cycle.run_asgi(failing_app)
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1",
            "GET",
            "/diagnostic?credential=private-query",
            "1.1",
            500,
        )
    finally:
        logger.remove(sink)
    text = output.getvalue()
    assert "uvicorn.error" in text and "OperationalError" in text and "TimeoutError" in text
    assert "h11_impl.py" in text and "model.example" in text
    assert "private-" not in text
    assert "application logger survived reconfiguration" in text
    assert cycle.response_complete and transport.write.called


def test_cancel_callback_failure_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import process_control

    processes: list[subprocess.Popen] = []
    original = subprocess.Popen

    def spawn(*args: object, **kwargs: object) -> subprocess.Popen:
        child = original(*args, **kwargs)
        processes.append(child)
        return child

    def failed_cancel() -> bool:
        raise ValueError("cancel state unavailable")

    monkeypatch.setattr(process_control.subprocess, "Popen", spawn)
    with pytest.raises(ValueError, match="cancel state unavailable"):
        run_cancellable([sys.executable, "-c", "import time; time.sleep(30)"], cancel_check=failed_cancel)
    assert processes[0].poll() is not None


def test_cancel_error_keeps_cleanup_failure_note_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from app.core import process_control
    from app.pipeline import lifecycle

    process = Mock(spec=subprocess.Popen)
    process.pid = -1
    process.poll.return_value = None
    process.terminate.side_effect = PermissionError("terminate denied token=private-cleanup")
    monkeypatch.setattr(process_control.subprocess, "Popen", lambda *args, **kwargs: process)
    tree = Mock(spec=process_control.ProcessTree)
    monkeypatch.setattr(process_control, "ProcessTree", lambda pid: tree)

    def failed_cancel() -> bool:
        raise ValueError("cancel state unavailable")

    try:
        with pytest.raises(ValueError) as caught:
            run_cancellable(["fake-process"], cancel_check=failed_cancel)
        summary = safe_exception_summary(caught.value)
        assert "cancel state unavailable" in summary and "PermissionError: terminate denied" in summary
        assert "private-cleanup" not in summary
        assert process in lifecycle._subprocesses
    finally:
        process.poll.return_value = 0
        lifecycle.cleanup_subprocesses()


def test_multi_gpu_resource_probe_uses_first_device(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import asr_detection

    monkeypatch.setattr(
        asr_detection.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "GPU A, 4000, 1000\nGPU B, 12000, 9000\n", ""),
    )
    info = asr_detection.detect_resources()
    assert info.gpu_available and info.gpu_name == "GPU A"
    assert info.vram_total_mb == 4000 and info.vram_free_mb == 1000


def test_metrics_failure_is_unknown_and_recovery_clears_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import metrics
    from app.pipeline import storage_lifecycle

    def unavailable() -> dict:
        raise PermissionError("disk permission denied token=private-token")

    monkeypatch.setattr(storage_lifecycle, "get_disk_usage", unavailable)
    failed = metrics.snapshot()
    assert failed.disk_free_gb is None and "PermissionError" in failed.disk_error
    assert "private-token" not in failed.disk_error
    monkeypatch.setattr(storage_lifecycle, "get_disk_usage", lambda: {"free_gb": 10.0})
    monkeypatch.setattr(storage_lifecycle, "get_directory_size", lambda path: 0.5)
    recovered = metrics.snapshot()
    assert recovered.disk_free_gb == 10.0 and recovered.disk_error is None


async def test_worker_releases_budget_when_claim_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import resource_budget
    from app.db.entities import TaskStatus
    from app.pipeline import task_worker

    budget = resource_budget.ResourceBudget()
    monkeypatch.setattr(resource_budget, "_budget", budget)
    baseline = budget.available()

    def broken_claim(stage: str) -> None:
        raise OSError("database unavailable")

    monkeypatch.setattr(task_worker, "pop_and_claim", broken_claim)
    task_worker.shutdown_event.clear()
    worker = task_worker.TaskWorker()
    worker._running = True
    with pytest.raises(OSError, match="database unavailable"):
        await worker._dispatch(TaskStatus.QUEUED_FOR_ANALYSIS, set(), 1)
    assert budget.available() == baseline
    monkeypatch.setattr(task_worker, "pop_and_claim", lambda stage: None)
    await worker._dispatch(TaskStatus.QUEUED_FOR_ANALYSIS, set(), 1)
    assert budget.available() == baseline


@pytest.mark.parametrize("response", ["429", "html", "array"])
def test_webhook_rejects_invalid_http_or_json(monkeypatch: pytest.MonkeyPatch, response: str) -> None:
    import httpx

    from app.notify import webhook

    monkeypatch.setattr(
        webhook.settings, "wecom_webhook", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=private-key"
    )
    request = httpx.Request("POST", webhook.settings.wecom_webhook)
    replies = {
        "429": httpx.Response(429, request=request, text="private-body"),
        "html": httpx.Response(200, request=request, text="<html>unavailable</html>"),
        "array": httpx.Response(200, request=request, json=[]),
    }
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: replies[response])
    assert webhook.send_wecom("test", "body") is False


def test_audio_extraction_has_timeout_and_keeps_root(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.analysis import audio

    def timeout(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        assert kwargs["timeout"] == 600 and callable(kwargs["cancel_check"])
        raise subprocess.TimeoutExpired(command, 600, stderr=b"decoder stuck token=private")

    monkeypatch.setattr(audio, "run_cancellable", timeout)
    with pytest.raises(RuntimeError) as caught:
        audio.extract_pcm("clip.ts")
    assert "decoder stuck" in str(caught.value) and "private" not in str(caught.value)
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)


@pytest.mark.parametrize("docker_state", ["disabled", "setup", "ready"])
def test_doctor_uses_current_settings_and_preserves_existing_files(
    temp_db: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docker_state: str
) -> None:
    import httpx
    from rich.console import Console

    from app.commands import doctor
    from app.core import asr_detection
    from app.core.config import settings

    storage = Path(settings.storage_root)
    storage.mkdir(parents=True, exist_ok=True)
    if docker_state != "disabled":
        from app.core.docker_auth import AuthStore

        monkeypatch.setattr(settings, "docker_auth_enabled", True)
        monkeypatch.setattr(settings, "admin_password", "")
        monkeypatch.setenv("BLC_APP_ROOT", str(tmp_path))
        auth = AuthStore(tmp_path)
        code = auth.boot("")
        if docker_state == "ready":
            auth.initialize(code, "administrator-test-password", "local")
    existing = storage / ".doctor_write_test"
    existing.write_text("user asset", encoding="utf-8")
    output = io.StringIO()
    monkeypatch.setattr(doctor, "console", Console(file=output, width=240, color_system=None))
    monkeypatch.setattr(settings, "ffmpeg_path", sys.executable)
    monkeypatch.setattr(settings, "ffprobe_path", sys.executable)
    monkeypatch.setattr(
        asr_detection,
        "detect_resources",
        lambda: asr_detection.ResourceInfo(gpu_available=True, vram_total_mb=8192, vram_free_mb=4000),
    )
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(200))
    doctor.cmd_doctor(yes=True)
    text = output.getvalue()
    assert "尚未创建" not in text and "8.0 GB VRAM" in text and "funasr_nano" in text
    assert "FFmpeg" in text and "数据库完整性" in text
    assert existing.read_text(encoding="utf-8") == "user asset"
    if docker_state != "disabled":
        assert "ADMIN_PASSWORD 为空" not in text
        assert ("尚未初始化" if docker_state == "setup" else "网页登录已启用") in text


def test_doctor_fails_explicit_cuda_and_caches_identical_backend_probes(
    temp_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import click
    import httpx
    from rich.console import Console

    from app.commands import doctor
    from app.core import asr_detection, asr_devices
    from app.core.config import settings

    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    output = io.StringIO()
    monkeypatch.setattr(doctor, "console", Console(file=output, width=240, color_system=None))
    monkeypatch.setattr(asr_detection, "detect_resources", lambda: asr_detection.ResourceInfo())
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(200))
    monkeypatch.setattr(settings, "asr_primary", "paraformer")
    monkeypatch.setattr(settings, "asr_sensevoice_enabled", True)
    monkeypatch.setattr(settings, "asr_sensevoice", True)
    monkeypatch.setattr(settings, "asr_funasr_review", True)
    monkeypatch.setattr(settings, "asr_fallback_whisper", False)
    for field in ("asr_primary_device", "asr_auxiliary_device", "asr_review_device"):
        monkeypatch.setattr(settings, field, "cuda")
    calls: list[tuple[str, str]] = []

    def probe(backend: str, device: str) -> dict[str, str | bool | None]:
        calls.append((backend, device))
        return {"ok": False, "version": "2.13.0+cpu", "cuda_build": None, "error": "CPU 版"}

    monkeypatch.setattr(asr_devices, "probe_device", probe)
    with pytest.raises(click.exceptions.Exit) as caught:
        doctor.cmd_doctor(yes=True)
    assert caught.value.exit_code == 1
    assert calls == [("torch", "cuda")]
    text = output.getvalue()
    assert all(f"ASR 设备 {role}" in text for role in ("primary", "auxiliary", "review"))
    assert "2.13.0+cpu" in text and "CPU 版" in text


def test_cli_failure_is_safe_and_nonzero() -> None:
    import click
    from click.testing import CliRunner

    from app.cli import _diagnostic_command

    cli = click.Group()

    @cli.command()
    @_diagnostic_command
    def fail() -> None:
        raise _nested_error()

    result = CliRunner().invoke(cli, ["fail"])
    assert result.exit_code == 1 and "TimeoutError" in result.output
    assert "private-" not in result.output


def test_doctor_summarizes_broken_database(temp_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import click
    import httpx
    from rich.console import Console

    from app.commands import doctor
    from app.core import asr_detection, settings_store
    from app.core.config import settings
    from app.db import schema

    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    output = io.StringIO()
    monkeypatch.setattr(doctor, "console", Console(file=output, width=240, color_system=None))
    monkeypatch.setattr(schema, "validate_schema", lambda: False)
    monkeypatch.setattr(asr_detection, "detect_resources", lambda: asr_detection.ResourceInfo())
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(200))

    def missing_table() -> bool:
        raise OperationalError("select * from app_settings", (), OSError("no such table: app_settings"))

    monkeypatch.setattr(settings_store, "biliup_enabled", missing_table)
    with pytest.raises(click.exceptions.Exit) as caught:
        doctor.cmd_doctor(yes=True)
    assert caught.value.exit_code == 1
    assert "no such table" in output.getvalue() and "存在关键问题需修复" in output.getvalue()


def test_disk_probe_never_substitutes_another_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pipeline.storage_lifecycle import get_disk_usage

    def denied(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("target volume denied")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(PermissionError, match="target volume denied"):
        get_disk_usage(tmp_path / "missing")
