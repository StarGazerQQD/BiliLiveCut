"""Release artifacts must share one verified identity and provenance chain."""

from __future__ import annotations

import hashlib
import json
import zipfile
import zlib
from pathlib import Path

import pytest

from scripts import verify_release_artifacts


def _write_sums(directory: Path, artifact: Path) -> tuple[str, str]:
    content = artifact.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    crc32 = f"{zlib.crc32(content) & 0xFFFFFFFF:08X}"
    (directory / "SHA256SUMS.txt").write_text(f"{sha256}  {artifact.name}\n", encoding="utf-8")
    (directory / "CRC32SUMS.txt").write_text(f"{crc32}  {artifact.name}\n", encoding="utf-8")
    return sha256, crc32


def _build_release_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    version_config = json.loads(verify_release_artifacts.VERSION_CONFIG.read_text(encoding="utf-8"))
    bootstrap_entries = json.loads(verify_release_artifacts.BOOTSTRAP_CONFIG.read_text(encoding="utf-8"))
    version = version_config["release_version"]
    source_commit = version_config["source_commit_full"]
    source_short = version_config["source_commit_short"]
    builder_commit = "a" * 40

    payload_dir = tmp_path / "payload"
    lite_dir = tmp_path / "lite"
    full_dir = tmp_path / "full"
    for directory in (payload_dir, lite_dir, full_dir):
        directory.mkdir()

    payload_zip = payload_dir / version_config["naming"]["payload_zip"]
    payload_zip.write_bytes(b"payload")
    payload_sha256 = hashlib.sha256(payload_zip.read_bytes()).hexdigest()
    payload_manifest = {
        "release_version": version,
        "portable_release_version": version,
        "source_commit": source_commit,
        "core_source_commit": source_commit,
        "source_commit_short": source_short,
        "core_source_commit_short": source_short,
        "builder_commit": builder_commit,
        "payload_sha256": payload_sha256,
    }
    (payload_dir / "payload_manifest.json").write_text(json.dumps(payload_manifest), encoding="utf-8")

    lite = lite_dir / version_config["naming"]["lite_exe"].format(version=version)
    lite.write_bytes(b"lite executable")
    lite_sha256, lite_crc32 = _write_sums(lite_dir, lite)
    bootstrap_wheels = {entry["wheel_filename"]: entry["wheel_sha256"] for entry in bootstrap_entries}
    lite_manifest = {
        **{
            key: payload_manifest[key]
            for key in ("release_version", "source_commit", "builder_commit", "payload_sha256")
        },
        "artifact_type": "lite",
        "artifact_sha256": lite_sha256,
        "artifact_crc32": lite_crc32,
        "bootstrap_wheels": bootstrap_wheels,
    }
    (lite_dir / "build-manifest.json").write_text(json.dumps(lite_manifest), encoding="utf-8")

    full = full_dir / version_config["naming"]["full_zip"].format(version=version)
    root = f"BiliLiveCut-Portable-Full-{version}-x64"
    with zipfile.ZipFile(full, "w") as archive:
        archive.writestr(f"{root}/BiliLiveCut-Portable.exe", lite.read_bytes())
        archive.writestr(
            f"{root}/checksums.json",
            json.dumps(
                {
                    "release_version": version,
                    "source_commit": source_commit,
                    "exe_sha256": lite_sha256,
                }
            ),
        )
    full_sha256, full_crc32 = _write_sums(full_dir, full)
    full_manifest = {
        **{
            key: payload_manifest[key]
            for key in ("release_version", "source_commit", "builder_commit", "payload_sha256")
        },
        "artifact_type": "full",
        "artifact_sha256": full_sha256,
        "artifact_crc32": full_crc32,
    }
    (full_dir / "build-manifest.json").write_text(json.dumps(full_manifest), encoding="utf-8")
    return payload_dir, lite_dir, full_dir, builder_commit


def test_release_artifact_identity_accepts_coherent_build(tmp_path: Path) -> None:
    payload_dir, lite_dir, full_dir, builder_commit = _build_release_fixture(tmp_path)

    verify_release_artifacts.verify_release_artifacts(
        payload_dir,
        lite_dir,
        full_dir,
        expected_builder_commit=builder_commit,
    )


def test_release_artifact_identity_rejects_tampered_lite(tmp_path: Path) -> None:
    payload_dir, lite_dir, full_dir, builder_commit = _build_release_fixture(tmp_path)
    lite = next(lite_dir.glob("*.exe"))
    lite.write_bytes(lite.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="lite.artifact_sha256 mismatch"):
        verify_release_artifacts.verify_release_artifacts(
            payload_dir,
            lite_dir,
            full_dir,
            expected_builder_commit=builder_commit,
        )


def test_release_artifact_identity_rejects_wrong_builder_commit(tmp_path: Path) -> None:
    payload_dir, lite_dir, full_dir, _builder_commit = _build_release_fixture(tmp_path)

    with pytest.raises(RuntimeError, match="payload.builder_commit mismatch"):
        verify_release_artifacts.verify_release_artifacts(
            payload_dir,
            lite_dir,
            full_dir,
            expected_builder_commit="b" * 40,
        )
