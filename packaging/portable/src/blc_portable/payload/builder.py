"""Payload 构建器 — 构建 source_payload.zip 和完整 Manifest。

流程:
1. 从 731a31c 提取源码 → staging/
2. 应用受控版本 Overlay → 0.1.14.6-alpha
3. 生成 Manifest
4. 构建 ZIP
5. 校验
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from pathlib import Path

from .manifest import (
    RELEASE_VERSION,
    SOURCE_COMMIT_FULL,
    create_manifest,
    validate_manifest,
)
from .source_snapshot import (
    apply_version_overlay,
    extract_source,
    verify_source_origin,
)

_logger = logging.getLogger(__name__)

# 输出目录
PORTABLE_ROOT = Path(__file__).resolve().parent
BUILD_DIR = PORTABLE_ROOT / "build"
DIST_DIR = PORTABLE_ROOT / "dist"
PAYLOAD_DIR = DIST_DIR / "payload"


# Payload 白名单 — 允许进 ZIP 的内容
PAYLOAD_INCLUDE = [
    "app/",
    "config/",
    "pyproject.toml",
    "setup.py",
    "setup_c.py",
    ".env.example",
    "README.md",
    "CHANGELOG.md",
    "payload_manifest.json",
]

# Payload 排除 — 禁止进 ZIP
PAYLOAD_EXCLUDE = [
    ".git",
    ".github",
    "tests/",
    "docs/",
    "__pycache__/",
    "*.pyc",
    "storage/",
    ".venv/",
    "models/",
    "vendor/",
    "bin/",
    "*.log",
    "*.db",
    "*.sqlite3",
    "*.egg-info/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
]


def _should_include(rel_path: str) -> bool:
    """判断路径是否应进入 Payload。

    :param rel_path: 相对于 staging 根的路径 (POSIX)。
    :returns: True 表示包含。
    """
    # 排除模式
    for pattern in PAYLOAD_EXCLUDE:
        if pattern.endswith("/"):
            if rel_path == pattern.rstrip("/") or rel_path.startswith(pattern):
                return False
        elif pattern.startswith("*"):
            if rel_path.endswith(pattern[1:]):
                return False
        else:
            if rel_path == pattern or rel_path.startswith(pattern + "/") or rel_path.startswith(pattern):
                return False

    # 包含模式
    for pattern in PAYLOAD_INCLUDE:
        if pattern.endswith("/"):
            if rel_path == pattern.rstrip("/") or rel_path.startswith(pattern):
                return True
        else:
            if rel_path == pattern or rel_path.startswith(pattern):
                return True

    return False


def _safe_extract_zip(zip_path: Path, dest: Path) -> list[str]:
    """安全解压 ZIP，防止 Zip Slip、绝对路径、符号链接。

    :param zip_path: ZIP 文件路径。
    :param dest: 目标目录。
    :returns: 解压的文件列表。
    :raises RuntimeError: 检测到不安全路径时。
    """
    extracted: list[str] = []
    dest = dest.resolve()

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            # 安全检查
            if member.startswith("/"):
                raise RuntimeError(f"ZIP 包含绝对路径: {member}")
            if ".." in member.split("/"):
                raise RuntimeError(f"ZIP 包含路径遍历: {member}")
            if ":" in member:
                raise RuntimeError(f"ZIP 包含盘符: {member}")

            target = (dest / member).resolve()
            if not str(target).startswith(str(dest)):
                raise RuntimeError(f"ZIP 路径越界: {member} -> {target}")

            if member.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))
                extracted.append(member)

    return extracted


def build_payload(source_commit: str = "731a31c", builder_commit: str | None = None) -> dict:
    """构建完整 Payload: 提取 → Overlay → Manifest → ZIP → 校验。

    :param source_commit: 业务源码 Commit (默认 731a31c)。
    :param builder_commit: 构建工具 Commit (默认当前 HEAD)。
    :returns: 构建报告 dict。
    """
    import subprocess

    if builder_commit is None:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        builder_commit = result.stdout.strip()

    # 清理并创建输出目录
    staging_dir = BUILD_DIR / "payload_staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1: 提取源码
    _logger.info("Step 1: 提取 %s 的源码", source_commit[:8])
    extract_report = extract_source(source_commit, staging_dir)

    # Step 2: 应用版本 Overlay
    _logger.info("Step 2: 应用版本 Overlay → %s", RELEASE_VERSION)
    overlay_files = apply_version_overlay(staging_dir)

    # Step 3: 验证业务文件未被非受控修改
    _logger.info("Step 3: 验证源码来源")
    verify_source_origin(staging_dir, SOURCE_COMMIT_FULL)

    # Step 4: 构建 ZIP
    _logger.info("Step 4: 构建 source_payload.zip")
    zip_path = PAYLOAD_DIR / "source_payload.zip"
    included_files: list[str] = []

    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        # 先收集所有文件（稳定排序）
        all_files: list[Path] = []
        for p in sorted(staging_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(staging_dir).as_posix()
                if _should_include(rel):
                    all_files.append(p)

        for fp in all_files:
            rel = fp.relative_to(staging_dir).as_posix()
            # 使用固定时间戳确保可复现
            info = zipfile.ZipInfo(rel, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, fp.read_bytes())
            included_files.append(rel)

    _logger.info("ZIP: %s (%d bytes, %d files)", zip_path.name, zip_path.stat().st_size, len(included_files))

    # Step 5: 生成 Manifest
    _logger.info("Step 5: 生成 Manifest")
    manifest = create_manifest(
        payload_zip_path=zip_path,
        staging_dir=staging_dir,
        source_commit_full=SOURCE_COMMIT_FULL,
        builder_commit_full=builder_commit,
        release_overlays=overlay_files,
    )

    manifest_path = PAYLOAD_DIR / "payload_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Step 6: 自校验
    _logger.info("Step 6: 自校验")
    errors = validate_manifest(manifest, zip_path)
    if errors:
        for err in errors:
            _logger.error("自校验失败: %s", err)
        raise RuntimeError(f"Payload 自校验失败: {len(errors)} 个错误")

    # Step 7: 生成 SHA256SUMS
    _logger.info("Step 7: 生成 SHA256SUMS")
    from .manifest import compute_file_sha256

    sums_lines: list[str] = []
    for p in sorted(PAYLOAD_DIR.glob("*")):
        if p.is_file():
            sha = compute_file_sha256(p)
            sums_lines.append(f"{sha}  {p.name}")

    sums_path = PAYLOAD_DIR / "SHA256SUMS.txt"
    sums_path.write_text("\n".join(sums_lines) + "\n", encoding="utf-8")

    # Step 8: 可复现性验证 — 重新构建一次
    _logger.info("Step 8: 可复现性验证")
    verify_staging = BUILD_DIR / "payload_staging_verify"
    if verify_staging.exists():
        shutil.rmtree(verify_staging)
    verify_staging.mkdir(parents=True, exist_ok=True)

    verify_zip = BUILD_DIR / "verify_payload.zip"
    extract_source(source_commit, verify_staging)
    apply_version_overlay(verify_staging)

    with zipfile.ZipFile(str(verify_zip), "w", zipfile.ZIP_DEFLATED) as zf:
        v_files: list[Path] = []
        for p in sorted(verify_staging.rglob("*")):
            if p.is_file():
                rel = p.relative_to(verify_staging).as_posix()
                if _should_include(rel):
                    v_files.append(p)
        for fp in v_files:
            rel = fp.relative_to(verify_staging).as_posix()
            info = zipfile.ZipInfo(rel, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, fp.read_bytes())

    from .manifest import compute_payload_sha256

    hash1 = compute_payload_sha256(zip_path)
    hash2 = compute_payload_sha256(verify_zip)

    if hash1 != hash2:
        shutil.rmtree(verify_staging)
        verify_zip.unlink()
        raise RuntimeError(f"可复现性失败: hash1={hash1[:16]} hash2={hash2[:16]}")

    # 清理验证文件
    shutil.rmtree(verify_staging)
    verify_zip.unlink()

    # 报告
    report = {
        "release_version": RELEASE_VERSION,
        "source_commit_short": source_commit[:7],
        "source_commit_full": SOURCE_COMMIT_FULL,
        "builder_commit_full": builder_commit,
        "source_file_count": extract_report["file_count"],
        "payload_file_count": len(included_files),
        "release_overlay_files": overlay_files,
        "payload_sha256": hash1,
        "verified_reproducible": True,
        "zip_path": str(zip_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "output_dir": str(PAYLOAD_DIR.resolve()),
    }

    _logger.info("Payload 构建完成: %s", json.dumps(report, indent=2))
    return report


def _run_build_payload_main() -> None:
    """CLI 入口。"""
    import sys

    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    try:
        report = build_payload()
        print("\n构建成功:")
        print(f"  Payload: {report['zip_path']}")
        print(f"  Manifest: {report['manifest_path']}")
        print(f"  文件数: {report['payload_file_count']}")
        print(f"  SHA256: {report['payload_sha256'][:32]}")
    except Exception as exc:
        print(f"\n构建失败: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    _run_build_payload_main()
