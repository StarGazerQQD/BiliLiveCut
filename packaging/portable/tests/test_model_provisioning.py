"""Content-addressed, resumable model-provisioning regressions."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pytest import MonkeyPatch

_portable_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_portable_dir / "src"))
sys.path.insert(0, str(_portable_dir / "config"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_pack_helpers import legacy_installed_manifest  # noqa: E402


def _write_files(root: Path, relative_paths: list[str], payload: bytes = b"fixture") -> None:
    for relative in relative_paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _create_minimal_engine(engine: Any, models_dir: Path) -> None:
    target = models_dir / engine.engine_id
    target.mkdir(parents=True, exist_ok=True)
    _write_files(target, list(engine.required_files))


def test_legacy_017_provisioning_migrates_with_zero_network(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """An unchanged schema-5 installation is adopted without any SDK call."""
    from blc_portable.launcher import model_downloader
    from model_catalog import load_engines

    models_dir = tmp_path / "models"
    for engine in load_engines():
        _create_minimal_engine(engine, models_dir)
    (models_dir / "engine-pack-installed.json").write_text(
        json.dumps(legacy_installed_manifest(models_dir)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        model_downloader,
        "download_all_engines",
        lambda *_args, **_kwargs: pytest.fail("unchanged legacy content must not use the network"),
    )

    result = model_downloader.provision_models(
        tmp_path,
        config_dir=_portable_dir / "config",
        expected_filename="BiliLiveCut-EnginePack-0.1.18.0-alpha.zip",
        expected_crc32="",
        expected_sha256="",
        user_engine_pack_path=None,
        offline=False,
        fallback_online=False,
    )

    assert result["source"] == "already_installed"
    assert result["network_requests"] == 0
    migrated = json.loads((models_dir / "engine-pack-installed.json").read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 6


def test_legacy_manifest_is_not_blessed_after_model_revision_changes(tmp_path: Path) -> None:
    """The one-time migration must fail when the current desired content changed."""
    from dataclasses import replace

    from blc_portable.engine_pack.installer import check_installed_models
    from model_catalog import load_engines

    engines = load_engines()
    models_dir = tmp_path / "models"
    for engine in engines:
        _create_minimal_engine(engine, models_dir)
    (models_dir / "engine-pack-installed.json").write_text(
        json.dumps(legacy_installed_manifest(models_dir)),
        encoding="utf-8",
    )
    changed = [
        replace(engine, resolved_revision="f" * 40) if engine.engine_id == "whisper" else engine for engine in engines
    ]

    ok, errors = check_installed_models(models_dir, desired_engines=changed)

    assert not ok
    assert any("identities differ" in error for error in errors)
    assert json.loads((models_dir / "engine-pack-installed.json").read_text(encoding="utf-8"))["schema_version"] == 5


def test_completed_engine_staging_resumes_without_network(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """A completed fingerprint staging directory survives restart and commits offline."""
    from blc_portable.engine_pack.identity import desired_engine_records
    from blc_portable.engine_pack.installer import install_engine_from_staging
    from blc_portable.launcher import model_downloader
    from model_catalog import load_engines

    engines = load_engines()
    for engine in engines[1:]:
        staged = tmp_path / ".seed" / engine.engine_id
        _write_files(staged, list(engine.required_files))
        install_engine_from_staging(tmp_path, engine.engine_id, staged)

    desired = desired_engine_records(engines)
    whisper = engines[0]
    fingerprint = str(desired[whisper.engine_id]["content_fingerprint"])
    staging = tmp_path / ".model-staging" / f"{whisper.engine_id}-{fingerprint[:16]}"
    _write_files(staging, list(whisper.required_files))
    (staging / ".provision-complete.json").write_text(
        json.dumps({"content_fingerprint": fingerprint}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        model_downloader,
        "_download_hf_model",
        lambda *_args, **_kwargs: pytest.fail("completed staging must avoid Hugging Face"),
    )
    monkeypatch.setattr(
        model_downloader,
        "_download_ms_model",
        lambda *_args, **_kwargs: pytest.fail("reusable engines must avoid ModelScope"),
    )

    result = model_downloader.download_all_engines(tmp_path, config_dir=_portable_dir / "config")

    assert result["network_requests"] == 0
    assert result["resumed_staging"] == ["whisper"]
    assert (tmp_path / "models" / "whisper" / whisper.required_files[0]).is_file()


def test_later_download_failure_preserves_earlier_engine_success(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    """A later engine failure cannot roll back an already committed engine."""
    from blc_portable.engine_pack.installer import reusable_engine_ids
    from blc_portable.launcher import model_downloader
    from model_catalog import get_engine_by_id

    whisper = get_engine_by_id("whisper")
    assert whisper is not None

    def fake_hf(_repo: str, target: Path, _revision: str | None, _mirror: str | None) -> None:
        _write_files(target, list(whisper.required_files))

    def fail_modelscope(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected second-engine failure")

    monkeypatch.setattr(model_downloader, "_download_hf_model", fake_hf)
    monkeypatch.setattr(model_downloader, "_download_ms_model", fail_modelscope)

    with pytest.raises(RuntimeError, match="injected second-engine failure"):
        model_downloader.download_all_engines(tmp_path, config_dir=_portable_dir / "config")

    reusable, _ = reusable_engine_ids(tmp_path / "models", full_rehash=True)
    assert "whisper" in reusable
    assert (tmp_path / "models" / "whisper").is_dir()


def test_fingerprint_ignores_display_and_release_metadata() -> None:
    """Only immutable model content inputs affect an engine fingerprint."""
    from dataclasses import replace

    from blc_portable.engine_pack.identity import catalog_engine_identity, engine_fingerprint
    from model_catalog import get_engine_by_id

    engine = get_engine_by_id("sensevoice")
    assert engine is not None
    renamed = replace(engine, display_name="A future app release label")

    assert engine_fingerprint(catalog_engine_identity(engine)) == engine_fingerprint(catalog_engine_identity(renamed))
