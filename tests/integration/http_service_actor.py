from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psutil


def main() -> None:
    """提供真实 HTTP 边界及可控的管道关闭结果。"""
    port, counter = int(sys.argv[1]), Path(sys.argv[2])
    mode = sys.argv[3] if len(sys.argv) > 3 else "unavailable"
    with counter.open("a", encoding="utf-8") as record:
        record.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "create_time": psutil.Process().create_time(),
                    "started_at": time.monotonic(),
                    "port": port,
                }
            )
            + "\n"
        )
    calls = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            nonlocal calls
            assert self.path == "/auth/status"
            calls += 1
            self.send_response(200 if mode != "unavailable" or calls == 1 else 503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    class Server(HTTPServer):
        allow_reuse_address = True

    with Server(("127.0.0.1", port), Handler) as server:

        def stop_on_parent_exit() -> None:
            sys.stdin.buffer.read(1)
            with counter.open("a", encoding="utf-8") as record:
                record.write(json.dumps({"pid": os.getpid(), "parent_eof_at": time.monotonic()}) + "\n")
            if mode == "forced":
                return
            server.shutdown()

        stopping = threading.Thread(target=stop_on_parent_exit)
        stopping.start()
        server.serve_forever(poll_interval=0.05)
        stopping.join(timeout=5)
        assert not stopping.is_alive()
    code = 1 if mode == "failed" else 0
    with counter.open("a", encoding="utf-8") as record:
        record.write(json.dumps({"pid": os.getpid(), "stopped_at": time.monotonic(), "exit_code": code}) + "\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
