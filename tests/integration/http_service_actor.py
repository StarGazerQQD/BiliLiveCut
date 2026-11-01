from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main() -> None:
    """提供先成功后失联的真实 HTTP 边界，收到关闭管道后优雅退出。"""
    port, counter = int(sys.argv[1]), Path(sys.argv[2])
    with counter.open("a", encoding="utf-8") as record:
        record.write(json.dumps({"pid": os.getpid(), "started_at": time.monotonic()}) + "\n")
    calls = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            nonlocal calls
            assert self.path == "/auth/status"
            calls += 1
            self.send_response(200 if calls == 1 else 503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    class Server(HTTPServer):
        allow_reuse_address = True

    with Server(("127.0.0.1", port), Handler) as server:

        def stop_on_parent_exit() -> None:
            sys.stdin.buffer.read(1)
            server.shutdown()

        stopping = threading.Thread(target=stop_on_parent_exit)
        stopping.start()
        server.serve_forever(poll_interval=0.05)
        stopping.join(timeout=5)
        assert not stopping.is_alive()
    with counter.open("a", encoding="utf-8") as record:
        record.write(json.dumps({"pid": os.getpid(), "stopped_at": time.monotonic(), "exit_code": 0}) + "\n")


if __name__ == "__main__":
    main()
