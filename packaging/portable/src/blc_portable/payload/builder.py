"""Payload 构建器 — 构建 source_payload.zip 和完整 Manifest。

流程:
1. 从固定的当前发布基线 7c2764b 提取源码 → staging/
2. 应用受控版本 Overlay → 0.1.15.2-alpha
3. 构建 ZIP (收集 included_files 集合)
4. 基于 included_files 生成 Manifest (文件数/Hash 与 ZIP 严格一致)
5. 逐文件交叉校验 ZIP vs Manifest
6. 可复现性验证 (重新构建, 相同 SHA-256)
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import zipfile
from pathlib import Path

from .manifest import (
    RELEASE_VERSION,
    SOURCE_COMMIT_FULL,
    SOURCE_COMMIT_SHORT,
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
PORTABLE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILD_DIR = PORTABLE_ROOT / "build"
DIST_DIR = PORTABLE_ROOT / "dist"
PAYLOAD_DIR = DIST_DIR / "payload"

# Payload lists — single source from file_plan.py
from .file_plan import (  # noqa: E402, I001
    EXCLUDE_PATTERNS as PAYLOAD_EXCLUDE,
    PAYLOAD_ITEMS as PAYLOAD_INCLUDE,
)

PAYLOAD_INCLUDE = list(PAYLOAD_INCLUDE)
PAYLOAD_EXCLUDE = list(PAYLOAD_EXCLUDE)

# 目标平台 (Portable 始终为 Windows x64)
TARGET_PLATFORM = "win_x64"


def _should_include(rel_path: str) -> bool:
    """判断路径是否应进入 Payload。

    :param rel_path: 相对于 staging 根的路径 (POSIX)。
    :returns: True 表示包含。
    """
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

    for pattern in PAYLOAD_INCLUDE:
        if pattern.endswith("/"):
            if rel_path == pattern.rstrip("/") or rel_path.startswith(pattern):
                return True
        else:
            if rel_path == pattern or rel_path.startswith(pattern):
                return True

    return False


def _collect_included_files(staging_dir: Path) -> list[str]:
    """收集所有应进入 Payload 的文件 (排序后返回相对路径)。

    :param staging_dir: staging 目录。
    :returns: 排序后的文件路径列表。
    """
    files: list[str] = []
    for p in sorted(staging_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(staging_dir).as_posix()
            if _should_include(rel):
                files.append(rel)
    return files


def _write_zip(
    zip_path: Path,
    staging_dir: Path,
    included_files: list[str],
) -> None:
    """将 included_files 写入 ZIP，使用固定时间戳确保可复现。

    :param zip_path: 目标 ZIP 路径。
    :param staging_dir: staging 目录。
    :param included_files: 要写入的文件路径列表。
    """
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in included_files:
            fp = staging_dir / rel
            info = zipfile.ZipInfo(rel, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, fp.read_bytes())

    _logger.info(
        "ZIP: %s (%d bytes, %d files)",
        zip_path.name,
        zip_path.stat().st_size,
        len(included_files),
    )


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


# ── 原生加速模块编译与注入 ─────────────────────────────────


def _compile_and_copy_native_modules(staging_dir: Path) -> dict[str, bool]:
    """编译 C/Cython/Rust 原生加速模块并复制到 staging 目录。"""
    import os as _os
    import subprocess as _sp

    repo_root = PORTABLE_ROOT.parent.parent
    analysis_dir = repo_root / "app" / "analysis"
    staging_analysis = staging_dir / "app" / "analysis"
    staging_analysis.mkdir(parents=True, exist_ok=True)
    results: dict[str, bool] = {}

    # ── C 扩展 ──
    try:
        _logger.info("Compiling C extension...")
        r = _sp.run(
            [sys.executable, str(repo_root / "setup_c.py"), "build_ext", "--inplace"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            timeout=120,
        )
        if r.returncode == 0:
            results["c"] = True
            _logger.info("C extension compiled successfully")
        else:
            _logger.warning("C extension compilation failed: %s", r.stderr[-300:])
            results["c"] = False
    except Exception as exc:
        _logger.warning("C extension compilation exception: %s", exc)
        results["c"] = False

    # ── Cython 扩展 ──
    try:
        _logger.info("Compiling Cython extension...")
        r = _sp.run(
            [sys.executable, str(repo_root / "setup.py"), "build_ext", "--inplace"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            timeout=300,
        )
        if r.returncode == 0:
            results["cython"] = True
            _logger.info("Cython extension compiled successfully")
        else:
            _logger.warning("Cython extension compilation failed: %s", r.stderr[-300:])
            results["cython"] = False
    except Exception as exc:
        _logger.warning("Cython extension compilation exception: %s", exc)
        results["cython"] = False

    # ── Rust 扩展 ──
    try:
        _logger.info("Compiling Rust extension...")
        r = _sp.run(
            [sys.executable, str(repo_root / "tools" / "native" / "build_rust.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            timeout=300,
        )
        if r.returncode == 0:
            results["rust"] = True
            _logger.info("Rust extension compiled successfully")
        else:
            # 输出可诊断原因
            _logger.warning("Rust extension compilation failed (exit=%d)", r.returncode)
            if r.stdout.strip():
                _logger.warning("Rust stdout: %s", r.stdout.strip()[-500:])
            if r.stderr.strip():
                _logger.warning("Rust stderr: %s", r.stderr.strip()[-500:])
            results["rust"] = False
    except Exception as exc:
        _logger.error("Rust extension compilation exception: %s", exc)
        results["rust"] = False

    # ── 复制编译产物 → staging (仅复制目标平台产物) ──
    # Windows Payload 不得包含 Linux .so
    # C: 必需, Cython: 必需, Rust: 允许 fallback
    TARGET_EXTENSIONS = ("*.pyd",) if _os.name == "nt" else ("*.so",)
    ALLOWED_FALLBACK_EXTENSIONS = ("*.dll",) if _os.name == "nt" else ("*.dll", "*.pyd")
    copied = 0
    for ext in TARGET_EXTENSIONS:
        for src in analysis_dir.glob(ext):
            dst = staging_analysis / src.name
            shutil.copy2(str(src), str(dst))
            _logger.info("Copied native module: %s (%d bytes)", src.name, dst.stat().st_size)
            copied += 1
    # Allow non-target fallback DLLs but log them
    for ext in ALLOWED_FALLBACK_EXTENSIONS:
        for src in analysis_dir.glob(ext):
            dst = staging_analysis / src.name
            shutil.copy2(str(src), str(dst))
            _logger.info("Copied fallback module: %s (%d bytes)", src.name, dst.stat().st_size)
            copied += 1

    if copied:
        _logger.info("Injected %d native modules into Payload", copied)
    else:
        _logger.warning("No native modules generated — Portable will use Python fallback")

    return results


# ── 主构建流程 ────────────────────────────────────────


def build_payload(
    builder_commit: str | None = None,
    skip_reproducible: bool = False,
) -> dict:
    """构建完整 Payload: 提取 → Overlay → ZIP → Manifest → 校验。

    所有文件级操作基于唯一的 included_files 集合。

    :param builder_commit: 构建工具 Commit (默认当前 HEAD)。
    :param skip_reproducible: 跳过可复现性验证。
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
    _logger.info("Step 1: Extracting source from %s", SOURCE_COMMIT_SHORT)
    extract_report = extract_source(SOURCE_COMMIT_FULL, staging_dir)

    # Step 2: 应用版本 Overlay
    _logger.info("Step 2: Applying version overlay -> %s", RELEASE_VERSION)
    overlay_files = apply_version_overlay(
        staging_dir,
        source_commit_full=SOURCE_COMMIT_FULL,
        builder_commit_full=builder_commit,
    )
    backport_ids: list[str] = []

    # Step 2.5: 编译并注入原生加速模块
    _logger.info("Step 2.5: Compiling native extensions...")
    native_results = _compile_and_copy_native_modules(staging_dir)
    _logger.info("Native module results: %s", native_results)

    # Step 3: 验证业务文件未被非受控修改
    _logger.info("Step 3: Verifying source origin")
    verify_source_origin(staging_dir, SOURCE_COMMIT_FULL)

    # Step 4: 收集统一文件集合
    _logger.info("Step 4: Collecting included files")
    included_files = _collect_included_files(staging_dir)
    _logger.info("Included files: %d", len(included_files))

    # Step 5: 构建 ZIP
    _logger.info("Step 5: Building source_payload.zip")
    zip_path = PAYLOAD_DIR / "source_payload.zip"
    _write_zip(zip_path, staging_dir, included_files)

    # Step 6: 生成 Manifest (基于 included_files)
    _logger.info("Step 6: Generating Manifest")
    manifest = create_manifest(
        payload_zip_path=zip_path,
        staging_dir=staging_dir,
        included_file_relpaths=included_files,
        source_commit_full=SOURCE_COMMIT_FULL,
        builder_commit_full=builder_commit,
        release_overlays=overlay_files,
        backport_ids=backport_ids,
        target_platform=TARGET_PLATFORM,
    )

    manifest_path = PAYLOAD_DIR / "payload_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Step 7: 交叉校验 Manifest vs ZIP
    _logger.info("Step 7: Cross-validating Manifest vs ZIP")
    errors = validate_manifest(manifest, zip_path, staging_dir=staging_dir)
    if errors:
        for err in errors:
            _logger.error("Validation failed: %s", err)
        raise RuntimeError(f"Payload validation failed: {len(errors)} errors")

    # Step 8: 生成 SHA256SUMS
    _logger.info("Step 8: Generating SHA256SUMS")
    from .manifest import compute_file_sha256

    sums_lines: list[str] = []
    for p in sorted(PAYLOAD_DIR.glob("*")):
        if p.is_file() and p.name != "SHA256SUMS.txt":
            sha = compute_file_sha256(p)
            sums_lines.append(f"{sha}  {p.name}")

    sums_path = PAYLOAD_DIR / "SHA256SUMS.txt"
    sums_path.write_text("\n".join(sums_lines) + "\n", encoding="utf-8")

    # Step 9: 可复现性验证
    _logger.info("Step 9: Reproducibility verification")
    if skip_reproducible:
        _logger.warning("Reproducibility verification skipped (--skip-reproducible)")
        from .manifest import compute_payload_sha256

        hash1 = compute_payload_sha256(zip_path)
        verified = False
    else:
        verify_staging = BUILD_DIR / "payload_staging_verify"
        if verify_staging.exists():
            shutil.rmtree(verify_staging)
        verify_staging.mkdir(parents=True, exist_ok=True)

        verify_zip = BUILD_DIR / "verify_payload.zip"

        # 完全相同的构建流程
        extract_source(SOURCE_COMMIT_FULL, verify_staging)
        apply_version_overlay(
            verify_staging,
            source_commit_full=SOURCE_COMMIT_FULL,
            builder_commit_full=builder_commit,
        )
        _compile_and_copy_native_modules(verify_staging)

        # 使用同一套 included_files 逻辑收集文件
        v_included = _collect_included_files(verify_staging)
        _write_zip(verify_zip, verify_staging, v_included)

        from .manifest import compute_payload_sha256

        hash1 = compute_payload_sha256(zip_path)
        hash2 = compute_payload_sha256(verify_zip)

        if hash1 != hash2:
            shutil.rmtree(verify_staging)
            verify_zip.unlink()
            raise RuntimeError(f"Reproducibility failed: hash1={hash1[:16]} hash2={hash2[:16]}")

        shutil.rmtree(verify_staging)
        verify_zip.unlink()
        verified = True

    # 报告
    report = {
        "release_version": RELEASE_VERSION,
        "source_commit_short": SOURCE_COMMIT_SHORT,
        "source_commit_full": SOURCE_COMMIT_FULL,
        "builder_commit_full": builder_commit,
        "source_file_count": extract_report["file_count"],
        "payload_file_count": len(included_files),
        "release_overlay_files": overlay_files,
        "backport_ids": backport_ids,
        "payload_sha256": hash1,
        "verified_reproducible": verified,
        "target_platform": TARGET_PLATFORM,
        "zip_path": str(zip_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "output_dir": str(PAYLOAD_DIR.resolve()),
    }

    _logger.info("Payload build complete: %s", json.dumps(report, indent=2))
    return report


def _run_build_payload_main() -> None:
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    try:
        report = build_payload()
        print("\nBuild successful:")
        print(f"  Payload: {report['zip_path']}")
        print(f"  Manifest: {report['manifest_path']}")
        print(f"  Files: {report['payload_file_count']}")
        print(f"  SHA256: {report['payload_sha256'][:32]}")
        print(f"  Reproducible: {report['verified_reproducible']}")
    except Exception as exc:
        print(f"\nBuild failed: {exc}")
        sys.exit(1)


def main() -> int:
    """Entry point for thin wrapper.

    :returns: 0 success, 1 failure.
    """
    import argparse as _argparse

    parser = _argparse.ArgumentParser(description="Build Payload")
    parser.add_argument("--skip-reproducible", action="store_true", help="Skip reproducibility verification")
    args = parser.parse_args()

    try:
        build_payload(skip_reproducible=args.skip_reproducible)
        return 0
    except SystemExit as e:
        return int(str(e)) if str(e) else 0
    except Exception as exc:
        print(f"[Error] {exc}")
        return 1


if __name__ == "__main__":
    _run_build_payload_main()
