#!/usr/bin/env python3
"""Run a real Lite first-install and second-offline-launch smoke test."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO

WEB_URL = "http://127.0.0.1:8000/"
REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_CONFIG = REPO_ROOT / "packaging" / "portable" / "config" / "version.json"


def _configure_console_encoding() -> None:
    """Use UTF-8 output when the host console supports stream reconfiguration."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, TypeError, ValueError):
            continue


def _required_file(directory: Path, filename: str, label: str) -> Path:
    """Return the exact current-version artifact from a directory."""
    path = directory / filename
    if not path.is_file():
        raise RuntimeError(f"Missing {label}: {path}")
    return path.resolve()


def _port_is_open() -> bool:
    """Return whether the fixed Portable Web port already has a listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        return client.connect_ex(("127.0.0.1", 8000)) == 0


def _print_log(path: Path) -> None:
    """Print a launcher log using tolerant UTF-8 decoding."""
    print(f"==== {path.name} ====", flush=True)
    if path.is_file():
        print(path.read_text(encoding="utf-8", errors="replace"), flush=True)


def _stop_process_tree(process: subprocess.Popen[bytes] | None) -> None:
    """Stop a launcher and its service child without leaving port 8000 occupied."""
    if process is None or process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _wait_ready(process: subprocess.Popen[bytes], timeout_seconds: int, phase: str) -> None:
    """Wait until the Portable Web root responds successfully."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"Lite {phase} launcher exited before Web became ready (exit={return_code})")
        try:
            with urllib.request.urlopen(WEB_URL, timeout=5) as response:  # noqa: S310 - fixed localhost URL
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(2)
    raise RuntimeError(f"Lite {phase} launcher did not become ready within {timeout_seconds} seconds")


def _start_launcher(
    executable: Path,
    arguments: list[str],
    work_dir: Path,
    stdout: BinaryIO,
    stderr: BinaryIO,
) -> subprocess.Popen[bytes]:
    """Start the frozen launcher without a shell or PowerShell wrapper."""
    startupinfo = None
    creationflags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        [str(executable), *arguments],
        cwd=work_dir,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def _run_phase(
    executable: Path,
    arguments: list[str],
    work_dir: Path,
    phase: str,
    timeout_seconds: int,
) -> None:
    """Start one launcher phase, require Web readiness, then stop it."""
    stdout_path = work_dir / f"{phase}.stdout.log"
    stderr_path = work_dir / f"{phase}.stderr.log"
    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = _start_launcher(executable, arguments, work_dir, stdout, stderr)
            _wait_ready(process, timeout_seconds, phase)
    finally:
        _stop_process_tree(process)
        _print_log(stdout_path)
        _print_log(stderr_path)


def run_smoke(
    executable: Path,
    engine_pack: Path,
    *,
    work_parent: Path | None = None,
    first_timeout_seconds: int = 900,
    second_timeout_seconds: int = 300,
) -> None:
    """Perform first online installation and second strictly offline launch."""
    if sys.platform != "win32":
        raise RuntimeError("Lite executable smoke testing requires Windows")
    if _port_is_open():
        raise RuntimeError("Port 8000 is already in use before Lite smoke testing")

    parent = work_parent.resolve() if work_parent else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blc-lite-smoke-", dir=parent) as temporary:
        root = Path(temporary)
        local_executable = root / "BiliLiveCut.exe"
        shutil.copy2(executable, local_executable)

        _run_phase(
            local_executable,
            ["--engine-pack", str(engine_pack)],
            root,
            "first-install",
            first_timeout_seconds,
        )
        for relative in (
            "runtime/current.json",
            ".venv/Scripts/python.exe",
            "models/engine-pack-installed.json",
        ):
            required = root / relative
            if not required.is_file():
                raise RuntimeError(f"Lite first-install is missing {required}")

        venv_python = root / ".venv" / "Scripts" / "python.exe"
        subprocess.run([str(venv_python), "-m", "pip", "check"], cwd=root, check=True, timeout=120)

        deadline = time.monotonic() + 15
        while _port_is_open() and time.monotonic() < deadline:
            time.sleep(1)
        if _port_is_open():
            raise RuntimeError("Port 8000 remained occupied after the first Lite launch stopped")

        _run_phase(
            local_executable,
            ["--offline", "--engine-pack", str(engine_pack)],
            root,
            "second-offline",
            second_timeout_seconds,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse artifact directories and smoke timeouts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lite-dir", type=Path, required=True)
    parser.add_argument("--engine-pack-dir", type=Path, required=True)
    parser.add_argument("--work-parent", type=Path)
    parser.add_argument("--first-timeout-seconds", type=int, default=900)
    parser.add_argument("--second-timeout-seconds", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Resolve smoke artifacts, run both phases, and report the result."""
    _configure_console_encoding()
    args = parse_args(argv)
    try:
        version_config = json.loads(VERSION_CONFIG.read_text(encoding="utf-8"))
        version = str(version_config["release_version"])
        executable = _required_file(
            args.lite_dir.resolve(),
            str(version_config["naming"]["lite_exe"]).format(version=version),
            "Lite executable",
        )
        engine_pack = _required_file(
            args.engine_pack_dir.resolve(),
            str(version_config["naming"]["engine_pack_zip"]).format(version=version),
            "Fixture Engine Pack",
        )
        run_smoke(
            executable,
            engine_pack,
            work_parent=args.work_parent,
            first_timeout_seconds=args.first_timeout_seconds,
            second_timeout_seconds=args.second_timeout_seconds,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: Lite fresh online installation and second offline launch OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
