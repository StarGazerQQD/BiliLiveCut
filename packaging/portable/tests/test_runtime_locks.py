"""Portable runtime lock and offline release workflow contracts."""

from __future__ import annotations

import re
import runpy
from pathlib import Path

_portable_dir = Path(__file__).resolve().parent.parent
_repo_root = _portable_dir.parent.parent
_lock_dir = _portable_dir / "locks"
_entry_pattern = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9.-]*)==(?P<version>\S+) "
    r"--hash=sha256:(?P<sha256>[0-9a-f]{64})  # (?P<wheel>\S+\.whl)$"
)


def _canonicalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_lock(abi: str) -> dict[str, tuple[str, str, str]]:
    path = _lock_dir / f"requirements-runtime-{abi}-win-x64.lock"
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: dict[str, tuple[str, str, str]] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        match = _entry_pattern.fullmatch(line)
        assert match, f"Invalid strict-hash lock entry in {path.name}: {line}"
        name = match.group("name")
        assert name not in entries, f"Duplicate package in {path.name}: {name}"
        entries[name] = (match.group("version"), match.group("sha256"), match.group("wheel"))

    package_header = next(line for line in lines if line.startswith("# Packages:"))
    assert int(package_header.partition(":")[2]) == len(entries)
    assert list(entries) == sorted(entries), f"{path.name} must be sorted by canonical package name"
    return entries


def _direct_requirement_names() -> set[str]:
    names = set()
    for raw_line in (_lock_dir / "requirements-runtime.in").read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            name = line.partition("==")[0].split("[", 1)[0]
            names.add(_canonicalize(name))
    return names


def test_runtime_locks_are_complete_and_strict() -> None:
    py311 = _load_lock("py311")
    py312 = _load_lock("py312")

    assert len(py311) == len(py312) == 108
    assert set(py311) == set(py312)
    assert _direct_requirement_names() <= set(py311)


def test_runtime_locks_cover_core_application_imports() -> None:
    required = {
        "brotli",
        "fastapi",
        "loguru",
        "numpy",
        "pydantic-settings",
        "pyyaml",
        "rich",
        "sqlmodel",
        "torch",
        "torchaudio",
        "typer",
        "uvicorn",
    }
    assert required <= _direct_requirement_names()


def test_runtime_locks_do_not_cross_contaminate_cpython_abis() -> None:
    py311 = _load_lock("py311")
    py312 = _load_lock("py312")

    assert not any("cp312" in wheel for _version, _sha, wheel in py311.values())
    assert not any("cp311-cp311" in wheel for _version, _sha, wheel in py312.values())


def test_source_only_wheel_hashes_are_part_of_both_locks() -> None:
    script = runpy.run_path(str(_repo_root / "scripts" / "build_portable_runtime_wheels.py"))
    source_wheels = script["SOURCE_WHEELS"]
    py311 = _load_lock("py311")
    py312 = _load_lock("py312")

    assert len(source_wheels) == 5
    for package in source_wheels:
        name = _canonicalize(package.wheel_filename.split("-", 1)[0])
        assert package.source_sha256 and len(package.source_sha256) == 64
        assert package.wheel_sha256 and len(package.wheel_sha256) == 64
        assert py311[name][1] == package.wheel_sha256
        assert py312[name][1] == package.wheel_sha256


def test_release_workflow_performs_real_offline_installs() -> None:
    workflow = (_repo_root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "scripts/build_portable_runtime_wheels.py" in workflow
    assert workflow.count("--require-hashes") >= 4
    assert workflow.count("--no-index") >= 2
    assert 'portable-python\\python.exe" -m venv' in workflow
    assert "Full bundle offline installation OK" in workflow
