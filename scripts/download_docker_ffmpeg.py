# ruff: noqa: D100

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path

SOURCE_URLS = (
    "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
    "https://distfiles.macports.org/ffmpeg/ffmpeg-9.0.2.tar.xz",
)
SOURCE_SHA256 = "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
# 仅恢复传输故障；证书、权限、参数及本地写入错误直接终止。
RECOVERABLE_CURL_CODES = frozenset({5, 6, 7, 18, 22, 28, 35, 52, 55, 56, 92})
DOWNLOAD_TIMEOUT_SECONDS = 270


def download_source(destination: Path) -> None:
    """从固定来源下载同一份源码，校验成功后原子替换目标文件。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    last_error: subprocess.CalledProcessError | subprocess.TimeoutExpired | None = None
    with tempfile.TemporaryDirectory(prefix="ffmpeg-source-", dir=destination.parent) as directory:
        for index, url in enumerate(SOURCE_URLS):
            archive = Path(directory) / f"source-{index}.tar.xz"
            print(f"Downloading FFmpeg source: {url}", flush=True)
            command = [
                "curl",
                "--fail",
                "--show-error",
                "--silent",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--connect-timeout",
                "20",
                "--max-time",
                "120",
                "--retry",
                "2",
                "--retry-delay",
                "5",
                "--retry-max-time",
                "120",
                "--retry-all-errors",
                "--output",
                str(archive),
                url,
            ]
            try:
                subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            except subprocess.CalledProcessError as exc:
                if exc.returncode not in RECOVERABLE_CURL_CODES:
                    raise RuntimeError(
                        f"FFmpeg download failed: {url}; curl exit={exc.returncode}: {exc.stderr}"
                    ) from exc
                last_error = exc
                failures.append(f"{url}: curl exit={exc.returncode}: {exc.stderr}")
            except subprocess.TimeoutExpired as exc:
                last_error = exc
                failures.append(f"{url}: exceeded {DOWNLOAD_TIMEOUT_SECONDS}s: {exc.stderr!r}")
            else:
                with archive.open("rb") as source:
                    actual = hashlib.file_digest(source, "sha256").hexdigest()
                if actual != SOURCE_SHA256:
                    raise RuntimeError(
                        f"FFmpeg source SHA-256 mismatch: {url}; expected={SOURCE_SHA256}; actual={actual}"
                    )
                archive.replace(destination)
                print(f"Verified FFmpeg source: {url}; SHA-256={actual}", flush=True)
                return
            print(f"FFmpeg source unavailable: {failures[-1]}", flush=True)
    raise RuntimeError("All FFmpeg source downloads failed:\n" + "\n".join(failures)) from last_error


def main() -> None:
    """下载 Docker 构建使用的固定 FFmpeg 发行源码。"""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    download_source(arguments.output)


if __name__ == "__main__":
    main()
