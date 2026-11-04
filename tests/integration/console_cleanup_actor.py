# ruff: noqa: D100
from __future__ import annotations

import _thread
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from subprocess import Popen

from app.analysis.transcription.process_tree import ProcessTree
from app.cli import main


def run() -> None:
    """执行真实 console，仅在进程启动或清理的 OS 边界注入故障。"""
    counter = Path(sys.argv.pop(1))
    if sys.argv[1:2] == ["--backend-stop"]:
        from app.core import process_spawn
        from config import service_restart

        sys.argv.pop(1)
        mode = sys.argv.pop(1)
        if sys.argv[1:2] == ["--stop-signal-file"]:
            sys.argv.pop(1)
            signal_file = Path(sys.argv.pop(1))

            def signal_stop() -> None:
                while not signal_file.exists():
                    time.sleep(0.05)
                _thread.interrupt_main()

            threading.Thread(target=signal_stop, daemon=True).start()
        original_spawn = process_spawn.spawn_owned
        service_restart.OUTER_SHUTDOWN_LIMIT_S = 12.0

        def spawn_backend(command: Sequence[str], **options: object) -> tuple[Popen, ProcessTree]:
            if list(command[1:4]) == ["-m", "app.cli", "backend"]:
                command = [
                    sys.executable,
                    str(Path(__file__).with_name("http_service_actor.py")),
                    command[command.index("--port") + 1],
                    str(counter),
                    mode,
                ]
            return original_spawn(command, **options)

        process_spawn.spawn_owned = spawn_backend
        main()
        return
    with counter.open("a", encoding="utf-8") as output:
        output.write("1")
    original = ProcessTree.terminate

    def unconfirmed(tree: ProcessTree) -> None:
        original(tree)
        raise OSError("controlled cleanup confirmation failure")

    ProcessTree.terminate = unconfirmed
    main()


if __name__ == "__main__":
    run()
