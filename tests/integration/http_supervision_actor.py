from __future__ import annotations

import _thread
import os
import runpy
import sys
import threading

from config import service_restart

LAUNCHER_HTTP_READY = "Launcher 已确认首次 HTTP 就绪"


def main() -> None:
    """仅缩短外部故障时钟，保持正式监督、HTTP、所属树和恢复调用链。"""
    service_restart.HTTP_UNAVAILABLE_LIMIT_S = 3.0
    service_restart.UNRESPONSIVE_SHUTDOWN_LIMIT_S = 6.0
    mode, port, frontend = sys.argv[1:]
    command = [sys.executable, "-m", "app.cli", "start", "--port", port, "--frontend-dir", frontend]
    if mode in {"console", "console-signal"}:
        if mode == "console-signal":
            from app.commands.serve import _wait_parent_pipe

            os.environ.pop("_BLC_PARENT_STDIN")

            def signal_on_parent_stop() -> None:
                _wait_parent_pipe()
                _thread.interrupt_main()

            threading.Thread(target=signal_on_parent_stop, daemon=True).start()
        sys.argv = command[2:]
        runpy.run_module("app.cli", run_name="__main__", alter_sys=True)
        return

    from blc_portable.launcher import main as launcher

    from app.commands.serve import _wait_parent_pipe

    probe_service = launcher._service_http_ready
    ready_reported = False

    def observe_launcher_http_ready(web_port: int) -> bool:
        nonlocal ready_reported
        ready = probe_service(web_port)
        if ready and not ready_reported:
            ready_reported = True
            print(LAUNCHER_HTTP_READY, flush=True)
        return ready

    launcher._service_http_ready = observe_launcher_http_ready

    def interrupt_on_parent_stop() -> None:
        _wait_parent_pipe()
        _thread.interrupt_main()

    threading.Thread(target=interrupt_on_parent_stop, daemon=True).start()
    raise SystemExit(launcher._run_service(command, env=dict(os.environ), cwd=os.getcwd()))


if __name__ == "__main__":
    main()
