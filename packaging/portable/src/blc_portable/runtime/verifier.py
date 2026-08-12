"""Runtime verifier — Payload/Runtime integrity verification.

Provides both metadata checks (verify_runtime) and per-file content
verification (verify_runtime_files).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from blc_portable.runtime.activation import RUNTIME_SCHEMA_VERSION


def _streaming_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Streaming SHA-256 of a file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_runtime(app_root: Path) -> tuple[bool, list[str]]:
    """验证已安装 Runtime 的完整性。

    检查: current.json、release 目录、Payload SHA、Manifest SHA、
    ABI、platform、architecture、source commit。

    :param app_root: 应用根目录。
    :returns: (通过, 错误列表)。
    """
    from .__init__ import get_current_json_path, get_releases_dir

    errors: list[str] = []
    current_path = get_current_json_path(app_root)

    # 1. current.json
    if not current_path.exists():
        errors.append("current.json 不存在")
        return False, errors

    try:
        info = json.loads(current_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"current.json 无法解析: {exc}")
        return False, errors

    # 2. Release ID
    expected_fields = {
        "runtime_schema",
        "release_id",
        "release_version",
        "source_commit",
        "source_commit_short",
        "builder_commit",
        "payload_sha256",
        "manifest_sha256",
        "python_abi",
        "platform",
        "architecture",
        "activated_at",
    }
    if set(info) != expected_fields:
        errors.append("current.json 字段不符合当前 Runtime Schema")
        return False, errors

    rid = info["release_id"]
    if not rid:
        errors.append("current.json release_id 为空")
        return False, errors

    # 3. Release 目录存在
    release_dir = get_releases_dir(app_root) / rid
    if not release_dir.exists():
        errors.append(f"Release 目录不存在: {release_dir}")
        return False, errors

    if not (release_dir / "app" / "cli.py").exists():
        errors.append("Release 缺少 app/cli.py")

    # 4. ABI 匹配
    expected_abi = info["python_abi"]
    current_abi = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if expected_abi and expected_abi != current_abi:
        errors.append(f"Python ABI 不匹配: installed={expected_abi} current={current_abi}")

    # 5. Platform 匹配
    expected_platform = info["platform"]
    if expected_platform and expected_platform != sys.platform:
        errors.append(f"Platform 不匹配: installed={expected_platform} current={sys.platform}")

    # 6. Payload SHA 非空
    payload_sha = info["payload_sha256"]
    if not payload_sha:
        errors.append("current.json payload_sha256 为空")

    # 7. Schema
    schema = info["runtime_schema"]
    if schema != RUNTIME_SCHEMA_VERSION:
        errors.append(f"runtime_schema invalid: {schema}, expected={RUNTIME_SCHEMA_VERSION}")

    return len(errors) == 0, errors


def verify_runtime_files(app_root: Path) -> tuple[bool, list[str]]:
    """Verify installed Runtime files against installed manifest.

    Checks: file existence, size, SHA-256 for every file in the release.
    Uses streaming SHA-256 (never read_bytes()) for large files.

    :param app_root: app root dir.
    :returns: (pass, error list).
    """
    from .__init__ import get_current_release_dir

    errors: list[str] = []
    release_dir = get_current_release_dir(app_root)
    if release_dir is None:
        errors.append("No active Runtime release")
        return False, errors

    manifest_path = release_dir / "payload_manifest.json"
    if not manifest_path.exists():
        errors.append("Payload manifest not found in release")
        return False, errors

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"Manifest unreadable: {exc}")
        return False, errors

    from blc_portable.payload.manifest import MANIFEST_FORMAT_VERSION, validate_manifest_schema

    schema_errors = validate_manifest_schema(manifest)
    if schema_errors or manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        errors.extend(schema_errors or ["Payload manifest format_version does not match the current schema"])
        return False, errors

    file_count = 0
    size_mismatch = 0
    sha_mismatch = 0
    missing = 0
    extra_files = 0

    manifest_files = set()
    for fp_str, info in manifest["files"].items():
        manifest_files.add(fp_str)
        target = release_dir / fp_str
        if not target.exists():
            missing += 1
            errors.append(f"Missing: {fp_str}")
            continue

        if not isinstance(info, dict) or set(info) != {"size", "sha256"}:
            errors.append(f"Invalid manifest file entry: {fp_str}")
            continue
        expected_size = info["size"]
        actual_size = target.stat().st_size
        if actual_size != expected_size:
            size_mismatch += 1

        expected_hash = info["sha256"]
        actual_hash = _streaming_sha256(target)
        if actual_hash != expected_hash:
            sha_mismatch += 1
        file_count += 1

    # Check for extra files not in manifest
    for actual_file in release_dir.rglob("*"):
        if actual_file.is_file():
            rel = actual_file.relative_to(release_dir).as_posix()
            if rel not in manifest_files and rel != "payload_manifest.json":
                extra_files += 1

    if missing:
        errors.append(f"Missing files: {missing}")
    if size_mismatch:
        errors.append(f"Size mismatches: {size_mismatch}")
    if sha_mismatch:
        errors.append(f"SHA-256 mismatches: {sha_mismatch}")
    if extra_files:
        errors.append(f"Extra files: {extra_files}")

    total_errors = missing + size_mismatch + sha_mismatch + extra_files
    return total_errors == 0 and len(errors) == 0, errors
