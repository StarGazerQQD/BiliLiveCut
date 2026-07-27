#!/usr/bin/env python3
"""Cross-verify release artifact identity, provenance, and checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_CONFIG = REPO_ROOT / "packaging" / "portable" / "config" / "version.json"
BOOTSTRAP_CONFIG = REPO_ROOT / "packaging" / "portable" / "locks" / "bootstrap-wheels.json"
PROJECT_LICENSE = REPO_ROOT / "LICENSE"
PROJECT_LICENSE_ID = "MIT"
PROJECT_COPYRIGHT = "Copyright (c) 2026 StarGazerQQD"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object or raise a contextual verification error."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return value


def _hashes(path: Path) -> tuple[str, str]:
    """Return streaming SHA-256 and uppercase CRC32 for one artifact."""
    sha256 = hashlib.sha256()
    crc32 = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha256.update(block)
            crc32 = zlib.crc32(block, crc32)
    return sha256.hexdigest(), f"{crc32 & 0xFFFFFFFF:08X}"


def _read_single_checksum(path: Path) -> tuple[str, str]:
    """Read a one-artifact checksum file and reject ambiguous content."""
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        raise RuntimeError(f"Cannot read checksum file {path}: {exc}") from exc
    if len(lines) != 1:
        raise RuntimeError(f"Checksum file must contain exactly one record: {path}")
    parts = lines[0].split(maxsplit=1)
    if len(parts) != 2:
        raise RuntimeError(f"Invalid checksum record: {path}")
    return parts[0], parts[1].strip()


def _require_equal(label: str, actual: object, expected: object) -> None:
    """Raise a stable error when an identity field differs."""
    if actual != expected:
        raise RuntimeError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def _load_project_license() -> bytes:
    """Load the canonical MIT license and reject incomplete release metadata."""
    try:
        content = PROJECT_LICENSE.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Project license is missing or unreadable: {PROJECT_LICENSE}") from exc
    required_markers = (
        "MIT License",
        PROJECT_COPYRIGHT,
        "Permission is hereby granted",
        'THE SOFTWARE IS PROVIDED "AS IS"',
    )
    for marker in required_markers:
        if marker not in text:
            raise RuntimeError(f"Project license is invalid: missing {marker!r}")
    return content


def _verify_build_artifact(
    artifact_dir: Path,
    *,
    artifact_type: str,
    artifact_name: str,
    payload: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    """Verify one Lite/Full artifact and its local build manifest/checksum files."""
    artifact = artifact_dir / artifact_name
    if not artifact.is_file():
        raise RuntimeError(f"Missing {artifact_type} artifact: {artifact}")
    manifest = _load_json(artifact_dir / "build-manifest.json")

    for field in ("release_version", "source_commit", "builder_commit", "payload_sha256"):
        _require_equal(f"{artifact_type}.{field}", manifest.get(field), payload.get(field))
    _require_equal(f"{artifact_type}.artifact_type", manifest.get("artifact_type"), artifact_type)

    sha256, crc32 = _hashes(artifact)
    _require_equal(f"{artifact_type}.artifact_sha256", manifest.get("artifact_sha256"), sha256)
    _require_equal(f"{artifact_type}.artifact_crc32", str(manifest.get("artifact_crc32", "")).upper(), crc32)

    sum_sha256, sum_sha256_name = _read_single_checksum(artifact_dir / "SHA256SUMS.txt")
    sum_crc32, sum_crc32_name = _read_single_checksum(artifact_dir / "CRC32SUMS.txt")
    _require_equal(f"{artifact_type}.SHA256SUMS filename", sum_sha256_name, artifact.name)
    _require_equal(f"{artifact_type}.SHA256SUMS digest", sum_sha256.lower(), sha256)
    _require_equal(f"{artifact_type}.CRC32SUMS filename", sum_crc32_name, artifact.name)
    _require_equal(f"{artifact_type}.CRC32SUMS digest", sum_crc32.upper(), crc32)
    return artifact, manifest


def verify_release_artifacts(
    payload_dir: Path,
    lite_dir: Path,
    full_dir: Path,
    *,
    expected_builder_commit: str,
) -> None:
    """Verify that Payload, Lite, and Full are one coherent release build."""
    version_config = _load_json(VERSION_CONFIG)
    payload = _load_json(payload_dir / "payload_manifest.json")
    version = str(version_config["release_version"])
    source_commit = str(version_config["source_commit_full"])
    source_short = str(version_config["source_commit_short"])
    license_content = _load_project_license()
    license_sha256 = hashlib.sha256(license_content).hexdigest()

    if not COMMIT_RE.fullmatch(expected_builder_commit):
        raise RuntimeError(f"Expected builder commit is not a full lowercase Git SHA: {expected_builder_commit!r}")
    for field in ("release_version", "portable_release_version"):
        _require_equal(f"payload.{field}", payload.get(field), version)
    for field in ("source_commit", "core_source_commit"):
        _require_equal(f"payload.{field}", payload.get(field), source_commit)
    for field in ("source_commit_short", "core_source_commit_short"):
        _require_equal(f"payload.{field}", payload.get(field), source_short)
    _require_equal("payload.builder_commit", payload.get("builder_commit"), expected_builder_commit)
    _require_equal("payload.project_license", payload.get("project_license"), PROJECT_LICENSE_ID)
    _require_equal("payload.project_license_sha256", payload.get("project_license_sha256"), license_sha256)

    payload_zip = payload_dir / str(version_config["naming"]["payload_zip"])
    payload_sha256, _ = _hashes(payload_zip)
    _require_equal("payload.payload_sha256", payload.get("payload_sha256"), payload_sha256)
    with zipfile.ZipFile(payload_zip) as archive:
        payload_license_entries = [name for name in archive.namelist() if name == "LICENSE"]
        if len(payload_license_entries) != 1:
            raise RuntimeError("Payload ZIP must contain exactly one root LICENSE")
        _require_equal("payload embedded LICENSE", archive.read("LICENSE"), license_content)

    identity = {
        "release_version": version,
        "source_commit": source_commit,
        "builder_commit": expected_builder_commit,
        "payload_sha256": payload_sha256,
    }
    lite_name = str(version_config["naming"]["lite_exe"]).format(version=version)
    full_name = str(version_config["naming"]["full_zip"]).format(version=version)
    lite_artifact, lite_manifest = _verify_build_artifact(
        lite_dir,
        artifact_type="lite",
        artifact_name=lite_name,
        payload=identity,
    )
    full_artifact, _full_manifest = _verify_build_artifact(
        full_dir,
        artifact_type="full",
        artifact_name=full_name,
        payload=identity,
    )

    for artifact_type, manifest in (("lite", lite_manifest), ("full", _full_manifest)):
        _require_equal(f"{artifact_type}.project_license", manifest.get("project_license"), PROJECT_LICENSE_ID)
        _require_equal(
            f"{artifact_type}.project_license_sha256",
            manifest.get("project_license_sha256"),
            license_sha256,
        )

    bootstrap_entries = json.loads(BOOTSTRAP_CONFIG.read_text(encoding="utf-8"))
    expected_bootstrap = {str(entry["wheel_filename"]): str(entry["wheel_sha256"]) for entry in bootstrap_entries}
    _require_equal("lite.bootstrap_wheels", lite_manifest.get("bootstrap_wheels"), expected_bootstrap)

    with zipfile.ZipFile(full_artifact) as archive:
        names = archive.namelist()
        roots = {name.split("/", 1)[0] for name in names if name and "/" in name}
        if len(roots) != 1:
            raise RuntimeError(f"Full ZIP must contain exactly one top-level directory, got {sorted(roots)!r}")
        bundle_root = roots.pop()
        embedded_exe = f"{bundle_root}/BiliLiveCut-Portable.exe"
        embedded_checksums = f"{bundle_root}/checksums.json"
        embedded_license = f"{bundle_root}/LICENSE.txt"
        required_entries = (embedded_exe, embedded_checksums, embedded_license)
        if any(names.count(entry) != 1 for entry in required_entries):
            raise RuntimeError("Full ZIP must contain exactly one launcher, checksums.json, and LICENSE.txt")
        embedded_exe_sha256 = hashlib.sha256(archive.read(embedded_exe)).hexdigest()
        embedded_license_content = archive.read(embedded_license)
        checksums = json.loads(archive.read(embedded_checksums).decode("utf-8"))
    lite_sha256, _ = _hashes(lite_artifact)
    _require_equal("full embedded Lite SHA-256", embedded_exe_sha256, lite_sha256)
    _require_equal("full checksums.exe_sha256", checksums.get("exe_sha256"), lite_sha256)
    _require_equal("full checksums.release_version", checksums.get("release_version"), version)
    _require_equal("full checksums.source_commit", checksums.get("source_commit"), source_commit)
    _require_equal("full embedded LICENSE", embedded_license_content, license_content)
    _require_equal("full checksums.project_license", checksums.get("project_license"), PROJECT_LICENSE_ID)
    _require_equal("full checksums.project_license_sha256", checksums.get("project_license_sha256"), license_sha256)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse artifact directories and the expected build commit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-dir", type=Path, required=True)
    parser.add_argument("--lite-dir", type=Path, required=True)
    parser.add_argument("--full-dir", type=Path, required=True)
    parser.add_argument("--expected-builder-commit", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run cross-artifact verification and return a process exit code."""
    args = parse_args(argv)
    try:
        verify_release_artifacts(
            args.payload_dir.resolve(),
            args.lite_dir.resolve(),
            args.full_dir.resolve(),
            expected_builder_commit=args.expected_builder_commit,
        )
    except (OSError, KeyError, RuntimeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("PASS: Payload, Lite, and Full artifact identities and checksums match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
