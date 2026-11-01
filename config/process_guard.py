# ruff: noqa: D100
from __future__ import annotations

import errno
import json
import os
import signal
import sys


def _watch_parent(life: int, ready: int, status: int, extra: list[int]) -> None:
    group = os.getpgrp()
    try:
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, signal.SIG_IGN)
        for descriptor in {0, 1, 2, status, *extra} - {life, ready}:
            try:
                os.close(descriptor)
            except OSError as exc:
                # 标准流或调用者的额外描述符可以原本就已关闭。
                if exc.errno != errno.EBADF:
                    raise
        os.write(ready, b"1")
        os.close(ready)
        while os.read(life, 1):
            pass
    finally:
        # 进程组在 fork 前已由父启动器独占创建，绝不使用宿主的进程组。
        os.killpg(group, signal.SIGKILL)


def main() -> None:
    """在单线程启动进程中建立父死亡监管，再以原 PID 执行真实命令。"""
    life, status = int(sys.argv[1]), int(sys.argv[2])
    restore_signals = sys.argv[3] == "1"
    extra: list[int] = json.loads(sys.argv[4])
    executable, command = sys.argv[5], sys.argv[6:]
    os.set_inheritable(life, False)
    os.set_inheritable(status, False)
    if os.getpgrp() != os.getpid():
        raise RuntimeError("进程监管未取得独占进程组")
    try:
        ready_read, ready_write = os.pipe()
        guardian = os.fork()
        if guardian == 0:
            os.close(ready_read)
            _watch_parent(life, ready_write, status, extra)
            os._exit(1)
        os.close(life)
        os.close(ready_write)
        with os.fdopen(ready_read, "rb") as ready:
            if ready.read(1) != b"1":
                raise RuntimeError("父死亡监管未就绪")
        os.write(status, json.dumps({"kind": "ready", "guardian": guardian}).encode() + b"\n")
        if restore_signals:
            for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                if number := getattr(signal, name, None):
                    signal.signal(number, signal.SIG_DFL)
        os.execvpe(executable, command, os.environ)
    except OSError as exc:
        os.write(status, json.dumps({"kind": "exec_error", "errno": exc.errno}).encode() + b"\n")
        os._exit(1)


if __name__ == "__main__":
    main()
