"""Engine Pack 当前 schema 的测试数据工厂。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def current_manifest(files: dict[str, dict[str, object]] | None = None) -> dict[str, Any]:
    """构造字段完整、身份固定的当前内部 Manifest。"""
    from blc_portable.engine_pack.manifest import SOURCE_COMMIT_FULL, create_manifest

    manifest = create_manifest(
        source_commit=SOURCE_COMMIT_FULL,
        builder_commit="b" * 40,
        file_list=files or {},
        fixture=True,
    )
    return manifest.to_dict()


def current_tree_manifest(root: Path) -> dict[str, Any]:
    """根据临时目录中的四引擎文件生成当前内部 Manifest。"""
    files: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "engine-pack-manifest.json":
            rel = path.relative_to(root).as_posix()
            content = path.read_bytes()
            files[rel] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    return current_manifest(files)


def installed_manifest(
    models_dir: Path,
    *,
    zip_sha256: str | None = "a" * 64,
    installation_source: str = "engine_pack",
) -> dict[str, Any]:
    """根据临时 models 目录生成当前已安装模型清单。"""
    from blc_portable.engine_pack.identity import model_set_fingerprint
    from blc_portable.engine_pack.installer import (
        INSTALLED_MANIFEST_SCHEMA,
        _desired_records,
        compute_sha256,
    )

    engine_ids = ["whisper", "paraformer", "sensevoice", "funasr_nano"]
    files: dict[str, dict[str, object]] = {}
    desired = _desired_records()
    records: dict[str, dict[str, object]] = {}
    for engine_id in engine_ids:
        entries: dict[str, dict[str, object]] = {}
        engine_dir = models_dir / engine_id
        for path in sorted(engine_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(engine_dir).as_posix()
            size = path.stat().st_size
            entries[rel] = {"size": size, "sha256": compute_sha256(path)}
        files[engine_id] = {
            "target_path": f"models/{engine_id}",
            "file_count": len(entries),
            "total_size": sum(int(item["size"]) for item in entries.values()),
            "files": entries,
        }
        records[engine_id] = {
            "content_fingerprint": desired[engine_id]["content_fingerprint"],
            "identity": desired[engine_id]["identity"],
            "installation_source": installation_source,
            "zip_sha256": zip_sha256,
            "installed_at": "2026-08-12T00:00:00+00:00",
            **files[engine_id],
        }
    return {
        "schema_version": INSTALLED_MANIFEST_SCHEMA,
        "identity_schema_version": 1,
        "model_set_fingerprint": model_set_fingerprint(desired),
        "installed_at": "2026-08-12T00:00:00+00:00",
        "engines": records,
    }


def legacy_installed_manifest(models_dir: Path, *, version: str = "0.1.17.4-alpha") -> dict[str, Any]:
    """Build the exact schema-5 manifest accepted by the one-time migration."""
    from blc_portable.engine_pack.installer import compute_sha256

    engine_ids = ["whisper", "paraformer", "sensevoice", "funasr_nano"]
    files: dict[str, dict[str, object]] = {}
    for engine_id in engine_ids:
        entries: dict[str, dict[str, object]] = {}
        for path in sorted((models_dir / engine_id).rglob("*")):
            if path.is_file():
                entries[path.relative_to(models_dir / engine_id).as_posix()] = {
                    "size": path.stat().st_size,
                    "sha256": compute_sha256(path),
                }
        files[engine_id] = {
            "target_path": f"models/{engine_id}",
            "file_count": len(entries),
            "total_size": sum(int(item["size"]) for item in entries.values()),
            "files": entries,
        }
    return {
        "schema_version": 5,
        "engine_pack_version": version,
        "installation_source": "engine_pack",
        "zip_sha256": "a" * 64,
        "engine_ids": engine_ids,
        "file_count": sum(int(info["file_count"]) for info in files.values()),
        "total_size_bytes": sum(int(info["total_size"]) for info in files.values()),
        "installed_at": "2026-08-12T00:00:00",
        "source_commit": "97e39df3a9b24d35eca7ec6cb862291dadfad6e2",
        "files": files,
    }


def external_metadata(**overrides: object) -> dict[str, object]:
    """构造当前 Engine Pack 外部元数据。"""
    from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION, MANIFEST_FORMAT_VERSION, SOURCE_COMMIT_FULL

    result: dict[str, object] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "artifact_class": "production",
        "engine_pack_version": ENGINE_PACK_VERSION,
        "portable_release_version": ENGINE_PACK_VERSION,
        "engine_pack_api_version": MANIFEST_FORMAT_VERSION,
        "model_set_version": MANIFEST_FORMAT_VERSION,
        "filename": f"BiliLiveCut-EnginePack-{ENGINE_PACK_VERSION}.zip",
        "size_bytes": 600_000_000,
        "crc32": "ABCDEF01",
        "sha256": "a" * 64,
        "content_manifest_sha256": "b" * 64,
        "model_lock_sha256": "c" * 64,
        "source_commit": SOURCE_COMMIT_FULL,
        "builder_commit": "d" * 40,
        "build_timestamp": "2026-08-12T00:00:00+08:00",
        "expected_engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
    }
    result.update(overrides)
    return result
