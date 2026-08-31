"""Production model-provisioning orchestration smoke tests.

These tests deliberately cross the managed-interpreter subprocess boundary.
The tiny provider only replaces the multi-gigabyte remote repositories; the
launcher command, helper module, content identities, staging transactions and
installed manifests are the production implementations.
"""

from __future__ import annotations

import json
import shutil
import sys
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


def test_clean_install_runs_real_external_provisioner(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """A clean app root crosses the child boundary and commits all engines."""
    _enable_tiny_provider(monkeypatch)

    result = _prepare_with_real_child(tmp_path)

    assert result["source"] == "online_download"
    assert result["provider"] == "release_smoke_tiny"
    assert result["network_requests"] == 4
    assert result["installed_engines"] == ["whisper", "paraformer", "sensevoice", "funasr_nano"]
    assert Path(result["provisioning_interpreter"]) == Path(sys.executable).resolve()
    installed = json.loads((tmp_path / "models" / "engine-pack-installed.json").read_text(encoding="utf-8"))
    assert installed["schema_version"] == 6
    assert set(installed["engines"]) == {"whisper", "paraformer", "sensevoice", "funasr_nano"}


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
    app_root = tmp_path / "app"
    app_root.mkdir()
    monkeypatch.setattr(launcher_module, "_provisioner_paths", lambda: (source_root, config_root))

    result = launcher_module.prepare_models(Path(sys.executable), app_root)

    assert result["provider"] == "release_smoke_tiny"
    assert result["network_requests"] == 4
    assert Path(result["provisioning_interpreter"]) == Path(sys.executable).resolve()


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
