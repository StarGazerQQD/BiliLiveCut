"""Content-addressed Engine Pack discovery, verification and installation."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import uuid
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from blc_portable.atomic_fs import replace_with_retry

from .identity import (
    IDENTITY_SCHEMA_VERSION,
    desired_engine_records,
    engine_fingerprint,
    manifest_engine_identity,
    model_set_fingerprint,
)

CHUNK_SIZE = 8 * 1024 * 1024
INSTALLED_MANIFEST_NAME = "engine-pack-installed.json"
INSTALLED_MANIFEST_SCHEMA = 6
_LEGACY_INSTALLED_MANIFEST_SCHEMA = 5
_LEGACY_0174_FINGERPRINTS = {
    "whisper": "fec19a13490e9f98e758e0b18b4a13ea8e189cb77720a0671f5e425ff103e706",
    "paraformer": "9ed3037841a19f59070ccb0a8e06f7e91c3cde45c7f5d64fe3d5a302929165a9",
    "sensevoice": "2e7868d69e4b0a289b20e5f8dcb385397e4112d4a9c0ac01c0de6579b11dbadc",
    "funasr_nano": "999153916f7dd6e89b8a5aa4516c2df7be666bd9d034241a634ef8d8c7a86296",
}
_CURRENT_MANIFEST_FIELDS = {
    "schema_version",
    "identity_schema_version",
    "model_set_fingerprint",
    "installed_at",
    "engines",
}
_ENGINE_RECORD_FIELDS = {
    "content_fingerprint",
    "identity",
    "installation_source",
    "zip_sha256",
    "installed_at",
    "target_path",
    "file_count",
    "total_size",
    "files",
}
_LEGACY_MANIFEST_FIELDS = {
    "schema_version",
    "engine_pack_version",
    "installation_source",
    "zip_sha256",
    "engine_ids",
    "file_count",
    "total_size_bytes",
    "installed_at",
    "source_commit",
    "files",
}
_FILE_INFO_FIELDS = {"target_path", "file_count", "total_size", "files"}


def compute_crc32(path: Path) -> str:
    """Stream one file and return an uppercase CRC32 digest."""
    crc_value = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            crc_value = zlib.crc32(chunk, crc_value)
    return f"{crc_value & 0xFFFFFFFF:08X}"


def compute_sha256(path: Path) -> str:
    """Stream one file and return a lowercase SHA-256 digest."""
    import hashlib

    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_local_engine_packs(
    app_root: Path,
    expected_filename: str,
    user_path: str | None = None,
) -> list[Path]:
    """Return candidate packs without making the filename part of compatibility."""
    if user_path:
        selected = Path(user_path)
        if selected.is_file():
            return [selected]
        if not selected.is_dir():
            print(f"  [警告] 用户指定路径不存在: {user_path}")
            return []
        roots = [selected]
    else:
        roots = [app_root, app_root / "packages"]

    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        exact = root / expected_filename
        ordered = ([exact] if exact.is_file() else []) + sorted(
            root.glob("BiliLiveCut-EnginePack-*.zip"),
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
        for candidate in ordered:
            resolved = candidate.resolve()
            if resolved not in seen:
                candidates.append(candidate)
                seen.add(resolved)
    return candidates


def _safe_extract(zip_path: Path, target_dir: Path) -> None:
    """Safely extract a pack using the shared archive limits."""
    from blc_portable.archive.safe_zip import safe_extract

    with zipfile.ZipFile(zip_path) as archive:
        safe_extract(archive, target_dir)


def _catalog_engines() -> list[Any]:
    """Load the current immutable model catalog."""
    import sys

    configured = os.environ.get("BLC_MODEL_CONFIG_DIR")
    config_root = Path(configured) if configured else Path(__file__).resolve().parent.parent.parent.parent / "config"
    config_dir = str(config_root.resolve())
    if config_dir not in sys.path:
        sys.path.insert(0, config_dir)
    from model_catalog import load_engines

    return list(load_engines())


def _desired_records(engines: Sequence[Any] | None = None) -> dict[str, dict[str, object]]:
    """Return current desired identities keyed by engine ID."""
    return desired_engine_records(engines or _catalog_engines())


def _read_installed_manifest(models_dir: Path) -> dict[str, Any] | None:
    """Read the installed-model manifest, returning ``None`` on corruption."""
    path = models_dir / INSTALLED_MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return raw if isinstance(raw, dict) else None


def _collect_engine_files(engine_dir: Path) -> dict[str, dict[str, object]]:
    """Collect a strict file hash manifest for one engine directory."""
    entries: dict[str, dict[str, object]] = {}
    for path in sorted(engine_dir.rglob("*")):
        if path.is_file():
            entries[path.relative_to(engine_dir).as_posix()] = {
                "size": path.stat().st_size,
                "sha256": compute_sha256(path),
            }
    return entries


def _new_engine_record(
    engine_id: str,
    engine_dir: Path,
    desired: Mapping[str, object],
    *,
    installation_source: str,
    zip_sha256: str | None,
) -> dict[str, object]:
    """Create one schema-6 installed-engine record."""
    entries = _collect_engine_files(engine_dir)
    if not entries:
        raise RuntimeError(f"Engine directory is empty: {engine_id}")
    return {
        "content_fingerprint": desired["content_fingerprint"],
        "identity": desired["identity"],
        "installation_source": installation_source,
        "zip_sha256": zip_sha256,
        "installed_at": dt.datetime.now(dt.UTC).isoformat(),
        "target_path": f"models/{engine_id}",
        "file_count": len(entries),
        "total_size": sum(int(entry["size"]) for entry in entries.values()),
        "files": entries,
    }


def _write_current_manifest(
    models_dir: Path,
    engine_records: Mapping[str, Mapping[str, object]],
    desired: Mapping[str, Mapping[str, object]],
) -> None:
    """Atomically write the release-independent installed-model manifest."""
    models_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": INSTALLED_MANIFEST_SCHEMA,
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "model_set_fingerprint": model_set_fingerprint(desired),
        "installed_at": dt.datetime.now(dt.UTC).isoformat(),
        "engines": {engine_id: dict(record) for engine_id, record in sorted(engine_records.items())},
    }
    temporary = models_dir / f"{INSTALLED_MANIFEST_NAME}.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    replace_with_retry(temporary, models_dir / INSTALLED_MANIFEST_NAME)


def _validate_file_info(engine_id: str, info: object) -> list[str]:
    """Validate one persisted file-info record without touching disk."""
    errors: list[str] = []
    if not isinstance(info, dict) or set(info) != _FILE_INFO_FIELDS:
        return [f"Installed manifest files[{engine_id}] fields invalid"]
    if info["target_path"] != f"models/{engine_id}":
        errors.append(f"Installed manifest files[{engine_id}].target_path invalid")
    entries = info["files"]
    if not isinstance(entries, dict) or not entries:
        return [*errors, f"Installed manifest files[{engine_id}].files must be non-empty"]
    total_size = 0
    for relative, entry in entries.items():
        relative_path = Path(str(relative))
        if not isinstance(relative, str) or not relative or relative_path.is_absolute() or ".." in relative_path.parts:
            errors.append(f"Installed manifest path invalid: {engine_id}/{relative}")
            continue
        if not isinstance(entry, dict) or set(entry) != {"size", "sha256"}:
            errors.append(f"Installed manifest file entry invalid: {engine_id}/{relative}")
            continue
        size = entry["size"]
        digest = entry["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            errors.append(f"Installed manifest size invalid: {engine_id}/{relative}")
            continue
        if not isinstance(digest, str) or len(digest) != 64:
            errors.append(f"Installed manifest sha256 invalid: {engine_id}/{relative}")
            continue
        total_size += size
    if info["file_count"] != len(entries):
        errors.append(f"Installed manifest file_count invalid: {engine_id}")
    if info["total_size"] != total_size:
        errors.append(f"Installed manifest total_size invalid: {engine_id}")
    return errors


def _verify_engine_files(engine_id: str, engine_dir: Path, info: Mapping[str, object]) -> list[str]:
    """Fully compare one installed engine with its persisted hash list."""
    errors: list[str] = []
    entries = info["files"]
    if not isinstance(entries, dict):
        return [f"Engine file list invalid: {engine_id}"]
    expected_paths = set(entries)
    for relative, raw_entry in entries.items():
        if not isinstance(raw_entry, dict):
            errors.append(f"Engine file entry invalid: {engine_id}/{relative}")
            continue
        target = engine_dir / relative
        if not target.is_file():
            errors.append(f"Missing: {engine_id}/{relative}")
            continue
        if target.stat().st_size != raw_entry["size"]:
            errors.append(f"Size mismatch: {engine_id}/{relative}")
        if compute_sha256(target) != raw_entry["sha256"]:
            errors.append(f"SHA-256 mismatch: {engine_id}/{relative}")
    actual_paths = {path.relative_to(engine_dir).as_posix() for path in engine_dir.rglob("*") if path.is_file()}
    for relative in sorted(actual_paths - expected_paths):
        errors.append(f"Extra file: {engine_id}/{relative}")
    return errors


def _migrate_legacy_manifest(
    models_dir: Path,
    legacy: Mapping[str, object],
    desired: Mapping[str, Mapping[str, object]],
) -> dict[str, Any]:
    """Migrate the one supported 0.1.17 schema after a complete local rehash."""
    if set(legacy) != _LEGACY_MANIFEST_FIELDS:
        raise RuntimeError("Legacy installed manifest fields do not match schema 5")
    version = legacy["engine_pack_version"]
    if version != "0.1.17.4-alpha":
        raise RuntimeError("Only the audited 0.1.17.4 schema-5 installed manifest can be migrated")
    current_fingerprints = {key: str(value["content_fingerprint"]) for key, value in desired.items()}
    if current_fingerprints != _LEGACY_0174_FINGERPRINTS:
        raise RuntimeError("Legacy 0.1.17.4 model identities differ from the current catalog")
    engine_ids = legacy["engine_ids"]
    files = legacy["files"]
    if not isinstance(engine_ids, list) or set(engine_ids) != set(desired) or not isinstance(files, dict):
        raise RuntimeError("Legacy installed manifest engine set does not match the current catalog")
    records: dict[str, dict[str, object]] = {}
    for engine_id in sorted(desired):
        info = files.get(engine_id)
        validation_errors = _validate_file_info(engine_id, info)
        if validation_errors:
            raise RuntimeError("Legacy installed manifest invalid: " + "; ".join(validation_errors))
        assert isinstance(info, dict)
        disk_errors = _verify_engine_files(engine_id, models_dir / engine_id, info)
        if disk_errors:
            raise RuntimeError("Legacy installed models changed: " + "; ".join(disk_errors[:5]))
        records[engine_id] = {
            "content_fingerprint": desired[engine_id]["content_fingerprint"],
            "identity": desired[engine_id]["identity"],
            "installation_source": "legacy_migration",
            "zip_sha256": legacy.get("zip_sha256"),
            "installed_at": dt.datetime.now(dt.UTC).isoformat(),
            "target_path": info["target_path"],
            "file_count": info["file_count"],
            "total_size": info["total_size"],
            "files": info["files"],
        }
    _write_current_manifest(models_dir, records, desired)
    migrated = _read_installed_manifest(models_dir)
    if migrated is None:
        raise RuntimeError("Migrated installed manifest could not be read")
    return migrated


def _load_current_or_migrate(
    models_dir: Path,
    desired: Mapping[str, Mapping[str, object]],
    *,
    migrate_legacy: bool,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Load schema 6 or perform the explicit schema-5 migration."""
    installed = _read_installed_manifest(models_dir)
    if installed is None:
        return None, [f"{INSTALLED_MANIFEST_NAME} 不存在或损坏"]
    if installed.get("schema_version") == _LEGACY_INSTALLED_MANIFEST_SCHEMA and migrate_legacy:
        from blc_portable.archive.locks import FileLock, get_engine_pack_lock_path

        try:
            with FileLock(get_engine_pack_lock_path(models_dir.parent)).acquire(timeout=120):
                installed = _read_installed_manifest(models_dir)
                if installed is None:
                    return None, [f"{INSTALLED_MANIFEST_NAME} 不存在或损坏"]
                if installed.get("schema_version") == _LEGACY_INSTALLED_MANIFEST_SCHEMA:
                    installed = _migrate_legacy_manifest(models_dir, installed, desired)
        except RuntimeError as exc:
            return None, [str(exc)]
    if installed.get("schema_version") != INSTALLED_MANIFEST_SCHEMA:
        return None, [f"Installed manifest schema unsupported: {installed.get('schema_version')}"]
    if set(installed) != _CURRENT_MANIFEST_FIELDS:
        return None, ["Installed manifest schema-6 fields invalid"]
    if installed.get("identity_schema_version") != IDENTITY_SCHEMA_VERSION:
        return None, ["Installed manifest identity schema mismatch"]
    if not isinstance(installed.get("engines"), dict):
        return None, ["Installed manifest engines must be an object"]
    return installed, []


def reusable_engine_ids(
    models_dir: Path,
    *,
    full_rehash: bool = False,
    migrate_legacy: bool = True,
    desired_engines: Sequence[Any] | None = None,
) -> tuple[set[str], list[str]]:
    """Return engines whose content fingerprint and local files are reusable."""
    desired = _desired_records(desired_engines)
    installed, errors = _load_current_or_migrate(models_dir, desired, migrate_legacy=migrate_legacy)
    if installed is None:
        return set(), errors
    engine_records = installed["engines"]
    reusable: set[str] = set()
    for engine_id, desired_record in desired.items():
        raw = engine_records.get(engine_id)
        if not isinstance(raw, dict) or set(raw) != _ENGINE_RECORD_FIELDS:
            errors.append(f"Installed engine record invalid: {engine_id}")
            continue
        if raw.get("content_fingerprint") != desired_record["content_fingerprint"]:
            errors.append(f"Content fingerprint mismatch: {engine_id}")
            continue
        if raw.get("identity") != desired_record["identity"]:
            errors.append(f"Content identity mismatch: {engine_id}")
            continue
        info = {
            "target_path": raw.get("target_path"),
            "file_count": raw.get("file_count"),
            "total_size": raw.get("total_size"),
            "files": raw.get("files"),
        }
        info_errors = _validate_file_info(engine_id, info)
        if info_errors:
            errors.extend(info_errors)
            continue
        engine_dir = models_dir / engine_id
        if not engine_dir.is_dir() or not any(engine_dir.iterdir()):
            errors.append(f"Engine directory missing or empty: {engine_id}")
            continue
        if full_rehash:
            disk_errors = _verify_engine_files(engine_id, engine_dir, info)
            if disk_errors:
                errors.extend(disk_errors)
                continue
        reusable.add(engine_id)
    return reusable, errors


def check_installed_models(
    models_dir: Path,
    full_rehash: bool = False,
    *,
    migrate_legacy: bool = True,
    desired_engines: Sequence[Any] | None = None,
) -> tuple[bool, list[str]]:
    """Check the complete desired model set by content identity."""
    desired = _desired_records(desired_engines)
    reusable, errors = reusable_engine_ids(
        models_dir,
        full_rehash=full_rehash,
        migrate_legacy=migrate_legacy,
        desired_engines=desired_engines,
    )
    missing = set(desired) - reusable
    if missing:
        errors.append(f"Missing or stale engines: {sorted(missing)}")
    return not missing, errors


def _existing_records(
    models_dir: Path,
    desired: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Return valid schema-6 records, migrating legacy state when possible."""
    installed, _ = _load_current_or_migrate(models_dir, desired, migrate_legacy=False)
    if installed is None:
        return {}
    records = installed.get("engines")
    return {key: dict(value) for key, value in records.items() if isinstance(value, dict)}


def _install_engine_directory(
    app_root: Path,
    engine_id: str,
    source_dir: Path,
    desired_record: Mapping[str, object],
    *,
    installation_source: str,
    zip_sha256: str | None,
    desired: Mapping[str, Mapping[str, object]],
) -> None:
    """Atomically replace one engine and persist it before proceeding."""
    if not source_dir.is_dir() or not any(source_dir.iterdir()):
        raise RuntimeError(f"Engine staging directory is empty: {engine_id}")
    models_dir = app_root / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / engine_id
    incoming = models_dir / f".{engine_id}.incoming-{uuid.uuid4().hex[:8]}"
    backup = models_dir / f".{engine_id}.backup-{uuid.uuid4().hex[:8]}"
    shutil.move(str(source_dir), str(incoming))
    try:
        if target.exists():
            shutil.move(str(target), str(backup))
        replace_with_retry(incoming, target)
        record = _new_engine_record(
            engine_id,
            target,
            desired_record,
            installation_source=installation_source,
            zip_sha256=zip_sha256,
        )
        records = _existing_records(models_dir, desired)
        records[engine_id] = record
        _write_current_manifest(models_dir, records, desired)
    except Exception:
        if target.exists():
            source_dir.parent.mkdir(parents=True, exist_ok=True)
            if source_dir.exists():
                shutil.rmtree(source_dir, ignore_errors=True)
            shutil.move(str(target), str(source_dir))
        if backup.exists():
            shutil.move(str(backup), str(target))
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        raise
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def install_engine_from_staging(
    app_root: Path,
    engine_id: str,
    staging_engine_dir: Path,
    *,
    installation_source: str = "online_download",
) -> None:
    """Commit one downloaded engine under the shared installation lock."""
    from blc_portable.archive.locks import FileLock, get_engine_pack_lock_path

    desired = _desired_records()
    if engine_id not in desired:
        raise RuntimeError(f"Unknown engine ID: {engine_id}")
    with FileLock(get_engine_pack_lock_path(app_root)).acquire(timeout=120):
        _install_engine_directory(
            app_root,
            engine_id,
            staging_engine_dir,
            desired[engine_id],
            installation_source=installation_source,
            zip_sha256=None,
            desired=desired,
        )


def install_from_engine_pack(
    app_root: Path,
    pack_path: Path,
    expected_crc32: str,
    expected_sha256: str,
) -> dict[str, Any]:
    """Install only stale engines from a fully verified local pack."""
    actual_crc32 = compute_crc32(pack_path)
    if expected_crc32 and actual_crc32 != expected_crc32:
        raise RuntimeError(f"CRC32 mismatch: expected={expected_crc32} actual={actual_crc32}")
    actual_sha256 = compute_sha256(pack_path)
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise RuntimeError(f"SHA-256 mismatch: expected={expected_sha256[:16]} actual={actual_sha256[:16]}")
    if not expected_crc32 and not expected_sha256:
        print("  Engine Pack has no external digest; validating its complete internal manifest")
    print(f"  Engine Pack 校验通过: CRC32={actual_crc32} SHA256={actual_sha256[:16]}...")

    from blc_portable.archive.locks import FileLock, get_engine_pack_lock_path

    from .manifest import load_manifest_for_install
    from .verifier import verify_extracted_tree

    desired = _desired_records()
    staging_dir = app_root / f"models-staging-pack-{uuid.uuid4().hex[:12]}"
    installed_now: list[str] = []
    reused, _ = reusable_engine_ids(app_root / "models")
    with FileLock(get_engine_pack_lock_path(app_root)).acquire(timeout=120):
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            _safe_extract(pack_path, staging_dir)
            manifest_path = staging_dir / "engine-pack-manifest.json"
            if not manifest_path.is_file():
                raise RuntimeError("Engine Pack 缺少 engine-pack-manifest.json")
            manifest = load_manifest_for_install(manifest_path)
            verification_errors = verify_extracted_tree(staging_dir, manifest, strict_release=False)
            if verification_errors:
                raise RuntimeError("Engine Pack 校验失败:\n  " + "\n  ".join(verification_errors))
            pack_engines = {engine.engine_id: engine for engine in manifest.engines}
            if set(pack_engines) != set(desired):
                raise RuntimeError("Engine Pack engine set does not match the current catalog")
            for engine_id, desired_record in desired.items():
                pack_identity = manifest_engine_identity(pack_engines[engine_id])
                if engine_fingerprint(pack_identity) != desired_record["content_fingerprint"]:
                    raise RuntimeError(f"Engine Pack content fingerprint mismatch: {engine_id}")
            for engine_id, desired_record in desired.items():
                if engine_id in reused:
                    print(f"  reuse engine by content fingerprint: {engine_id}")
                    continue
                source_dir = staging_dir / str(pack_engines[engine_id].target_path)
                _install_engine_directory(
                    app_root,
                    engine_id,
                    source_dir,
                    desired_record,
                    installation_source="engine_pack",
                    zip_sha256=actual_sha256,
                    desired=desired,
                )
                installed_now.append(engine_id)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
    complete, errors = check_installed_models(app_root / "models", full_rehash=True)
    if not complete:
        raise RuntimeError("Post-install model verification failed: " + "; ".join(errors[:5]))
    return {
        "source": "engine_pack",
        "method": "content_addressed_extract",
        "network_requests": 0,
        "engines": sorted(desired),
        "installed_engines": installed_now,
        "reused_engines": sorted(reused),
        "model_set_fingerprint": model_set_fingerprint(desired),
    }
