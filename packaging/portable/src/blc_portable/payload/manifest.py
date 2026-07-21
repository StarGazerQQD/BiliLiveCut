"""Payload Manifest 规范 — 定义 Portable Payload 的元数据格式。

格式版本 5，新增身份拆分、目标平台、Engine Pack API 版本字段。
Manifest 只描述 ZIP 内实际存在的文件，不包含未入包的文件。

字段语义:
- portable_release_version: Portable 发布版本 (如 0.1.15-alpha)
- core_source_commit / core_source_commit_short: 固定业务源码基线 4bdaa13
- core_api_level: 业务源码的 schema version
- builder_commit: 构建工具 commit
- payload_schema: Manifest 格式版本 (本文件)
- applied_backports: 已应用的回移补丁 ID 列表
- engine_pack_api_version: Engine Pack 接口契约版本 (version.json engine_pack_schema)
- model_set_version: 模型锁版本 (version.json model_lock_schema)
- target_platform: 目标平台 (win_x64 / linux_x64)
- python_abi: 目标 Python ABI (cp311 / cp312)

兼容性:
- release_version 保留但语义为 portable_release_version
- source_commit / source_commit_short 保留但语义为 core_source_commit
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SOURCE_COMMIT_SHORT = "4bdaa13"
SOURCE_COMMIT_FULL = "4bdaa13b8b406ee8048885f123a0c969724a61ae"
RELEASE_VERSION = "0.1.15-alpha"
MANIFEST_FORMAT_VERSION = 5

# Cached version.json values
_VERSION_JSON: dict[str, Any] | None = None


def _load_version_json() -> dict[str, Any]:
    """Load version.json, cached.

    :returns: version.json dict.
    """
    global _VERSION_JSON
    if _VERSION_JSON is None:
        vp = Path(__file__).resolve().parent.parent.parent.parent / "config" / "version.json"
        _VERSION_JSON = json.loads(vp.read_text(encoding="utf-8"))
    return _VERSION_JSON


def _get_engine_pack_api_version() -> int:
    """Get engine_pack_schema from version.json."""
    return int(_load_version_json().get("engine_pack_schema", 4))


def _get_model_set_version() -> int:
    """Get model_lock_schema from version.json."""
    return int(_load_version_json().get("model_lock_schema", 4))


def _get_core_api_level() -> int:
    """Get runtime_schema (core API level) from version.json."""
    return int(_load_version_json().get("runtime_schema", 4))


def _get_python_abi() -> str:
    """Get Python ABI tag like cp311 or cp312."""
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def compute_sha256(data: bytes) -> str:
    """计算数据的 SHA-256。

    :param data: 字节数据。
    :returns: 十六进制哈希字符串。
    """
    return hashlib.sha256(data).hexdigest()


def compute_file_sha256(path: Path) -> str:
    """流式计算文件的 SHA-256。

    :param path: 文件路径。
    :returns: 十六进制哈希字符串。
    """
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_payload_sha256(zip_path: Path) -> str:
    """流式计算 Payload ZIP 的 SHA-256。

    :param zip_path: ZIP 文件路径。
    :returns: 十六进制哈希字符串。
    """
    return compute_file_sha256(zip_path)


def compute_source_tree_sha256(
    staging_dir: Path,
    included_relpaths: list[str],
) -> str:
    """计算源码树的稳定 SHA-256 — 仅对 ZIP 内实际入包的文件。

    按排序后的文件列表拼接: relpath + size + content。

    :param staging_dir: 源码 staging 目录。
    :param included_relpaths: ZIP 内实际包含的文件路径列表。
    :returns: 十六进制哈希字符串。
    """
    # 排序确保确定性
    sorted_paths = sorted(included_relpaths)

    hasher = hashlib.sha256()
    for rel in sorted_paths:
        fp = staging_dir / rel
        if not fp.is_file():
            raise RuntimeError(f"source_tree_sha256: file missing from staging: {rel}")
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(fp.stat().st_size).encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(fp.read_bytes())
        hasher.update(b"\x00")
    return hasher.hexdigest()


def build_file_manifest(
    staging_dir: Path,
    included_relpaths: list[str],
) -> dict[str, dict[str, Any]]:
    """为 ZIP 内每个文件构建哈希+大小记录。

    只处理 included_relpaths 中的文件，不扫描 staging_dir 全量。

    :param staging_dir: staging 目录。
    :param included_relpaths: ZIP 内的文件路径列表。
    :returns: {relpath: {sha256, size}} 字典。
    """
    files: dict[str, dict[str, Any]] = {}
    for rel in sorted(included_relpaths):
        fp = staging_dir / rel
        if not fp.is_file():
            raise RuntimeError(f"build_file_manifest: file not found in staging: {rel}")
        files[rel] = {
            "sha256": compute_file_sha256(fp),
            "size": fp.stat().st_size,
        }
    return files


def create_manifest(
    payload_zip_path: Path,
    staging_dir: Path,
    included_file_relpaths: list[str],
    source_commit_full: str,
    builder_commit_full: str,
    release_overlays: list[str],
    backport_ids: list[str] | None = None,
    target_platform: str = "win_x64",
) -> dict[str, Any]:
    """生成完整的 Payload Manifest。

    所有文件级哈希和计数都基于 included_file_relpaths，与 ZIP 内容严格一致。

    :param payload_zip_path: Payload ZIP 文件路径。
    :param staging_dir: 源码 staging 目录。
    :param included_file_relpaths: ZIP 内实际包含的文件路径列表。
    :param source_commit_full: 业务源码基线完整 Commit Hash。
    :param builder_commit_full: 构建工具完整 Commit Hash。
    :param release_overlays: 发布元数据覆盖文件列表。
    :param backport_ids: 已应用的 backport ID 列表。
    :param target_platform: 目标平台 (win_x64 / linux_x64)。
    :returns: Manifest 字典。
    """
    file_entries = build_file_manifest(staging_dir, included_file_relpaths)
    payload_sha256 = compute_payload_sha256(payload_zip_path)
    source_tree_sha256 = compute_source_tree_sha256(staging_dir, included_file_relpaths)

    bp = backport_ids or []

    manifest: dict[str, Any] = {
        # ── Schema ──
        "format_version": MANIFEST_FORMAT_VERSION,
        "payload_schema": MANIFEST_FORMAT_VERSION,
        # ── 身份 (新字段) ──
        "portable_release_version": RELEASE_VERSION,
        "core_source_commit": source_commit_full,
        "core_source_commit_short": SOURCE_COMMIT_SHORT,
        "core_api_level": _get_core_api_level(),
        "builder_commit": builder_commit_full,
        "applied_backports": bp,
        "engine_pack_api_version": _get_engine_pack_api_version(),
        "model_set_version": _get_model_set_version(),
        # ── 平台 ──
        "target_platform": target_platform,
        "python_abi": _get_python_abi(),
        # ── 兼容旧字段 ──
        "release_version": RELEASE_VERSION,
        "source_commit": source_commit_full,
        "source_commit_short": SOURCE_COMMIT_SHORT,
        "backport_ids": bp,
        "architecture": target_platform,
        "python_version": _get_python_abi(),
        "schema_version": _get_core_api_level(),
        # ── 校验 ──
        "payload_sha256": payload_sha256,
        "source_tree_sha256": source_tree_sha256,
        "file_count": len(included_file_relpaths),
        "release_overlays": release_overlays,
        "files": file_entries,
    }
    return manifest


# ── Manifest 校验 ─────────────────────────────────────


def validate_manifest(
    manifest: dict[str, Any],
    payload_zip_path: Path,
    staging_dir: Path | None = None,
) -> list[str]:
    """验证 Manifest 与 ZIP 内容的交叉一致性。

    逐项检查: 必需字段、ZIP hash、逐文件存在性/哈希/大小、文件数一致性、
    额外文件检测、路径安全检查。

    :param manifest: Manifest 字典。
    :param payload_zip_path: Payload ZIP 路径。
    :param staging_dir: staging 目录 (可选, 用于额外校验)。
    :returns: 错误信息列表。空列表表示通过。
    """
    import zipfile as _zipfile

    errors: list[str] = []

    # ── 1. 必需字段 ──
    required = [
        "format_version",
        "release_version",
        "source_commit",
        "payload_sha256",
        "files",
        "file_count",
    ]
    for field in required:
        if field not in manifest:
            errors.append(f"Manifest 缺少必需字段: {field}")

    # ── 2. 版本 / Commit 一致性 ──
    if manifest.get("release_version") != RELEASE_VERSION:
        errors.append(f"release_version 不匹配: manifest={manifest.get('release_version')} expected={RELEASE_VERSION}")

    if manifest.get("source_commit") != SOURCE_COMMIT_FULL:
        errors.append(
            f"source_commit 不匹配: manifest={manifest.get('source_commit', '')[:8]} expected={SOURCE_COMMIT_FULL[:8]}"
        )

    if manifest.get("source_commit_short") != SOURCE_COMMIT_SHORT:
        errors.append(
            f"source_commit_short 不匹配: {manifest.get('source_commit_short')} expected={SOURCE_COMMIT_SHORT}"
        )

    # ── 3. Payload ZIP 哈希 ──
    if not payload_zip_path.exists():
        errors.append(f"Payload ZIP 不存在: {payload_zip_path}")
        return errors

    actual_payload_sha = compute_payload_sha256(payload_zip_path)
    expected_payload_sha = manifest.get("payload_sha256", "")
    if actual_payload_sha != expected_payload_sha:
        errors.append(f"Payload SHA-256 不匹配: actual={actual_payload_sha[:16]} manifest={expected_payload_sha[:16]}")

    # ── 4. 逐文件交叉校验 ──
    manifest_files: dict[str, dict[str, Any]] = manifest.get("files", {})
    if not isinstance(manifest_files, dict) or not manifest_files:
        errors.append("Manifest 'files' 为空或格式错误")
        return errors

    # 读取 ZIP 内文件列表 — 处理无效 ZIP
    try:
        with _zipfile.ZipFile(payload_zip_path, "r") as zf:
            zip_names = set()
            zip_file_count = 0
            for name in zf.namelist():
                # 路径安全检查
                if name.startswith("/"):
                    errors.append(f"ZIP 含绝对路径: {name}")
                    continue
                if ".." in name.split("/"):
                    errors.append(f"ZIP 含路径遍历: {name}")
                    continue
                if ":" in name:
                    errors.append(f"ZIP 含盘符: {name}")
                    continue
                if not name.endswith("/"):
                    zip_names.add(name)
                    zip_file_count += 1
    except _zipfile.BadZipFile as exc:
        errors.append(f"Payload ZIP 无效 (not a valid ZIP): {exc}")
        return errors

    # 4a. file_count 一致
    expected_count = manifest.get("file_count", 0)
    if isinstance(expected_count, int) and expected_count != zip_file_count:
        errors.append(f"file_count 不一致: manifest={expected_count} zip={zip_file_count}")

    # 4b. Manifest 声明的文件数与实际一致
    if len(manifest_files) != zip_file_count:
        errors.append(f"Manifest files 条目数 ({len(manifest_files)}) != ZIP 文件数 ({zip_file_count})")

    # 4c. Manifest 声明的文件与 ZIP 文件集合完全一致
    manifest_file_set = set(manifest_files.keys())
    extra_in_manifest = manifest_file_set - zip_names
    extra_in_zip = zip_names - manifest_file_set

    if extra_in_manifest:
        errors.append(
            f"Manifest 声明了 {len(extra_in_manifest)} 个 ZIP 中不存在的文件: {sorted(extra_in_manifest)[:10]}"
        )
    if extra_in_zip:
        errors.append(f"ZIP 包含 {len(extra_in_zip)} 个 Manifest 未声明的文件: {sorted(extra_in_zip)[:10]}")

    # 4d. 逐文件哈希校验 (ZIP 中实际内容 vs Manifest 声明)
    sha_mismatches = 0
    size_mismatches = 0
    missing = 0
    max_errors = 5

    with _zipfile.ZipFile(payload_zip_path, "r") as zf:
        for rel_path in sorted(zip_names):
            file_info = manifest_files.get(rel_path)
            if file_info is None:
                missing += 1
                if missing <= max_errors:
                    errors.append(f"ZIP 文件未在 Manifest 中: {rel_path}")
                continue

            expected_sha = file_info.get("sha256", "")
            expected_size = file_info.get("size", 0)

            if expected_size:
                actual_size = zf.getinfo(rel_path).file_size
                if actual_size != expected_size:
                    size_mismatches += 1
                    if size_mismatches <= max_errors:
                        errors.append(f"Size 不一致: {rel_path} manifest={expected_size} zip={actual_size}")

            if expected_sha and len(expected_sha) == 64:
                actual_content = zf.read(rel_path)
                actual_sha = compute_sha256(actual_content)
                if actual_sha != expected_sha:
                    sha_mismatches += 1
                    if sha_mismatches <= max_errors:
                        errors.append(f"SHA-256 不一致: {rel_path} manifest={expected_sha[:16]} zip={actual_sha[:16]}")

    if missing:
        errors.append(f"ZIP 中 {missing} 个文件不在 Manifest 中")
    if sha_mismatches:
        errors.append(f"{sha_mismatches} 个文件 SHA-256 不一致")
    if size_mismatches:
        errors.append(f"{size_mismatches} 个文件 size 不一致")

    return errors


def cross_verify_installed(
    installed_dir: Path,
    manifest: dict[str, Any],
) -> list[str]:
    """验证已安装的 Runtime 目录与 Manifest 完全一致。

    检查: 缺失文件、额外文件、hash 不一致、文件数量、路径遍历。

    :param installed_dir: 已安装的 release 目录。
    :param manifest: 已解析的 Manifest 字典。
    :returns: 错误列表。空列表表示通过。
    """
    errors: list[str] = []
    installed_dir = installed_dir.resolve()

    manifest_files: dict[str, dict[str, Any]] = manifest.get("files", {})
    expected_count = manifest.get("file_count", len(manifest_files))

    if not manifest_files:
        errors.append("Manifest 'files' 为空")
        return errors

    missing_files = 0
    hash_mismatches = 0
    size_mismatches = 0
    max_detail = 5
    actual_files: set[str] = set()

    # 检查 Manifest 中的每个文件
    for rel_path, file_info in manifest_files.items():
        target = installed_dir / rel_path

        # 路径安全检查
        resolved = target.resolve()
        if not str(resolved).startswith(str(installed_dir)):
            errors.append(f"路径越界: {rel_path} -> {resolved}")
            continue

        if not target.is_file():
            missing_files += 1
            if missing_files <= max_detail:
                errors.append(f"缺失文件: {rel_path}")
            continue

        actual_files.add(rel_path)

        expected_sha = file_info.get("sha256", "")
        expected_size = file_info.get("size", 0)

        if expected_size:
            actual_size = target.stat().st_size
            if actual_size != expected_size:
                size_mismatches += 1
                if size_mismatches <= max_detail:
                    errors.append(f"Size 不一致: {rel_path} manifest={expected_size} actual={actual_size}")

        if expected_sha and len(expected_sha) == 64:
            actual_sha = compute_file_sha256(target)
            if actual_sha != expected_sha:
                hash_mismatches += 1
                if hash_mismatches <= max_detail:
                    errors.append(f"SHA-256 不一致: {rel_path} manifest={expected_sha[:16]} actual={actual_sha[:16]}")

    # 检查额外文件
    extra_files = 0
    for p in installed_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(installed_dir).as_posix()
            if rel not in manifest_files and rel != "payload_manifest.json":
                extra_files += 1
                if extra_files <= max_detail:
                    errors.append(f"额外文件 (不在 Manifest 中): {rel}")

    # 汇总
    if missing_files:
        errors.append(f"缺失文件总数: {missing_files}")
    if hash_mismatches:
        errors.append(f"哈希不一致总数: {hash_mismatches}")
    if size_mismatches:
        errors.append(f"大小不一致总数: {size_mismatches}")
    if extra_files:
        errors.append(f"额外文件总数: {extra_files}")

    # 文件数量检查
    expected_count_val = expected_count
    actual_count = len(actual_files)
    if expected_count_val and actual_count != expected_count_val:
        errors.append(f"文件数量不一致: manifest={expected_count_val} actual={actual_count}")

    return errors
