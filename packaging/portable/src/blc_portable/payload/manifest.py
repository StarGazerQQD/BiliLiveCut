"""Payload Manifest 规范 — 定义 Portable Payload 的元数据格式。

格式版本 1，包含 release_version / source_commit / builder_commit / schema_version 等。
"""

from __future__ import annotations

import hashlib
import os
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE_COMMIT_SHORT = "731a31c"  # 与 packaging/portable/config/version.json 保持同步
SOURCE_COMMIT_FULL = "731a31cd04ae1df27dd6b6c5ffc535123932b825"
RELEASE_VERSION = "0.1.14.7-alpha"
MANIFEST_FORMAT_VERSION = 2


def _get_schema_version() -> int:
    """从项目配置获取当前 schema version。"""
    try:
        from app.db.schema import CURRENT_SCHEMA_VERSION

        return CURRENT_SCHEMA_VERSION
    except ImportError:
        return 1


def _get_python_version() -> str:
    """获取当前 Python 版本。"""
    return f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}"


def compute_sha256(data: bytes) -> str:
    """计算数据的 SHA-256。

    :param data: 字节数据。
    :returns: 十六进制哈希字符串。
    """
    return hashlib.sha256(data).hexdigest()


def compute_file_sha256(path: Path) -> str:
    """计算文件的 SHA-256。

    :param path: 文件路径。
    :returns: 十六进制哈希字符串。
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_payload_sha256(zip_path: Path) -> str:
    """计算 Payload ZIP 的 SHA-256。

    :param zip_path: ZIP 文件路径。
    :returns: 十六进制哈希字符串。
    """
    return compute_file_sha256(zip_path)


def compute_source_tree_sha256(staging_dir: Path, exclude: list[str] | None = None) -> str:
    """计算源码树的稳定 SHA-256（文件排序后拼接）。

    :param staging_dir: 源码 staging 目录。
    :param exclude: 排除的文件名列表。
    :returns: 十六进制哈希字符串。
    """
    exclude = exclude or []
    files: list[Path] = []
    for p in sorted(staging_dir.rglob("*")):
        if p.is_file() and p.name not in exclude and "__pycache__" not in p.parts:
            files.append(p)

    hasher = hashlib.sha256()
    for fp in files:
        rel = fp.relative_to(staging_dir).as_posix()
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(fp.stat().st_size).encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(fp.read_bytes())
        hasher.update(b"\x00")
    return hasher.hexdigest()


def create_manifest(
    payload_zip_path: Path,
    staging_dir: Path,
    source_commit_full: str,
    builder_commit_full: str,
    release_overlays: list[str],
) -> dict[str, Any]:
    """生成完整的 Payload Manifest。

    :param payload_zip_path: Payload ZIP 文件路径。
    :param staging_dir: 源码 staging 目录。
    :param source_commit_full: 业务源码基线完整 Commit Hash。
    :param builder_commit_full: 构建工具完整 Commit Hash。
    :param release_overlays: 发布元数据覆盖文件列表。
    :returns: Manifest 字典。
    """
    # 逐文件哈希
    file_hashes: dict[str, str] = {}
    for p in sorted(staging_dir.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            rel = p.relative_to(staging_dir).as_posix()
            file_hashes[rel] = compute_file_sha256(p)

    payload_sha256 = compute_payload_sha256(payload_zip_path)
    source_tree_sha256 = compute_source_tree_sha256(staging_dir)

    manifest: dict[str, Any] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "release_version": RELEASE_VERSION,
        "source_commit": source_commit_full,
        "source_commit_short": SOURCE_COMMIT_SHORT,
        "builder_commit": builder_commit_full,
        "schema_version": _get_schema_version(),
        "python_version": _get_python_version(),
        "architecture": f"{platform.machine().lower()}_{'win' if os.name == 'nt' else 'linux'}",
        "payload_sha256": payload_sha256,
        "source_tree_sha256": source_tree_sha256,
        "file_count": len(file_hashes),
        "release_overlays": release_overlays,
        "generated_at": datetime.now(UTC).isoformat(),
        "files": file_hashes,
    }
    return manifest


def validate_manifest(manifest: dict[str, Any], payload_zip_path: Path) -> list[str]:
    """验证 Manifest 完整性。

    :param manifest: Manifest 字典。
    :param payload_zip_path: Payload ZIP 路径。
    :returns: 错误信息列表。空列表表示通过。
    """
    errors: list[str] = []

    # 基本字段
    required = ["format_version", "release_version", "source_commit", "payload_sha256", "files"]
    for field in required:
        if field not in manifest:
            errors.append(f"Manifest 缺少必需字段: {field}")

    if manifest.get("release_version") != RELEASE_VERSION:
        errors.append(f"发布版本不匹配: manifest={manifest.get('release_version')} expected={RELEASE_VERSION}")

    if manifest.get("source_commit") != SOURCE_COMMIT_FULL:
        errors.append(
            f"Source Commit 不匹配: manifest={manifest.get('source_commit')[:8]} expected={SOURCE_COMMIT_FULL[:8]}"
        )

    if manifest.get("source_commit_short") != SOURCE_COMMIT_SHORT:
        errors.append(
            f"Source Commit Short 不匹配: {manifest.get('source_commit_short')} expected={SOURCE_COMMIT_SHORT}"
        )

    # Payload ZIP 哈希
    if payload_zip_path.exists():
        actual = compute_payload_sha256(payload_zip_path)
        if actual != manifest.get("payload_sha256"):
            errors.append(
                f"Payload ZIP 哈希不匹配: actual={actual[:16]} manifest={manifest.get('payload_sha256', '')[:16]}"
            )

    return errors
