"""Payload 当前 schema 的测试数据工厂。"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Any


def manifest_for_zip(payload_zip: Path) -> dict[str, Any]:
    """根据一个现有 Payload ZIP 生成严格的当前 Manifest。"""
    from blc_portable.payload.manifest import (
        MANIFEST_FORMAT_VERSION,
        RELEASE_VERSION,
        SOURCE_COMMIT_FULL,
        SOURCE_COMMIT_SHORT,
        _get_core_api_level,
        _get_engine_pack_api_version,
        _get_model_set_version,
        _get_python_abi,
        compute_payload_sha256,
    )
    from blc_portable.project_license import PROJECT_LICENSE_ID

    files: dict[str, dict[str, object]] = {}
    with zipfile.ZipFile(payload_zip) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            content = archive.read(info.filename)
            files[info.filename] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
    license_entry = files.get("LICENSE")
    if license_entry is None:
        raise ValueError("测试 Payload 缺少 LICENSE")
    source_tree = hashlib.sha256()
    for rel_path, info in sorted(files.items()):
        with zipfile.ZipFile(payload_zip) as archive:
            content = archive.read(rel_path)
        source_tree.update(rel_path.encode("utf-8"))
        source_tree.update(b"\x00")
        source_tree.update(str(info["size"]).encode("utf-8"))
        source_tree.update(b"\x00")
        source_tree.update(content)
        source_tree.update(b"\x00")
    return {
        "format_version": MANIFEST_FORMAT_VERSION,
        "portable_release_version": RELEASE_VERSION,
        "core_source_commit": SOURCE_COMMIT_FULL,
        "core_source_commit_short": SOURCE_COMMIT_SHORT,
        "core_api_level": _get_core_api_level(),
        "builder_commit": SOURCE_COMMIT_FULL,
        "engine_pack_api_version": _get_engine_pack_api_version(),
        "model_set_version": _get_model_set_version(),
        "target_platform": "win_x64",
        "python_abi": _get_python_abi(),
        "payload_sha256": compute_payload_sha256(payload_zip),
        "files": files,
        "file_count": len(files),
        "project_license": PROJECT_LICENSE_ID,
        "project_license_sha256": str(license_entry["sha256"]),
        "source_tree_sha256": source_tree.hexdigest(),
    }
