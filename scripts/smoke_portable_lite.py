#!/usr/bin/env python3
"""Run the frozen Lite launcher through production bootstrap and recovery."""

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
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

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


def _port_is_open(web_port: int) -> bool:
    """Return whether the selected Portable Web port already has a listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        return client.connect_ex(("127.0.0.1", web_port)) == 0


def _available_web_port() -> int:
    """Ask Windows for a currently available loopback port for this smoke run."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _print_log(path: Path) -> None:
    """Print a launcher log using tolerant UTF-8 decoding."""
    print(f"==== {path.name} ====", flush=True)
    if path.is_file():
        print(path.read_text(encoding="utf-8", errors="replace"), flush=True)


def _stop_process_tree(process: subprocess.Popen[bytes] | None) -> None:
    """Stop a launcher and its service child."""
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


def _wait_ready(process: subprocess.Popen[bytes], timeout_seconds: int, phase: str, web_port: int) -> None:
    """Wait until the Portable Web root responds successfully."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"Lite {phase} launcher exited before Web became ready (exit={return_code})")
        try:
            web_url = f"http://127.0.0.1:{web_port}/"
            with urllib.request.urlopen(web_url, timeout=5) as response:  # noqa: S310 - fixed localhost host
                if response.status == 200:
                    with urllib.request.urlopen(f"{web_url}api/settings", timeout=5) as settings_response:  # noqa: S310
                        settings = json.loads(settings_response.read().decode("utf-8"))
                    if settings.get("web_port") != web_port or settings.get("current_web_port") != web_port:
                        raise RuntimeError(f"Lite {phase} did not expose the configured/current Web port")
                    if settings.get("restart_required") is not False:
                        raise RuntimeError(f"Lite {phase} incorrectly requires another restart")
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
    environment: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    """Start the frozen launcher without a shell or PowerShell wrapper."""
    startupinfo = None
    creationflags = 0
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
    child_environment = os.environ.copy()
    if environment:
        child_environment.update(environment)
    return subprocess.Popen(
        [str(executable), *arguments],
        cwd=work_dir,
        env=child_environment,
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
    web_port: int,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Start one launcher phase, require Web readiness, then stop it."""
    stdout_path = work_dir / f"{phase}.stdout.log"
    stderr_path = work_dir / f"{phase}.stderr.log"
    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = _start_launcher(executable, arguments, work_dir, stdout, stderr, environment)
            _wait_ready(process, timeout_seconds, phase, web_port)
    finally:
        _stop_process_tree(process)
        _print_log(stdout_path)
        _print_log(stderr_path)
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        for path in (stdout_path, stderr_path)
    )


def _run_expected_failure(
    executable: Path,
    work_dir: Path,
    phase: str,
    timeout_seconds: int,
    expected_text: str,
    environment: Mapping[str, str],
) -> str:
    """Require one frozen launcher invocation to fail with an exact cause."""
    stdout_path = work_dir / f"{phase}.stdout.log"
    stderr_path = work_dir / f"{phase}.stderr.log"
    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = _start_launcher(executable, [], work_dir, stdout, stderr, environment)
            try:
                return_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"Lite {phase} did not fail within {timeout_seconds} seconds") from exc
        if return_code == 0:
            raise RuntimeError(f"Lite {phase} unexpectedly succeeded")
    finally:
        _stop_process_tree(process)
        _print_log(stdout_path)
        _print_log(stderr_path)
    output = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        for path in (stdout_path, stderr_path)
    )
    if expected_text not in output:
        raise RuntimeError(f"Lite {phase} did not preserve expected root failure: {expected_text}")
    return output


def _wait_port_closed(web_port: int, timeout_seconds: int = 20) -> None:
    """Wait for the stopped service tree to release the selected Web port."""
    deadline = time.monotonic() + timeout_seconds
    while _port_is_open(web_port) and time.monotonic() < deadline:
        time.sleep(1)
    if _port_is_open(web_port):
        raise RuntimeError(f"Port {web_port} remained occupied after the Lite launcher stopped")


def _installed_manifest(root: Path) -> dict[str, object]:
    """Read and validate the current installed-model manifest."""
    manifest_path = root / "models" / "engine-pack-installed.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 6 or not isinstance(payload.get("engines"), dict):
        raise RuntimeError(f"Lite smoke produced an invalid installed-model manifest: {manifest_path}")
    return payload


def _installed_engine_ids(root: Path) -> set[str]:
    """Read installed engine IDs from the current content manifest."""
    engines = _installed_manifest(root)["engines"]
    assert isinstance(engines, dict)
    return set(engines)


def run_smoke(
    executable: Path,
    *,
    work_parent: Path | None = None,
    first_timeout_seconds: int = 900,
    second_timeout_seconds: int = 300,
    recovery_timeout_seconds: int = 900,
) -> None:
    """Exercise interrupted online provisioning, reuse, offline and recovery."""
    if sys.platform != "win32":
        raise RuntimeError("Lite executable smoke testing requires Windows")
    web_port = _available_web_port()
    if _port_is_open(web_port):
        raise RuntimeError(f"Port {web_port} is already in use before Lite smoke testing")

    parent = work_parent.resolve() if work_parent else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="blc-lite-smoke-", dir=parent) as temporary:
        root = Path(temporary)
        local_executable = root / "BiliLiveCut.exe"
        shutil.copy2(executable, local_executable)
        launcher_config = root / "config" / "launcher.json"
        launcher_config.parent.mkdir(parents=True)
        launcher_config.write_text(
            json.dumps({"schema": 1, "web_port": web_port}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        base_python = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        if sys.version_info[:2] not in {(3, 11), (3, 12)} or not base_python.is_file():
            raise RuntimeError("Lite smoke driver requires a discoverable Python 3.11/3.12 base interpreter")
        launcher_environment = {
            "PATH": os.pathsep.join((str(base_python.parent), os.environ.get("PATH", ""))),
        }
        provider_environment = {
            **launcher_environment,
            "CI": "true",
            "BLC_RELEASE_SMOKE_TINY_MODELS": "1",
        }
        failing_environment = {
            **provider_environment,
            "BLC_RELEASE_SMOKE_FAIL_ENGINE": "paraformer",
        }

        _run_expected_failure(
            local_executable,
            root,
            "interrupted-provisioning",
            first_timeout_seconds,
            "Injected release-smoke hub unavailable for engine paraformer",
            failing_environment,
        )
        for relative in (
            "runtime/current.json",
            ".venv/Scripts/python.exe",
            "models/whisper",
            "models/engine-pack-installed.json",
        ):
            required = root / relative
            if not required.exists():
                raise RuntimeError(f"Lite interrupted bootstrap did not preserve {required}")
        if _installed_engine_ids(root) != {"whisper"}:
            raise RuntimeError("Interrupted provisioning did not preserve exactly the completed Whisper engine")
        interrupted_manifest = _installed_manifest(root)
        interrupted_engines = interrupted_manifest["engines"]
        assert isinstance(interrupted_engines, dict)
        whisper_record = interrupted_engines["whisper"]

        _wait_port_closed(web_port)
        resume_output = _run_phase(
            local_executable,
            [],
            root,
            "resume-provisioning",
            first_timeout_seconds,
            web_port,
            provider_environment,
        )
        if f"http://127.0.0.1:{web_port}" not in resume_output:
            raise RuntimeError("Lite Launcher output did not report the persisted Web port")
        for relative in (
            "runtime/current.json",
            ".venv/Scripts/python.exe",
            "models/engine-pack-installed.json",
        ):
            required = root / relative
            if not required.is_file():
                raise RuntimeError(f"Lite resumed installation is missing {required}")
        expected_engines = {"whisper", "paraformer", "sensevoice", "funasr_nano"}
        if _installed_engine_ids(root) != expected_engines:
            raise RuntimeError("Lite resumed installation did not commit all four engines")
        resumed_manifest = _installed_manifest(root)
        resumed_engines = resumed_manifest["engines"]
        assert isinstance(resumed_engines, dict)
        if resumed_engines["whisper"] != whisper_record:
            raise RuntimeError("Resumed provisioning replaced the previously committed Whisper engine")

        venv_python = root / ".venv" / "Scripts" / "python.exe"
        subprocess.run([str(venv_python), "-m", "pip", "check"], cwd=root, check=True, timeout=120)

        _wait_port_closed(web_port)
        _run_phase(
            local_executable,
            ["--offline"],
            root,
            "second-offline",
            second_timeout_seconds,
            web_port,
            launcher_environment,
        )

        _wait_port_closed(web_port)
        models_before_recovery = _installed_manifest(root)["engines"]
        assert isinstance(models_before_recovery, dict)
        venv_python.write_bytes(b"corrupt release-smoke interpreter")
        _run_phase(
            local_executable,
            [],
            root,
            "damaged-venv-recovery",
            recovery_timeout_seconds,
            web_port,
            provider_environment,
        )
        models_after_recovery = _installed_manifest(root)["engines"]
        if models_after_recovery != models_before_recovery:
            raise RuntimeError("Damaged-venv recovery unexpectedly replaced persistent model assets")
        version_probe = subprocess.run(
            [str(venv_python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        if version_probe.stdout.strip() not in {"3.11", "3.12"}:
            raise RuntimeError(f"Recovered venv has unsupported Python: {version_probe.stdout.strip()}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse artifact directories and smoke timeouts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lite-dir", type=Path, required=True)
    parser.add_argument("--work-parent", type=Path)
    parser.add_argument("--first-timeout-seconds", type=int, default=900)
    parser.add_argument("--second-timeout-seconds", type=int, default=300)
    parser.add_argument("--recovery-timeout-seconds", type=int, default=900)
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
        run_smoke(
            executable,
            work_parent=args.work_parent,
            first_timeout_seconds=args.first_timeout_seconds,
            second_timeout_seconds=args.second_timeout_seconds,
            recovery_timeout_seconds=args.recovery_timeout_seconds,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: Lite production bootstrap, interruption, offline reuse and damaged-venv recovery OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
