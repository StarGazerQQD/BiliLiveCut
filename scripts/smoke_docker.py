"""验证真实 Docker 镜像的认证、依赖、持久化和停止行为。"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def main() -> int:
    """使用临时容器和卷执行验收，不下载模型或读取仓库 .env。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="bili-live-cut:docker-test")
    parser.add_argument("--frontend-image", default="bili-live-cut-frontend:docker-test")
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--browser", action="store_true", help="使用 Playwright Chromium 验证实际网页登录")
    parser.add_argument("--host-network", action="store_true", help="供禁用 bridge 的隔离测试 daemon 使用")
    args = parser.parse_args()
    prefix = "blc-docker-smoke-" + secrets.token_hex(5)
    volume = prefix + "-data"
    denied_volume = prefix + "-denied"
    network_name = prefix + "-network"
    containers: list[str] = []
    password = secrets.token_urlsafe(24)
    environment = {**os.environ, "ADMIN_PASSWORD": password}

    def docker(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run([args.docker, *command], capture_output=True, text=True, timeout=180, env=environment)
        if check and result.returncode:
            raise RuntimeError(f"Docker {command[0]} 失败：{result.stderr.strip()}")
        return result

    def request(url: str, *, authenticated: bool = False) -> int:
        headers = {}
        if authenticated:
            headers["Authorization"] = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
        try:
            with build_opener(ProxyHandler({})).open(Request(url, headers=headers), timeout=3) as response:
                return response.status
        except HTTPError as exc:
            return exc.code

    def start(name: str, *, initialize: bool = False) -> str:
        containers.append(name)
        network = ["--network", "host"] if args.host_network else ["--publish", "127.0.0.1::8000"]
        command = ["blc", "backend", "--host", "127.0.0.1", "--port", "8000"] if args.host_network else []
        docker(
            "run",
            "--detach",
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--name",
            name,
            *network,
            "--env",
            "ADMIN_PASSWORD=" if initialize else "ADMIN_PASSWORD",
            "--env",
            "ASR_NO_MODEL_DOWNLOAD=1",
            "--env",
            "TREND_ENABLED=false",
            "--env",
            "TREND_SCHEDULE_ENABLED=false",
            "--env",
            "HTTP_PROXY=http://127.0.0.1:1",
            "--mount",
            f"type=volume,source={volume},target=/data",
            args.image,
            *command,
        )
        port = "8000" if args.host_network else docker("port", name, "8000/tcp").stdout.strip().rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = json.loads(docker("inspect", "--format", "{{json .State}}", name).stdout)
            if not state["Running"]:
                logs = docker("logs", name)
                raise RuntimeError(f"容器提前退出：{logs.stdout}{logs.stderr}")
            try:
                if (
                    request(url + ("/auth/status" if initialize else "/api/dashboard"), authenticated=not initialize)
                    == 200
                ):
                    assert request(url + "/api/dashboard") == 401
                    docker("exec", name, "python", "/app/docker/healthcheck.py")
                    return url
            except (OSError, URLError):
                pass
            time.sleep(1)
        raise RuntimeError("容器 Web 服务在 120 秒内没有就绪。")

    def start_frontend(backend: str) -> str:
        name = prefix + "-frontend"
        containers.append(name)
        if args.host_network:
            network = ["--network", "host", "--add-host", "app:127.0.0.1"]
        else:
            docker("network", "create", network_name)
            docker("network", "connect", "--alias", "app", network_name, backend)
            network = ["--network", network_name, "--publish", "127.0.0.1::8080"]
        docker(
            "run",
            "--detach",
            "--name",
            name,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *network,
            args.frontend_image,
        )
        port = "8080" if args.host_network else docker("port", name, "8080/tcp").stdout.strip().rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if (
                    request(url + "/")
                    == request(url + "/settings/configuration")
                    == request(url + "/auth/status")
                    == 200
                ):
                    assert request(url + "/assets/missing-smoke.js") == 404
                    assert docker("exec", name, "id", "-u").stdout.strip() == "101"
                    for package, minimum in {
                        "brotli-libs": "1.2.1-r0",
                        "musl": "1.2.6-r3",
                        "musl-utils": "1.2.6-r3",
                        "zlib": "1.3.2-r1",
                        "curl": "8.23.0-r0",
                        "libcurl": "8.23.0-r0",
                    }.items():
                        docker("exec", name, "apk", "info", "--exists", f"{package}>={minimum}")
                    return url
            except (OSError, URLError):
                pass
            if not json.loads(docker("inspect", "--format", "{{json .State}}", name).stdout)["Running"]:
                logs = docker("logs", name)
                raise RuntimeError(f"前端容器提前退出：{logs.stdout}{logs.stderr}")
            time.sleep(0.2)
        raise RuntimeError("前端静态页面或 API 代理未就绪。")

    try:
        # 在 Linux 卷内设置权限，Windows bind mount 的 chmod 不具有等价语义。
        docker("volume", "create", denied_volume)
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "python",
            "--mount",
            f"type=volume,source={denied_volume},target=/data",
            args.image,
            "-c",
            "import os; assert os.getuid() == 1000; os.chmod('/data', 0o555)",
        )
        denied_name = prefix + "-denied"
        containers.append(denied_name)
        denied = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--name",
            denied_name,
            "--env",
            "ADMIN_PASSWORD",
            "--mount",
            f"type=volume,source={denied_volume},target=/data,volume-nocopy",
            args.image,
            check=False,
        )
        assert denied.returncode == 78 and "不可写" in denied.stderr, (denied.returncode, denied.stderr)
        docker("volume", "create", volume)
        first = prefix + "-first"
        start(first)
        docker(
            "exec",
            first,
            "python",
            "-c",
            "\n".join(
                [
                    "import os, sqlite3; from pathlib import Path",
                    "import app, shutil; from app.commands.serve import _frontend_directory",
                    "frontend = _frontend_directory(None)",
                    "assert frontend == Path(app.__file__).parent / 'web' / 'ui'",
                    "assert (frontend / 'index.html').is_file() and (frontend / 'media-auth.js').is_file()",
                    "assert any(frontend.rglob('*.js')) and any(frontend.rglob('*.css'))",
                    "assert not (frontend.parent / 'templates').exists() and not (frontend.parent / 'static').exists()",
                    "assert shutil.which('node') is None and shutil.which('npm') is None",
                    "import torch, torchaudio; from funasr import AutoModel",
                    "from funasr.utils.load_utils import load_audio_text_image_video",
                    "import wave, av",
                    "from faster_whisper.audio import decode_audio",
                    "assert av.ffmpeg_version_info == '9.0.2'",
                    "status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())",
                    "assert int(status['NoNewPrivs']) == 1",
                    "assert int(status['CapEff'], 16) == int(status['CapBnd'], 16) == 0",
                    "from app.accelerators import _c_speedups, _cython_speedups, _repeat_speedups, "
                    "_rust_speedups, dispatcher",
                    "assert dispatcher.get_repeat_backend() == 'C++'",
                    "assert dispatcher.consecutive_repeat_details('abc' * 100) == (0, 3, 100)",
                    "assert dispatcher.get_cluster_backend() == 'Rust+rayon'",
                    "assert dispatcher.get_rust_backend() == 'Rust+rayon'",
                    "assert dispatcher.pcm16_statistics(bytes([0, 128, 255, 127])) == (2, 2147418113, 32768)",
                    "assert dispatcher.danmaku_text_features(['高能!', '高能!'], ['高能']) "
                    "== (1.0, 1.0, 1.0, ('高能!',))",
                    "assert dispatcher.cluster_similarity_matrix([{'asr_text': '中文'}] * 2) "
                    "== [[1.0, 0.55], [0.55, 1.0]]",
                    "assert dispatcher.cluster_groups([[1.0, 0.9], [0.9, 1.0]], 0.82) == [(0, [0, 1], 0.9)]",
                    "assert dispatcher.text_novelty('AB 新鲜', 'ab 日常') == (0.5, 1.0 - 1.0 / 3.0, 0.5)",
                    "assert os.getuid() == 1000 and torch.version.cuda is None",
                    "assert 'DISPLAY' not in os.environ and 'WAYLAND_DISPLAY' not in os.environ",
                    "with wave.open('/data/smoke.wav', 'wb') as audio:",
                    "    audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))",
                    "    audio.writeframes(bytes(32000))",
                    "samples = load_audio_text_image_video('/data/smoke.wav', fs=16000)",
                    "assert isinstance(samples, torch.Tensor) and samples.numel() == 16000",
                    "assert decode_audio('/data/smoke.wav', sampling_rate=16000).size == 16000",
                    "db = sqlite3.connect('/data/blc.db')",
                    "assert db.execute(\"select count(*) from sqlite_master where type='table'\").fetchone()[0] > 0",
                    "for key in ('PLUGIN_DIR', 'HF_HOME', 'MODELSCOPE_CACHE'):",
                    "    root = Path(os.environ[key]); assert root.is_relative_to('/data')",
                    "    (root / 'docker-smoke-marker').write_text('persistent')",
                ]
            ),
        )
        # 确认真实镜像完成精确裁剪，保留账号、证书和运行库；不依赖 Dockerfile 文本断言。
        packages = docker("exec", first, "dpkg-query", "-W", "-f=${Package}\t${db:Status-Status}\n").stdout
        installed = {line.split("\t")[0] for line in packages.splitlines() if line.endswith("\tinstalled")}
        assert not installed & {
            "apt",
            "libapt-pkg7.0",
            "sqv",
            "mount",
            "passwd",
            "adduser",
            "libsemanage2",
            "libsemanage-common",
            "libsepol2",
        }
        assert {"dpkg", "ca-certificates", "tzdata", "libc6", "libssl3t64", "libsqlite3-0"} <= installed
        assert not docker("exec", first, "dpkg", "--audit").stdout.strip()
        # 检查真实安装版本，避免旧基础层仍满足依赖关系却缺少官方安全更新。
        for package, minimum in {
            "ca-certificates": "20250419+deb13u1",
            "libbrotli1": "1.1.0-2+deb13u1",
            "libexpat1": "2.9.0-1~deb13u1",
        }.items():
            installed_version = docker("exec", first, "dpkg-query", "-W", "-f=${Version}", package).stdout.strip()
            docker("exec", first, "dpkg", "--compare-versions", installed_version, "ge", minimum)
            print(f"{package}: {installed_version} >= {minimum}")
        docker(
            "exec",
            first,
            "python",
            "-c",
            "import pwd, grp, ssl, sqlite3, lzma, bz2, ctypes, socket; "
            "assert pwd.getpwnam('appuser').pw_uid == grp.getgrnam('appuser').gr_gid == 1000; "
            "assert ssl.create_default_context().get_ca_certs(); "
            "assert lzma.decompress(lzma.compress(b'pcm')) == b'pcm'; "
            "assert bz2.decompress(bz2.compress(b'pcm')) == b'pcm'",
        )
        # 实际烧录字幕并核对 PyAV/FFmpeg 所加载的 libass 属于原始 Debian 包。
        media_check = docker(
            "exec",
            first,
            "python",
            "-c",
            "\n".join(
                [
                    "import json, subprocess; from pathlib import Path; import av",
                    "version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', 'libass9'], text=True)",
                    "assert not subprocess.check_output(['dpkg', '--verify', 'libass9'], text=True).strip()",
                    "files = subprocess.check_output(['dpkg-query', '-L', 'libass9'], text=True).splitlines()",
                    "owned = {Path(p).resolve() for p in files if '/libass.so.' in p}",
                    "loaded = {Path(line.split()[-1]).resolve() for line in "
                    "Path('/proc/self/maps').read_text().splitlines() if '/libass.so.' in line}",
                    "assert owned and loaded and loaded <= owned, (owned, loaded)",
                    "assert not list(Path('/opt/ffmpeg').rglob('*libass*'))",
                    "linked = subprocess.check_output(['ldd', '/opt/ffmpeg/bin/ffmpeg'], text=True)",
                    "assert any('/libass.so.' in line and Path(line.split('=>')[1].split()[0]).resolve() "
                    "in owned for line in linked.splitlines())",
                    "Path('/data/smoke.srt').write_text('1\\n00:00:00,000 --> 00:00:01,000\\n中文回归\\n', "
                    "encoding='utf-8')",
                    "frame = subprocess.check_output(['ffmpeg', '-hide_banner', '-loglevel', 'error', "
                    "'-f', 'lavfi', '-i', 'color=c=black:s=320x180:d=1', '-vf', 'subtitles=/data/smoke.srt', "
                    "'-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])",
                    "assert len(frame) == 320 * 180 * 3 and any(frame)",
                    "print(json.dumps({'libass9': version, 'loaded': sorted(map(str, loaded)), "
                    "'subtitle_pixels': sum(value != 0 for value in frame)}))",
                ]
            ),
        )
        print(media_check.stdout.strip())
        filters = docker("exec", first, "ffmpeg", "-hide_banner", "-filters").stdout
        assert "subtitles" in filters and "drawtext" in filters
        fonts = docker("exec", first, "fc-list", ":lang=zh", "family").stdout
        assert "Noto" in fonts
        docker(
            "exec",
            first,
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=1",
            "-i",
            "/data/smoke.wav",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-vf",
            "drawtext=font='Noto Sans CJK SC':text='无头测试':fontsize=24:fontcolor=white",
            "/data/smoke.mp4",
        )
        docker(
            "exec",
            first,
            "python",
            "-c",
            "from faster_whisper.audio import decode_audio; "
            "assert decode_audio('/data/smoke.mp4', sampling_rate=16000).size >= 16000",
        )
        docker("stop", "--time", "60", first)
        state = json.loads(docker("inspect", "--format", "{{json .State}}", first).stdout)
        assert state["ExitCode"] in (0, 143) and not state["OOMKilled"]
        logs = docker("logs", first)
        # Uvicorn 重放 SIGTERM 时最后一条异步日志未必已刷出；退出状态排除强制杀死。
        assert "Web 服务关闭失败" not in logs.stdout + logs.stderr
        second = prefix + "-second"
        start(second)
        docker(
            "exec",
            second,
            "python",
            "-c",
            "\n".join(
                [
                    "import os; from pathlib import Path",
                    "for key in ('PLUGIN_DIR', 'HF_HOME', 'MODELSCOPE_CACHE'):",
                    "    assert (Path(os.environ[key]) / 'docker-smoke-marker').read_text() == 'persistent'",
                ]
            ),
        )
        docker("stop", "--time", "60", second)
        third = prefix + "-initialized"
        url = start(third, initialize=True)
        url = start_frontend(third)
        logs = docker("logs", third)
        code = re.findall(r"仅可使用一次）：([\w-]+)", logs.stdout + logs.stderr)[-1]
        password = secrets.token_urlsafe(24)

        def expire_session() -> None:
            docker(
                "exec",
                third,
                "python",
                "-c",
                "import sqlite3; db=sqlite3.connect('/data/config/web-auth.sqlite3'); "
                "db.execute('UPDATE sessions SET seen=0'); db.commit(); db.close()",
            )

        if args.browser:
            from check_docker_auth_browser import check_browser

            password = check_browser(url, code, password, expire_session)
        else:
            payload = json.dumps({"code": code, "password": password}).encode()
            with build_opener(ProxyHandler({})).open(
                Request(url + "/auth/setup", data=payload, headers={"Content-Type": "application/json", "Origin": url}),
                timeout=10,
            ) as result:
                assert result.status == 200
        assert request(url + "/api/dashboard", authenticated=True) == 200
        docker(
            "exec",
            third,
            "python",
            "-c",
            "import os, stat; from pathlib import Path; "
            "s = Path('/data/config/web-auth.sqlite3').stat(); "
            "assert stat.S_IMODE(s.st_mode) == 0o600 and s.st_uid == os.getuid()",
        )
        logs = docker("logs", third)
        assert password not in logs.stdout + logs.stderr
        docker("stop", "--time", "60", third)
        fourth = prefix + "-initialized-recreated"
        url = start(fourth, initialize=True)
        assert request(url + "/api/dashboard", authenticated=True) == 200
        logs = docker("logs", fourth)
        assert "仅可使用一次）" not in logs.stdout + logs.stderr
        docker("stop", "--time", "60", fourth)
        print("Docker 初始化、密码持久化、CPU ASR、原生扩展、FFmpeg、字体、数据持久化和正常停止：通过。")
        return 0
    finally:
        for name in containers:
            docker("rm", "--force", name, check=False)
        if not args.host_network:
            docker("network", "rm", network_name, check=False)
        docker("volume", "rm", volume, check=False)
        docker("volume", "rm", denied_volume, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
