"""CLI 子命令 — Web 服务。"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from threading import Event
from types import ModuleType

import click
from loguru import logger
from rich.console import Console

from app.core.sanitize import safe_exception_summary
from config.launcher_settings import (
    APP_ROOT_ENV,
    WEB_PORT_ENV,
    LauncherConfigError,
    load_launcher_config,
    runtime_app_root,
    validate_web_port,
)
from config.service_restart import RestartBudget

console = Console()


def _wait_parent_pipe() -> None:
    """等待父进程退出，避免同步管道读取阻塞 Windows 原生库初始化。"""
    import sys

    descriptor = sys.stdin.fileno()
    if os.name != "nt":
        os.read(descriptor, 1)
        return
    import ctypes
    import msvcrt
    import time
    from ctypes import wintypes

    peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
    pointer = ctypes.POINTER(wintypes.DWORD)
    peek.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, pointer, pointer, pointer]
    peek.restype = wintypes.BOOL
    handle, available = msvcrt.get_osfhandle(descriptor), wintypes.DWORD()
    # 数值库 DLL 初始化也会探测 stdin；不能用挂起的 ReadFile 占住同步管道。
    while True:
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error == 109:  # ERROR_BROKEN_PIPE：父进程已关闭写端。
                return
            raise ctypes.WinError(error)
        if available.value:
            return
        time.sleep(0.1)


def _serve_with_parent_pipe(uvicorn: ModuleType, **options: object) -> None:
    """受监管后端通过父进程管道正常退出，兼容没有控制台的 Windows 服务。"""
    _serve_owned(uvicorn, parent_pipe=True, **options)


def _serve_owned(uvicorn: ModuleType, *, parent_pipe: bool, **options: object) -> None:
    """独立后端拥有硬退出权限，HTTP 与应用收尾共享实际关闭时的预算。"""
    import threading
    import time

    from app.analysis.transcription.process_tree import ProcessTree
    from app.core.config import settings
    from app.core.supervisor import process_shutdown_control, standalone_process

    control = process_shutdown_control()
    if control is None:
        with standalone_process():
            return _serve_owned(uvicorn, parent_pipe=parent_pipe, **options)

    stop_requested = threading.Event()
    pipe_error: OSError | ValueError | None = None
    server = None
    # Job 句柄持有至独立 API 进程结束，覆盖录制器及所有后续创建的后代。
    owned_tree = ProcessTree(os.getpid()) if os.name == "nt" else None

    def begin_shutdown() -> None:
        control.begin(settings.worker_shutdown_timeout_seconds)

    def announce_deadline() -> None:
        try:
            os.write(2, "后端正在关闭；总期限到达仍未退出时将终止所属服务进程，未完成结果保留为待恢复状态。\n".encode())
        except OSError:
            # 诊断管道可能已关闭；硬退出仍由独立线程执行。
            return

    def enforce_exit() -> None:
        while not control.started.wait(0.05):
            if control.finished.is_set():
                return
            if server is not None and server.should_exit:
                begin_shutdown()
        assert control.expires_at is not None
        # stderr 写入也可能永久阻塞，不能占用共同关闭期限的执行线程。
        try:
            threading.Thread(target=announce_deadline, name="blc-exit-diagnostic", daemon=True).start()
        except (OSError, RuntimeError):
            # 诊断线程无法创建时仍按原期限退出，不能再次依赖日志或新增线程。
            pass
        if not control.finished.wait(max(0.0, control.expires_at - time.monotonic())):
            os._exit(1)

    threading.Thread(target=enforce_exit, name="blc-process-exit", daemon=True).start()

    class ParentPipeServer(uvicorn.Server):
        async def on_tick(self, counter: int) -> bool:
            # 仅在服务主循环中置退出标记，启动期间收到 EOF 也会执行 lifespan 清理。
            if stop_requested.is_set():
                self.should_exit = True
            return await super().on_tick(counter)

        async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
            begin_shutdown()
            from app.core.cancellation import shutdown_event
            from app.core.supervisor import remaining_shutdown

            shutdown_event.set()
            self.config.timeout_graceful_shutdown = remaining_shutdown(10)
            await super().shutdown(sockets)

    def watch_parent() -> None:
        nonlocal pipe_error
        try:
            _wait_parent_pipe()
        except (OSError, ValueError) as exc:
            pipe_error = exc
        begin_shutdown()
        stop_requested.set()

    if parent_pipe:
        threading.Thread(target=watch_parent, name="blc-parent-pipe", daemon=True).start()
    server = ParentPipeServer(uvicorn.Config("app.web.main:app", **options))
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        lifespan = getattr(server, "lifespan", None)
        if lifespan is None or not lifespan.shutdown_failed:
            control.finished.set()
        else:
            begin_shutdown()
    if owned_tree is not None:
        # 不调用 terminate：该 Job 包含当前进程，OS 会在最终退出时关闭句柄。
        assert owned_tree.pid == os.getpid()
    if lifespan is not None and (lifespan.startup_failed or lifespan.shutdown_failed):
        raise click.ClickException("后端生命周期未成功完成，请查看服务日志。")
    if pipe_error is not None:
        logger.error("父进程监管管道异常：{}", safe_exception_summary(pipe_error))
        raise click.ClickException("父进程监管管道异常，后端已完成关闭，请查看服务日志。") from pipe_error


def _request_console_stop(processes: list, *, windows: bool) -> None:
    """后端正常收尾；Windows 静态前端不依赖控制台信号即可关闭。"""
    import signal

    for index, process in enumerate(processes):
        if process.poll() is not None:
            continue
        try:
            if index == 0 and process.stdin is not None:
                process.stdin.close()
            elif windows:
                process.terminate()
            else:
                process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass


@click.option("--host", default="127.0.0.1", type=str, help="监听地址", show_default=True)
@click.option(
    "--port",
    default=None,
    type=click.IntRange(1, 65535),
    help="监听端口，默认使用已保存的 Launcher 配置",
    show_default=True,
)
@click.option("--reload", default=False, is_flag=True, help="开发热重载", show_default=True)
def cmd_serve(
    host: str = "127.0.0.1",
    port: int | None = None,
    reload: bool = False,
) -> None:
    """启动 API 服务（需安装 web 可选依赖）。

    页面由独立前端服务提供。

    :param host: 监听地址。
    :param port: 监听端口。
    :param reload: 是否开启热重载。
    """
    try:
        import uvicorn
    except ImportError as exc:
        console.print('[red]未安装 Web 依赖。请执行: pip install -e ".[web]"[/red]')
        raise click.exceptions.Exit(code=1) from exc

    try:
        actual_port = validate_web_port(port if port is not None else load_launcher_config(runtime_app_root()).web_port)
    except LauncherConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise click.exceptions.Exit(code=1) from exc
    os.environ[WEB_PORT_ENV] = str(actual_port)
    os.environ.setdefault(APP_ROOT_ENV, str(os.getcwd()))

    # P0: non-loopback requires password
    from app.core.config import settings as _srv_cfg

    if _srv_cfg.docker_auth_enabled:
        from app.core.docker_auth import AuthStore

        # 认证库必须由容器入口先初始化，否则不允许空密码对外监听。
        AuthStore(runtime_app_root()).state()
    if not _srv_cfg.admin_password and not _srv_cfg.docker_auth_enabled:
        from ipaddress import ip_address

        try:
            addr = ip_address(host.replace("localhost", "127.0.0.1"))
            if not addr.is_loopback:
                console.print(
                    "[red]拒绝启动 Web 管理后台：\n"
                    "当前监听地址不是本地地址，但 ADMIN_PASSWORD 为空。\n"
                    "请设置管理员密码或改为 127.0.0.1。[/red]"
                )
                raise click.exceptions.Exit(code=1)
        except ValueError:
            # non-IP hostname (like docker), also require password
            console.print(
                "[red]拒绝启动 Web 管理后台：\n"
                "当前监听地址不是本地地址，但 ADMIN_PASSWORD 为空。\n"
                "请设置管理员密码或改为 127.0.0.1。[/red]"
            )
            raise click.exceptions.Exit(code=1) from None

    console.print(f"[green]API 启动中[/green] -> http://{host}:{actual_port}")
    reload_options = {}
    if reload:
        source_root = Path(__file__).resolve().parents[2]
        reload_options["reload_dirs"] = [str(source_root / "app"), str(source_root / "config")]
    if os.environ.pop("_BLC_PARENT_STDIN", "") == "1" and not reload:
        _serve_with_parent_pipe(uvicorn, host=host, port=actual_port, log_config=None, timeout_graceful_shutdown=10)
    else:
        from app.core.supervisor import process_shutdown_control

        if process_shutdown_control() is not None and not reload:
            _serve_owned(uvicorn, parent_pipe=False, host=host, port=actual_port, log_config=None)
            return
        # 先结束 HTTP 长连接，再进入 lifespan；媒体执行仍由 Supervisor 等待真实退出。
        uvicorn.run(
            "app.web.main:app",
            host=host,
            port=actual_port,
            reload=reload,
            log_config=None,
            timeout_graceful_shutdown=10,
            **reload_options,
        )


# 注册列表
def _frontend_directory(frontend_dir: str | None) -> Path:
    """定位显式目录、Portable、wheel 或源码构建，不在运行时调用 npm。"""
    override = frontend_dir if frontend_dir is not None else os.environ.get("BLC_FRONTEND_DIR")
    if override is not None:
        directory = Path(override)
    elif source_dir := os.environ.get("BLC_SOURCE_DIR"):
        directory = Path(source_dir) / "frontend" / "dist"
    else:
        packaged = Path(__file__).resolve().parents[1] / "web" / "ui"
        directory = (
            packaged
            if (packaged / "index.html").is_file()
            else Path(__file__).resolve().parents[2] / "frontend" / "dist"
        )
    directory = directory.resolve()
    if not (directory / "index.html").is_file():
        raise click.BadParameter(
            f"未找到前端构建: {directory}。请先执行 npm --prefix frontend ci 和 npm --prefix frontend run build。",
            param_hint="--frontend-dir",
        )
    return directory


@click.option("--host", default="127.0.0.1", show_default=True, help="前端监听地址")
@click.option("--port", default=8080, type=click.IntRange(1, 65535), show_default=True, help="前端监听端口")
@click.option("--frontend-dir", type=click.Path(file_okay=False), help="前端构建目录，默认 frontend/dist")
@click.option("--backend", default="http://127.0.0.1:8000", show_default=True, help="反向代理的后端 HTTP 地址")
def cmd_frontend(
    host: str = "127.0.0.1",
    port: int = 8080,
    frontend_dir: str | None = None,
    backend: str = "http://127.0.0.1:8000",
) -> None:
    """单独启动已构建的前端，并把 API 请求代理到指定后端。

    不启动后端、不初始化数据库；后端不可用时 API 请求返回 502。
    开发热更新请在 frontend 目录执行 npm run dev。
    """
    from app.web.frontend_server import serve_frontend, validate_backend

    directory = _frontend_directory(frontend_dir)
    try:
        validate_backend(backend)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--backend") from None
    serve_frontend(directory, backend, host, port)


@click.option("--host", default="127.0.0.1", type=str, help="前端监听地址", show_default=True)
@click.option(
    "--port", default=None, type=click.IntRange(1, 65535), help="前端端口，默认使用 Launcher 配置", show_default=True
)
@click.option("--frontend-dir", default=None, type=str, help="独立前端构建目录（frontend/dist）", show_default=True)
def cmd_console(
    host: str = "127.0.0.1",
    port: int | None = None,
    frontend_dir: str | None = None,
) -> None:
    """同时启动独立前端进程和仅监听本机的 API 进程。

    :param host: 前端监听地址。
    :param port: 前端监听端口。
    :param frontend_dir: 已构建的静态资源目录。
    """
    import threading
    import time

    from config.service_restart import CLEANUP_UNCONFIRMED_EXIT, RESTART_OWNER_ENV

    parent_stopped = threading.Event()
    parent_error: OSError | ValueError | None = None

    def watch_parent() -> None:
        nonlocal parent_error
        try:
            _wait_parent_pipe()
        except (OSError, ValueError) as exc:
            parent_error = exc
        parent_stopped.set()

    if os.environ.pop("_BLC_PARENT_STDIN", "") == "1":
        threading.Thread(target=watch_parent, name="blc-console-parent", daemon=True).start()
    externally_supervised = os.environ.get(RESTART_OWNER_ENV) == "launcher"
    budget = RestartBudget()
    while not parent_stopped.is_set():
        try:
            _console_generation(host, port, frontend_dir, parent_stopped, budget)
            break
        except ExceptionGroup as exc:
            logger.opt(exception=exc).error("所属服务清理未确认，禁止自动重启")
            raise click.exceptions.Exit(CLEANUP_UNCONFIRMED_EXIT) from exc
        except (click.exceptions.Exit, OSError, RuntimeError) as exc:
            if isinstance(exc, click.exceptions.Exit) and exc.exit_code == 0:
                break
            if parent_stopped.is_set():
                break
            if externally_supervised:
                raise
            delay = budget.failed(time.monotonic())
            if delay is None:
                raise click.ClickException("前后端连续异常退出，自动恢复预算已耗尽，请检查服务日志。") from exc
            logger.opt(exception=exc).warning("前后端异常退出，{} 秒后恢复，第 {} 次", delay, budget.failures)
            try:
                if parent_stopped.wait(delay):
                    break
            except KeyboardInterrupt:
                break
    if parent_error is not None:
        raise click.ClickException("Launcher 监管管道异常，所属服务已关闭。") from parent_error


def _console_generation(
    host: str,
    port: int | None,
    frontend_dir: str | None,
    parent_stopped: Event,
    budget: RestartBudget,
) -> None:
    """运行一代前后端；所有所属进程确认退出后才返回给唯一重启监管者。"""
    import signal
    import subprocess
    import sys
    import time

    import psutil

    from app.analysis.transcription.process_tree import ProcessTree
    from app.core.process_spawn import spawn_owned
    from config.service_restart import OUTER_SHUTDOWN_LIMIT_S

    directory = _frontend_directory(frontend_dir)
    actual_port = validate_web_port(port if port is not None else load_launcher_config(runtime_app_root()).web_port)
    # 对外发布独立前端同样要求后台密码，不能借助回环代理绕过既有访问边界。
    from app.core.config import settings

    if (
        host not in {"127.0.0.1", "::1", "localhost"}
        and not settings.admin_password
        and not settings.docker_auth_enabled
    ):
        raise click.BadParameter("对外监听需要配置 ADMIN_PASSWORD。")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        api_port = probe.getsockname()[1]
    child_env = {**os.environ, "BLC_FRONTEND_PORT": str(actual_port)}
    child_env["_BLC_PARENT_STDIN"] = "1"
    processes: list[subprocess.Popen] = []
    owned: list[tuple[subprocess.Popen, ProcessTree]] = []
    shutdown_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        shutdown_signals.append(signal.SIGBREAK)
    previous_signals = {number: signal.getsignal(number) for number in shutdown_signals}
    closing = False
    force_exit = False
    process_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}

    def terminate(_number, _frame):
        nonlocal force_exit
        if closing:
            force_exit = True
            return
        if _number == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(0)

    for number in shutdown_signals:
        signal.signal(number, terminate)
    try:
        owned.append(
            spawn_owned(
                [sys.executable, "-m", "app.cli", "backend", "--host", "127.0.0.1", "--port", str(api_port)],
                env=child_env,
                stdin=subprocess.PIPE,
                **process_options,
            )
        )
        processes.append(owned[-1][0])
        from urllib.error import URLError
        from urllib.request import ProxyHandler, build_opener

        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 60
        while True:
            if parent_stopped.is_set():
                raise KeyboardInterrupt
            if processes[0].poll() is not None:
                raise RuntimeError("API 启动失败，请检查上方后端日志。")
            try:
                with opener.open(f"http://127.0.0.1:{api_port}/auth/status", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("等待 API 启动超时，请检查数据库与后端配置。")
            time.sleep(0.1)
        owned.append(
            spawn_owned(
                [
                    sys.executable,
                    "-m",
                    "app.cli",
                    "frontend",
                    "--frontend-dir",
                    str(directory),
                    "--backend",
                    f"http://127.0.0.1:{api_port}",
                    "--host",
                    host,
                    "--port",
                    str(actual_port),
                ],
                **process_options,
            )
        )
        processes.append(owned[-1][0])
        console.print(f"[green]工作台启动中[/green] -> http://{host}:{actual_port}")
        ready_deadline = time.monotonic() + 60
        ready_once = False
        next_probe = 0.0
        probe_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        probe_host = f"[{probe_host}]" if ":" in probe_host else probe_host
        while not parent_stopped.is_set() and all(process.poll() is None for process in processes):
            if time.monotonic() >= next_probe:
                ready = False
                try:
                    with opener.open(f"http://{probe_host}:{actual_port}/auth/status", timeout=1) as response:
                        ready = response.status == 200
                except (OSError, URLError):
                    pass
                budget.observe(ready=ready, now=time.monotonic())
                if ready and not ready_once:
                    logger.info("前后端已就绪，录制恢复清算已完成")
                ready_once |= ready
                if not ready_once and time.monotonic() >= ready_deadline:
                    raise RuntimeError("独立前端未能通过 API 就绪检查")
                next_probe = time.monotonic() + 1
            time.sleep(0.25)
        code = next(
            (process.returncode for process in processes if process.returncode is not None),
            0 if parent_stopped.is_set() else 1,
        )
        if code:
            raise click.exceptions.Exit(code=code if code > 0 else 1)
    except KeyboardInterrupt:
        pass
    finally:
        budget.ended(time.monotonic())
        closing = True
        try:
            console.print("[yellow]正在停止录制并释放后台资源，再次按 Ctrl+C 可强制退出。[/yellow]")
            cleanup_errors: list[Exception] = []
            try:
                _request_console_stop(processes, windows=os.name == "nt")
            except (OSError, ValueError) as exc:
                cleanup_errors.append(exc)
                force_exit = True
            for _process, tree in owned[1:]:
                try:
                    tree.terminate()
                except (OSError, RuntimeError, psutil.Error) as exc:
                    cleanup_errors.append(exc)
            # API 按当前已发布配置执行有限关闭；父层只等待所属进程，不另猜业务预算。
            shutdown_deadline = time.monotonic() + OUTER_SHUTDOWN_LIMIT_S
            while processes and processes[0].poll() is None:
                if force_exit or time.monotonic() >= shutdown_deadline:
                    console.print("[yellow]强制终止前后端及其子进程。[/yellow]")
                    break
                time.sleep(0.1)
            for process, tree in owned:
                try:
                    tree.terminate()
                except (OSError, RuntimeError, psutil.Error) as exc:
                    cleanup_errors.append(exc)
                try:
                    process.wait(timeout=5)
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                raise ExceptionGroup("前后端进程清理未全部确认", cleanup_errors)
        finally:
            for number, previous in previous_signals.items():
                signal.signal(number, previous)


SERVE_COMMANDS = [
    ("backend", cmd_serve, None),
    ("frontend", cmd_frontend, None),
    ("start", cmd_console, None),
    ("serve", cmd_serve, "兼容命令：单独启动后端，与 backend 相同（需安装 web 可选依赖）。"),
    ("console", cmd_console, "兼容命令：启动已构建前端和后端，与 start 相同。"),
]
