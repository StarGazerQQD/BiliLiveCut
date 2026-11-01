from __future__ import annotations

import os
import sys


def main() -> None:
    """让已退出子进程保持僵尸状态，只有真实父进程收到许可后才回收。"""
    child = os.fork()
    if child == 0:
        os._exit(0)
    try:
        print(child, flush=True)
        sys.stdin.readline()
    finally:
        os.waitpid(child, 0)


if __name__ == "__main__":
    main()
