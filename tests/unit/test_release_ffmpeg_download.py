"""Release FFmpeg 下载器的离线回归测试。"""

from __future__ import annotations

import io
import shutil
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from scripts import download_release_ffmpeg

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _build_ffmpeg_fixture(path: Path) -> None:
    """创建同时兼容 BtbN/Gyan 目录结构的最小 ZIP。"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr("ffmpeg-fixture/bin/ffmpeg.exe", b"ffmpeg-binary")
        package.writestr("ffmpeg-fixture/bin/ffprobe.exe", b"ffprobe-binary")


def test_download_retries_primary_then_validates_and_uses_fallback(tmp_path: Path) -> None:
    """主源持续 503、备用源首包损坏时仍应重试并只落盘完整文件。"""
    fixture = tmp_path / "fixture.zip"
    output_dir = tmp_path / "bin"
    _build_ffmpeg_fixture(fixture)
    calls: list[tuple[str, float]] = []
    sleeps: list[float] = []
    per_url_attempts: dict[str, int] = {}

    def fake_download(url: str, destination: Path, timeout_s: float) -> None:
        calls.append((url, timeout_s))
        per_url_attempts[url] = per_url_attempts.get(url, 0) + 1
        if url == "primary":
            raise OSError("503 Service Unavailable")
        if per_url_attempts[url] == 1:
            destination.write_bytes(b"not-a-zip")
            return
        shutil.copyfile(fixture, destination)

    selected = download_release_ffmpeg.download_release_ffmpeg(
        output_dir,
        urls=("primary", "fallback"),
        attempts=2,
        backoff_s=0.25,
        timeout_s=12.0,
        download=fake_download,
        sleep=sleeps.append,
    )

    assert selected == "fallback"
    assert calls == [
        ("primary", 12.0),
        ("primary", 12.0),
        ("fallback", 12.0),
        ("fallback", 12.0),
    ]
    assert sleeps == [0.25, 0.25]
    assert (output_dir / "ffmpeg.exe").read_bytes() == b"ffmpeg-binary"
    assert (output_dir / "ffprobe.exe").read_bytes() == b"ffprobe-binary"


def test_download_fails_closed_without_partial_binaries(tmp_path: Path) -> None:
    """全部来源失败时必须聚合错误，且不得留下伪造可执行文件。"""
    output_dir = tmp_path / "bin"

    def fail_download(url: str, destination: Path, timeout_s: float) -> None:
        del destination, timeout_s
        raise OSError(f"unavailable: {url}")

    with pytest.raises(RuntimeError, match="所有 FFmpeg 下载源均失败") as error:
        download_release_ffmpeg.download_release_ffmpeg(
            output_dir,
            urls=("primary", "fallback"),
            attempts=1,
            download=fail_download,
            sleep=lambda _delay: None,
        )

    assert "primary attempt 1/1" in str(error.value)
    assert "fallback attempt 1/1" in str(error.value)
    assert not output_dir.exists()


def test_main_reconfigures_narrow_windows_console_before_logging(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """CP1252 控制台也必须能输出下载器的中文日志。"""
    fixture = tmp_path / "fixture.zip"
    output_dir = tmp_path / "bin"
    _build_ffmpeg_fixture(fixture)
    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252", errors="strict")

    def fake_download(url: str, destination: Path, timeout_s: float) -> None:
        del url, timeout_s
        shutil.copyfile(fixture, destination)

    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(download_release_ffmpeg, "_download_archive", fake_download)

    exit_code = download_release_ffmpeg.main(["--output-dir", str(output_dir), "--attempts", "1"])
    stdout.flush()
    stderr.flush()

    assert exit_code == 0
    assert stdout.encoding.lower().replace("_", "-") == "utf-8"
    assert stderr.encoding.lower().replace("_", "-") == "utf-8"
    assert "FFmpeg 下载与校验完成" in stdout_bytes.getvalue().decode("utf-8")


def test_release_workflow_uses_resilient_ffmpeg_downloader() -> None:
    """Release 工作流必须调用受测下载器并预留完整重试时间。"""
    repo_root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((repo_root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build-windows-lite"]["steps"]
    step = next(item for item in steps if item.get("name") == "Download FFmpeg static binaries")

    assert step["run"] == "python scripts/download_release_ffmpeg.py --output-dir bin"
    assert step["timeout-minutes"] >= 10
