"""Engine Pack 验证模块 — Builder 和 Installer 共用的只读验证核心。

提供:
- verify_archive_metadata: 验证 ZIP 和相关输出文件的外部元数据
- verify_archive_manifest: 验证内部 Manifest 的结构和内容
- verify_extracted_tree: 验证解压后的目录树与 Manifest 完全一致
- verify_engine_pack_complete: 组合以上三项的完整验证入口
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def compute_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """流式计算文件 SHA-256。

    :param path: 文件路径。
    :param chunk_size: 读取块大小。
    :returns: SHA-256 十六进制字符串。
    """
    import hashlib

    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_archive_metadata(
    archive_path: Path,
    expected_crc32: str,
    expected_sha256: str,
) -> list[str]:
    """验证 Engine Pack ZIP 的外部元数据。

    校验: ZIP 文件存在、size > 0、CRC32 匹配、SHA-256 匹配。

    :param archive_path: ZIP 文件路径。
    :param expected_crc32: 期望 CRC32 (8 位大写十六进制)。
    :param expected_sha256: 期望 SHA-256 (64 位十六进制)。
    :returns: 错误列表，空列表表示通过。
    """
    import zlib

    errors: list[str] = []

    if not archive_path.exists():
        errors.append(f"ZIP 文件不存在: {archive_path}")
        return errors

    size = archive_path.stat().st_size
    if size == 0:
        errors.append("ZIP 文件大小为 0")

    # CRC32
    if expected_crc32:
        crc_val = 0
        with archive_path.open("rb") as f:
            while chunk := f.read(8 * 1024 * 1024):
                crc_val = zlib.crc32(chunk, crc_val)
        actual_crc32 = f"{crc_val & 0xFFFFFFFF:08X}"
        if actual_crc32 != expected_crc32:
            errors.append(f"CRC32 不匹配: expected={expected_crc32} actual={actual_crc32}")
    else:
        errors.append("expected_crc32 为空")

    # SHA-256
    if expected_sha256 and len(expected_sha256) == 64:
        actual_sha256 = compute_sha256(archive_path)
        if actual_sha256 != expected_sha256:
            errors.append(f"SHA-256 不匹配: expected={expected_sha256[:16]}... actual={actual_sha256[:16]}...")
    elif not expected_sha256:
        errors.append("expected_sha256 为空")
    else:
        errors.append(f"SHA-256 格式无效: 长度 {len(expected_sha256)} (期望 64)")

    return errors


def verify_archive_manifest(
    manifest_path: Path,
    expected_version: str | None = None,
    expected_engine_ids: set[str] | None = None,
) -> list[str]:
    """验证 Engine Pack 内部 Manifest 的结构和内容。

    校验: Manifest 存在、schema/version 有效、engine IDs 完整、file list 非空。

    :param manifest_path: Manifest 文件路径。
    :param expected_version: 期望版本 (None 跳过版本检查)。
    :param expected_engine_ids: 期望引擎 ID 集合 (None 使用默认四引擎)。
    :returns: 错误列表，空列表表示通过。
    """
    errors: list[str] = []

    if not manifest_path.exists():
        errors.append(f"Manifest 文件不存在: {manifest_path}")
        return errors

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        errors.append(f"Manifest 无法解析: {exc}")
        return errors

    # Schema version
    schema = manifest.get("schema_version", manifest.get("format_version"))
    if not isinstance(schema, int) or schema < 1:
        errors.append(f"Manifest schema_version 无效: {schema}")

    # Version
    ep_version = manifest.get("engine_pack_version", "")
    if not ep_version:
        errors.append("Manifest engine_pack_version 为空")
    elif expected_version and ep_version != expected_version:
        errors.append(f"Engine Pack 版本不匹配: {ep_version} != {expected_version}")

    # Engine IDs
    engine_ids = {e.get("engine_id", "") for e in manifest.get("engines", [])}
    default_engines = {"whisper", "paraformer", "sensevoice", "funasr_nano"}
    check_ids = expected_engine_ids if expected_engine_ids is not None else default_engines
    if engine_ids != check_ids:
        missing = check_ids - engine_ids
        extra = engine_ids - check_ids
        if missing:
            errors.append(f"Manifest 缺少引擎: {missing}")
        if extra:
            errors.append(f"Manifest 包含额外引擎: {extra}")

    # File list
    files = manifest.get("files", {})
    total = manifest.get("total_files", 0)
    if total == 0:
        errors.append("Manifest total_files 为 0")
    if len(files) != total:
        errors.append(f"Manifest files 数量不一致: declared={total} actual={len(files)}")

    for engine in manifest.get("engines", []):
        tp = engine.get("target_path", "")
        if not tp:
            errors.append(f"引擎 {engine.get('engine_id', '?')} target_path 为空")

    return errors


def verify_extracted_tree(
    extracted_dir: Path,
    manifest: dict[str, Any],
) -> list[str]:
    """验证解压后的目录树与 Manifest 完全一致。

    逐文件比对: 存在性、size、SHA-256。
    检测: 缺失文件、多余文件。

    :param extracted_dir: 解压根目录。
    :param manifest: 已解析的 Manifest 字典。
    :returns: 错误列表，空列表表示通过。
    """
    errors: list[str] = []

    # 1. 引擎目录存在性
    for engine in manifest.get("engines", []):
        tp = engine.get("target_path", "")
        if not tp:
            continue
        ep = extracted_dir / tp
        if not ep.exists():
            errors.append(f"缺少引擎目录: {tp}")
        elif not any(ep.iterdir()):
            errors.append(f"引擎目录为空: {tp}")

    # 2. 逐文件检查
    files = manifest.get("files", {})
    missing_files = []
    size_mismatch = []
    sha_mismatch = []

    for rel_path, info in files.items():
        target = extracted_dir / rel_path
        if not target.exists():
            missing_files.append(rel_path)
            continue

        expected_size = int(info.get("size", 0))
        actual_size = target.stat().st_size
        if expected_size and actual_size != expected_size:
            size_mismatch.append(f"{rel_path}: expected={expected_size} actual={actual_size}")

        expected_hash = str(info.get("sha256", ""))
        if expected_hash and len(expected_hash) == 64:
            actual_hash = compute_sha256(target)
            if actual_hash != expected_hash:
                sha_mismatch.append(f"{rel_path}: expected={expected_hash[:16]}... actual={actual_hash[:16]}...")

    if missing_files:
        errors.append(f"缺失文件 ({len(missing_files)}): {missing_files[:5]}{'...' if len(missing_files) > 5 else ''}")
    if size_mismatch:
        suffix = "..."
        errors.append(
            f"文件大小不匹配 ({len(size_mismatch)}): {size_mismatch[:3]}{suffix if len(size_mismatch) > 3 else ''}"
        )
    if sha_mismatch:
        errors.append(
            f"文件 SHA-256 不匹配 ({len(sha_mismatch)}): {sha_mismatch[:3]}{suffix if len(sha_mismatch) > 3 else ''}"
        )

    # 3. 多余文件检测
    manifest_paths = set(files.keys())
    manifest_paths.add("engine-pack-manifest.json")
    for p in extracted_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(extracted_dir).as_posix()
            if rel not in manifest_paths:
                errors.append(f"多余文件 (Manifest 未声明): {rel}")

    return errors


def verify_engine_pack_complete(
    archive_path: Path,
    manifest_path: Path,
    expected_crc32: str,
    expected_sha256: str,
    expected_version: str,
    expected_engine_ids: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """完整验证 Engine Pack — 组合 metadata + manifest + extracted tree。

    先验证 Manifest 结构，然后验证 ZIP 哈希，最后验证解压后目录树。

    :param archive_path: ZIP 文件路径。
    :param manifest_path: 内部 Manifest 路径 (解压后)。
    :param expected_crc32: 期望 CRC32。
    :param expected_sha256: 期望 SHA-256。
    :param expected_version: 期望版本。
    :param expected_engine_ids: 期望引擎 ID 集合。
    :returns: (通过, 错误列表)。
    """
    all_errors: list[str] = []

    # 1. Manifest 结构验证
    manifest_errors = verify_archive_manifest(manifest_path, expected_version, expected_engine_ids)
    all_errors.extend(manifest_errors)

    if manifest_errors:
        return False, all_errors

    # 2. ZIP 哈希验证
    metadata_errors = verify_archive_metadata(archive_path, expected_crc32, expected_sha256)
    all_errors.extend(metadata_errors)

    # 3. 如果 Manifest 解析成功，验证解压树
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tree_errors = verify_extracted_tree(manifest_path.parent, manifest)
        all_errors.extend(tree_errors)
    except (json.JSONDecodeError, OSError):
        pass

    return len(all_errors) == 0, all_errors
