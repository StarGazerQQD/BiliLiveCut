"""在源码目录之外验证已安装 wheel 的双端入口及静态资源，运行时不提供 Node.js。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import psutil


class Assets(HTMLParser):
    """读取 HTML 中同源的入口脚本与样式。"""

    def __init__(self) -> None:
        super().__init__()
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """只选择构建产物的静态资源引用。"""
        values = dict(attrs)
        path = values.get("src" if tag == "script" else "href")
        if tag in {"script", "link"} and path and path.startswith("/assets/"):
            self.paths.add(path)


def _stop_console(process: subprocess.Popen, descendants: list[psutil.Process]) -> None:
    """保留监督器执行正常关闭；断言失败也须回收本次验收创建的进程。"""
    owned = {child.pid: child for child in descendants}
    try:
        if process.poll() is None:
            children = psutil.Process(process.pid).children(recursive=True)
            owned.update((child.pid, child) for child in children)
            assert process.stdin is not None
            process.stdin.close()
            process.wait(timeout=35)
        _, alive = psutil.wait_procs(list(owned.values()), timeout=5)
        assert not alive, f"wheel 启动器遗留子进程: {alive}"
    finally:
        for child in reversed(list(owned.values())):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        psutil.wait_procs(list(owned.values()), timeout=5)


def main() -> None:
    """从临时运行目录启动真实 blc start，校验页面、资源、代理与正常退出。"""
    import app
    import config
    from app.commands.serve import _frontend_directory

    package = Path(app.__file__).resolve().parent
    assert "site-packages" in package.parts, f"验收必须使用安装的 wheel，不能导入工作区: {package}"
    directory = _frontend_directory(None)
    assert directory == package / "web" / "ui", directory
    assert not (package / "web" / "templates").exists()
    assert not (package / "web" / "static").exists()
    for name in ("scoring.yaml", "keywords.zh.txt"):
        assert (Path(config.__file__).parent / name).is_file(), name
    assets = Assets()
    assets.feed((directory / "index.html").read_text(encoding="utf-8"))
    assets.paths.add("/media-auth.js")
    assert any(path.endswith(".js") for path in assets.paths)
    assert any(path.endswith(".css") for path in assets.paths)
    entry = Path(sys.executable).parent / ("blc.exe" if os.name == "nt" else "blc")
    assert entry.is_file(), entry
    with tempfile.TemporaryDirectory(prefix="blc-wheel-smoke-") as temporary:
        runtime = Path(temporary)
        env = {**os.environ}
        for key in ("PYTHONPATH", "BLC_SOURCE_DIR", "BLC_FRONTEND_DIR", "VIRTUAL_ENV"):
            env.pop(key, None)
        env.update(
            {
                "PATH": "",
                "BLC_APP_ROOT": str(runtime),
                "STORAGE_ROOT": str(runtime / "storage"),
                "DATABASE_URL": f"sqlite:///{runtime / 'smoke.db'}",
                "ADMIN_PASSWORD": "",
                "DOCKER_AUTH_ENABLED": "false",
                "BILIBILI_COOKIE": "",
                "ASR_NO_MODEL_DOWNLOAD": "1",
                "TREND_ENABLED": "false",
                "TREND_SCHEDULE_ENABLED": "false",
                "PLUGIN_DIR": str(runtime / "plugins"),
                "PYTHONIOENCODING": "utf-8",
                "_BLC_PARENT_STDIN": "1",
            }
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        opener = build_opener(ProxyHandler({}))
        with (runtime / "start.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(entry), "start", "--port", str(port)],
                cwd=runtime,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
            )
            descendants = []
            try:
                deadline = time.monotonic() + 45
                while True:
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/", timeout=1) as response:
                            assert response.read() == (directory / "index.html").read_bytes()
                        break
                    except (OSError, URLError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            log.seek(0)
                            raise RuntimeError(log.read()) from None
                        time.sleep(0.1)
                descendants = psutil.Process(process.pid).children(recursive=True)
                for path in sorted(assets.paths):
                    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                        expected = "text/javascript" if path.endswith(".js") else "text/css"
                        assert expected in response.headers["Content-Type"], path
                        assert response.read() == (directory / path.lstrip("/")).read_bytes(), path
                for path in ("/rooms", "/settings/notifications"):
                    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                        assert response.read() == (directory / "index.html").read_bytes()
                with opener.open(f"http://127.0.0.1:{port}/api/dashboard", timeout=5) as response:
                    assert json.load(response)["rooms"] == []
            finally:
                _stop_console(process, descendants)
            assert process.returncode == 0, process.returncode
            log.seek(0)
            assert "Web 服务资源已关闭" in log.read(), "wheel 后端没有执行正常生命周期清理"
    print(f"wheel frontend smoke OK: {len(assets.paths)} entry assets, SPA, API proxy, no Node.js at runtime")


if __name__ == "__main__":
    main()
