"""Production model-provisioning orchestration smoke tests.

These tests deliberately cross the managed-interpreter subprocess boundary.
The tiny provider only replaces the multi-gigabyte remote repositories; the
launcher command, helper module, content identities, staging transactions and
installed manifests are the production implementations.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import venv
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pytest import MonkeyPatch

_PORTABLE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PORTABLE_DIR / "src"))
sys.path.insert(0, str(_PORTABLE_DIR / "config"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TINY_PROVIDER_ENV = "BLC_RELEASE_SMOKE_TINY_MODELS"
_FAIL_ENGINE_ENV = "BLC_RELEASE_SMOKE_FAIL_ENGINE"


def _enable_tiny_provider(monkeypatch: MonkeyPatch) -> None:
    """Enable the release-only provider for child processes."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv(_TINY_PROVIDER_ENV, "1")
    monkeypatch.delenv(_FAIL_ENGINE_ENV, raising=False)


def _write_required_files(root: Path, relative_paths: list[str]) -> None:
    """Create deterministic minimal files accepted by the current catalog."""
    for relative in relative_paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"fixture: {relative}\n".encode())


def _prepare_with_real_child(app_root: Path) -> dict[str, Any]:
    """Invoke the production helper using this test process interpreter."""
    from blc_portable.launcher.main import prepare_models

    return prepare_models(Path(sys.executable), app_root)


def test_repair_flag_reaches_real_provisioner(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    from blc_portable.launcher.main import prepare_models

    _enable_tiny_provider(monkeypatch)
    _prepare_with_real_child(tmp_path)
    installed = json.loads((tmp_path / "models/engine-pack-installed.json").read_text(encoding="utf-8"))
    relative = next(iter(installed["engines"]["whisper"]["files"]))
    damaged = tmp_path / "models/whisper" / relative
    original = damaged.read_bytes()
    damaged.write_bytes(b"x" * len(original))
    repaired = prepare_models(Path(sys.executable), tmp_path, repair=True)
    assert repaired["installed_engines"] == ["whisper"]
    assert damaged.read_bytes() == original


def test_clean_install_runs_real_external_provisioner(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """A clean app root crosses the child boundary and commits all engines."""
    _enable_tiny_provider(monkeypatch)

    from blc_portable.launcher.main import prepare_models

    environment = tmp_path / "clean-python"
    venv.EnvBuilder(with_pip=False).create(environment)
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = prepare_models(interpreter, tmp_path)

    assert result["source"] == "online_download"
    assert result["provider"] == "release_smoke_tiny"
    assert result["network_requests"] == 4
    assert result["installed_engines"] == ["whisper", "paraformer", "sensevoice", "funasr_nano"]
    assert Path(result["provisioning_interpreter"]) == interpreter.resolve()
    installed = json.loads((tmp_path / "models" / "engine-pack-installed.json").read_text(encoding="utf-8"))
    assert installed["schema_version"] == 7
    assert set(installed["engines"]) == {"whisper", "paraformer", "sensevoice", "funasr_nano"}


@pytest.mark.parametrize(
    "field,value,expected_requests", [("schema_version", 6, 4), ("release_version", "0.1.18.4-alpha", 0)]
)
def test_schema_change_reprovisions_but_release_provenance_reuses_content(
    tmp_path: Path, monkeypatch: MonkeyPatch, field: str, value: str | int, expected_requests: int
) -> None:
    _enable_tiny_provider(monkeypatch)
    _prepare_with_real_child(tmp_path)
    path = tmp_path / "models/engine-pack-installed.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    old[field] = value
    path.write_text(json.dumps(old), encoding="utf-8")
    result = _prepare_with_real_child(tmp_path)
    assert result["source"] == ("online_download" if expected_requests else "already_installed")
    assert result["network_requests"] == expected_requests
    reused = _prepare_with_real_child(tmp_path)
    assert reused["source"] == "already_installed"


def test_relocated_frozen_provisioner_uses_explicit_config_root(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Frozen ``provisioner`` and ``provisioner-config`` roots may be siblings."""
    from blc_portable.launcher import main as launcher_module

    _enable_tiny_provider(monkeypatch)
    source_root = tmp_path / "frozen" / "provisioner"
    config_root = tmp_path / "frozen" / "provisioner-config"
    shutil.copytree(
        _PORTABLE_DIR / "src" / "blc_portable",
        source_root / "blc_portable",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(_PORTABLE_DIR / "config", config_root)
    shared_config = source_root / "config"
    shared_config.mkdir()
    for name in ("__init__.py", "file_locks.py"):
        shutil.copy2(_PORTABLE_DIR.parents[1] / "config" / name, shared_config / name)
    app_root = tmp_path / "app"
    app_root.mkdir()
    environment = tmp_path / "clean-python"
    venv.EnvBuilder(with_pip=False).create(environment)
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(source_root.parent), raising=False)

    result = launcher_module.prepare_models(interpreter, app_root)

    assert result["provider"] == "release_smoke_tiny"
    assert result["network_requests"] == 4
    assert Path(result["provisioning_interpreter"]) == interpreter.resolve()


def test_hub_failure_preserves_runtime_and_completed_engine_for_retry(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """A later hub failure preserves Whisper and the same child can resume."""
    _enable_tiny_provider(monkeypatch)
    monkeypatch.setenv(_FAIL_ENGINE_ENV, "paraformer")

    with pytest.raises(RuntimeError, match="Injected release-smoke hub unavailable for engine paraformer"):
        _prepare_with_real_child(tmp_path)

    assert Path(sys.executable).is_file()
    assert (tmp_path / "models" / "whisper").is_dir()
    assert not (tmp_path / "models" / "paraformer").exists()

    monkeypatch.delenv(_FAIL_ENGINE_ENV)
    result = _prepare_with_real_child(tmp_path)

    assert result["network_requests"] == 3
    assert result["reused_engines"] == ["whisper"]
    assert set(result["installed_engines"]) == {"paraformer", "sensevoice", "funasr_nano"}


def test_only_stale_engine_is_reprovisioned_through_real_child(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Changing one persisted identity causes exactly one provider request."""
    _enable_tiny_provider(monkeypatch)
    _prepare_with_real_child(tmp_path)
    manifest_path = tmp_path / "models" / "engine-pack-installed.json"
    installed = json.loads(manifest_path.read_text(encoding="utf-8"))
    installed["engines"]["whisper"]["content_fingerprint"] = "0" * 64
    manifest_path.write_text(json.dumps(installed), encoding="utf-8")

    result = _prepare_with_real_child(tmp_path)

    assert result["network_requests"] == 1
    assert result["installed_engines"] == ["whisper"]
    assert set(result["reused_engines"]) == {"paraformer", "sensevoice", "funasr_nano"}


def test_tiny_provider_refuses_non_ci_use(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """Fixture model bytes can never be selected by a normal user launch."""
    from blc_portable.launcher import model_downloader

    monkeypatch.setenv(_TINY_PROVIDER_ENV, "1")
    monkeypatch.delenv("CI", raising=False)

    with pytest.raises(RuntimeError, match="restricted to CI release smoke tests"):
        model_downloader.download_all_engines(tmp_path, config_dir=_PORTABLE_DIR / "config")
