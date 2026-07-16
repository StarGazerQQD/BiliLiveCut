"""Runtime 安装器 — 从 Payload ZIP 原子安装源码到 releases/ 目录。

使用内容寻址 Release ID + 安装锁 + staging + atomic rename + rollback。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any


def compute_payload_hash(zip_path: Path) -> str:
    """流式计算 Payload ZIP 的 SHA-256。

    :param zip_path: ZIP 路径。
    :returns: SHA-256 十六进制。
    """
    hasher = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def build_release_id(version: str, commit_short: str, payload_hash: str) -> str:
    """构建内容寻址 Release ID。

    :param version: 版本号。
    :param commit_short: 短 Commit hash。
    :param payload_hash: Payload SHA-256 的前 12 位。
    :returns: Release ID 字符串。
    """
    return f"{version}+{commit_short}+{payload_hash[:12]}"


def install_from_payload(
    app_root: Path,
    zip_path: Path,
    manifest: dict[str, Any],
    expected_hash: str,
    expected_version: str,
    expected_commit: str,
) -> Path:
    """从 Payload ZIP 原子安装源码到 releases 目录。

    :param app_root: 应用根目录。
    :param zip_path: Payload ZIP 路径。
    :param manifest: Payload manifest 字典。
    :param expected_hash: 期望 Payload SHA-256。
    :param expected_version: 期望版本。
    :param expected_commit: 期望短 Commit。
    :returns: 已安装的 Release 目录。
    :raises RuntimeError: 校验或安装失败时。
    """
    # 校验 Payload SHA-256
    actual_hash = compute_payload_hash(zip_path)
    if actual_hash != expected_hash:
        raise RuntimeError(f"Payload hash mismatch: actual={actual_hash[:16]} expected={expected_hash[:16]}")

    if manifest.get("release_version") != expected_version:
        raise RuntimeError(f"Payload version mismatch: {manifest.get('release_version')} != {expected_version}")
    if manifest.get("source_commit_short") != expected_commit:
        raise RuntimeError(f"Source commit mismatch: {manifest.get('source_commit_short')} != {expected_commit}")

    print(f"  Payload: v{expected_version} | Source: {expected_commit} | SHA256: {actual_hash[:16]}")

    # 内容寻址 Release ID
    content_release_id = build_release_id(expected_version, expected_commit, actual_hash)

    from .__init__ import get_releases_dir, get_runtime_dir

    releases_dir = get_releases_dir()
    staging = get_runtime_dir() / "staging"
    release_dir = releases_dir / content_release_id

    if staging.exists():
        shutil.rmtree(staging)

    from blc_portable.archive.locks import FileLock, get_runtime_lock_path
    from blc_portable.archive.safe_zip import safe_extract

    lock = FileLock(get_runtime_lock_path(app_root))

    with lock.acquire(timeout=120):
        try:
            staging.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(zip_path) as zf:
                safe_extract(zf, staging)

            for path in ["app/cli.py", "pyproject.toml"]:
                if not (staging / path).exists():
                    raise RuntimeError(f"Release missing key file: {path}")

            releases_dir.mkdir(parents=True, exist_ok=True)
            if release_dir.exists():
                shutil.rmtree(release_dir)
            os.replace(str(staging), str(release_dir))

            from .verifier import write_current_json

            write_current_json(
                app_root,
                release_id=content_release_id,
                release_version=expected_version,
                source_commit=manifest.get("source_commit", ""),
                source_commit_short=expected_commit,
                builder_commit=manifest.get("builder_commit", ""),
                payload_sha256=actual_hash,
                manifest_sha256=manifest.get("payload_sha256", ""),
            )

        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    print(f"  Release installed: {release_dir}")
    return release_dir
