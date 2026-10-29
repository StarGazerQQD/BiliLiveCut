from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import download_docker_ffmpeg as download

if TYPE_CHECKING:
    from pytest import MonkeyPatch

PAYLOAD = b"source archive supplied by the external download boundary"


def prepare_destination(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    monkeypatch.setattr(download, "SOURCE_SHA256", hashlib.sha256(PAYLOAD).hexdigest())
    destination = tmp_path / "ffmpeg.tar.xz"
    destination.write_bytes(b"previous verified archive")
    return destination


def store_download(command: list[str]) -> None:
    Path(command[command.index("--output") + 1]).write_bytes(PAYLOAD)


def test_fixed_sources_preserve_version_and_original_digest() -> None:
    assert download.SOURCE_URLS == (
        "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
        "https://distfiles.macports.org/ffmpeg/ffmpeg-9.0.2.tar.xz",
    )
    assert download.SOURCE_SHA256 == "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
    assert download.DOWNLOAD_TIMEOUT_SECONDS * len(download.SOURCE_URLS) <= 600


def test_primary_success_verifies_and_publishes_without_mirror(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    destination = prepare_destination(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert destination.read_bytes() == b"previous verified archive"
        for flag, upper in (("--connect-timeout", 30), ("--max-time", 180), ("--retry", 3), ("--retry-max-time", 600)):
            assert 0 < int(command[command.index(flag) + 1]) <= upper
        assert command[command.index("--proto") + 1] == "=https"
        assert command[command.index("--proto-redir") + 1] == "=https"
        assert {"--fail", "--retry-all-errors"} <= set(command)
        assert "--insecure" not in command and "-k" not in command
        assert kwargs["check"] is True
        assert kwargs["timeout"] == download.DOWNLOAD_TIMEOUT_SECONDS
        store_download(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(download.subprocess, "run", run)
    download.download_source(destination)
    assert [command[-1] for command in commands] == [download.SOURCE_URLS[0]]
    assert destination.read_bytes() == PAYLOAD
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("code", sorted(download.RECOVERABLE_CURL_CODES))
def test_transport_failure_uses_fresh_mirror_file(tmp_path: Path, monkeypatch: MonkeyPatch, code: int) -> None:
    destination = prepare_destination(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        archive = Path(command[command.index("--output") + 1])
        assert not archive.exists()
        if len(commands) == 1:
            archive.write_bytes(b"incomplete primary response")
            raise subprocess.CalledProcessError(code, command, stderr="transfer failed")
        store_download(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(download.subprocess, "run", run)
    download.download_source(destination)
    assert [command[-1] for command in commands] == list(download.SOURCE_URLS)
    assert destination.read_bytes() == PAYLOAD
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("mode", ["both_network", "both_timeout", "timeout_then_success"])
def test_source_exhaustion_is_bounded_and_keeps_original(tmp_path: Path, monkeypatch: MonkeyPatch, mode: str) -> None:
    destination = prepare_destination(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if mode == "timeout_then_success" and len(commands) == 2:
            store_download(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        if mode == "both_network":
            raise subprocess.CalledProcessError(35, command, stderr="Connection reset by peer")
        raise subprocess.TimeoutExpired(command, download.DOWNLOAD_TIMEOUT_SECONDS, stderr=b"transfer stalled")

    monkeypatch.setattr(download.subprocess, "run", run)
    if mode == "timeout_then_success":
        download.download_source(destination)
        assert destination.read_bytes() == PAYLOAD
    else:
        with pytest.raises(RuntimeError, match="All FFmpeg source downloads failed") as caught:
            download.download_source(destination)
        assert all(url in str(caught.value) for url in download.SOURCE_URLS)
        assert caught.value.__cause__ is not None
        assert destination.read_bytes() == b"previous verified archive"
    assert len(commands) == len(download.SOURCE_URLS)
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("source_index", [0, 1])
@pytest.mark.parametrize("contents", [b"", b"corrupt source"])
def test_bad_digest_fails_closed_without_replacing_output(
    tmp_path: Path, monkeypatch: MonkeyPatch, source_index: int, contents: bytes
) -> None:
    destination = prepare_destination(tmp_path, monkeypatch)
    calls = 0

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls <= source_index:
            raise subprocess.CalledProcessError(28, command, stderr="Connection timed out")
        Path(command[command.index("--output") + 1]).write_bytes(contents)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(download.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        download.download_source(destination)
    assert calls == source_index + 1
    assert destination.read_bytes() == b"previous verified archive"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("code", [1, 3, 23, 26, 48, 60, 77])
def test_configuration_certificate_and_write_errors_do_not_switch_sources(
    tmp_path: Path, monkeypatch: MonkeyPatch, code: int
) -> None:
    destination = prepare_destination(tmp_path, monkeypatch)
    calls = 0

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        raise subprocess.CalledProcessError(code, command, stderr="fatal download error")

    monkeypatch.setattr(download.subprocess, "run", run)
    with pytest.raises(RuntimeError, match=f"curl exit={code}: fatal download error") as caught:
        download.download_source(destination)
    assert isinstance(caught.value.__cause__, subprocess.CalledProcessError)
    assert calls == 1
    assert destination.read_bytes() == b"previous verified archive"
    assert list(tmp_path.iterdir()) == [destination]


def test_missing_curl_does_not_switch_sources(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    destination = prepare_destination(tmp_path, monkeypatch)
    calls = 0

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        raise FileNotFoundError("curl")

    monkeypatch.setattr(download.subprocess, "run", run)
    with pytest.raises(FileNotFoundError, match="curl"):
        download.download_source(destination)
    assert calls == 1
    assert destination.read_bytes() == b"previous verified archive"
    assert list(tmp_path.iterdir()) == [destination]


def test_publish_failure_is_not_retried(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    destination = prepare_destination(tmp_path, monkeypatch)
    calls = 0

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        store_download(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def replace(path: Path, target: Path) -> Path:
        assert path.read_bytes() == PAYLOAD
        assert target == destination
        raise PermissionError("cannot replace destination")

    monkeypatch.setattr(download.subprocess, "run", run)
    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(PermissionError, match="cannot replace destination"):
        download.download_source(destination)
    assert calls == 1
    assert destination.read_bytes() == b"previous verified archive"
    assert list(tmp_path.iterdir()) == [destination]
