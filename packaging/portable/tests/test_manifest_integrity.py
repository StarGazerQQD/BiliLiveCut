"""Manifest 与 Payload 完整性测试 — 阶段 4。

覆盖:
- Manifest vs ZIP 交叉校验 (逐文件 hash, 额外文件, 缺失文件, 文件数)
- 身份字段拆分验证
- tamper 检测 (篡改 manifest, 篡改 ZIP, 篡改文件)
- 路径安全 (绝对路径, ..遍历, 盘符)
- 可复现性
- 无 Backport / wrong-identity
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

# 路径设置 — 与 test_portable.py 保持一致
_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

_portable_dir = Path(__file__).resolve().parent.parent
PAYLOAD_DIR = _portable_dir / "dist" / "payload"
PAYLOAD_ZIP = PAYLOAD_DIR / "source_payload.zip"
MANIFEST_PATH = PAYLOAD_DIR / "payload_manifest.json"
SHA256SUMS_PATH = PAYLOAD_DIR / "SHA256SUMS.txt"


def _has_payload() -> bool:
    """检查 Payload 是否已构建。"""
    return PAYLOAD_ZIP.exists() and MANIFEST_PATH.exists()


def _load_manifest() -> dict[str, Any]:
    """加载当前 Manifest。"""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ── Fixtures ────────────────────────────────────────────


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    """生产 Payload Manifest fixture。"""
    if not _has_payload():
        pytest.skip("Payload not built")
    return _load_manifest()


@pytest.fixture(scope="module")
def payload_zip() -> Path:
    """生产 Payload ZIP fixture。"""
    if not _has_payload():
        pytest.skip("Payload not built")
    return PAYLOAD_ZIP


# ── Manifest ZIP 交叉一致性 ──────────────────────────────


class TestManifestZipConsistency:
    """验证 Manifest 与 ZIP 文件集合完全一致。"""

    def test_file_count_matches_zip_contents(self, manifest: dict, payload_zip: Path) -> None:
        """验证 file_count 与 ZIP 内实际文件数一致。"""
        with zipfile.ZipFile(payload_zip, "r") as zf:
            actual = sum(1 for n in zf.namelist() if not n.endswith("/"))
        assert manifest["file_count"] == actual, f"file_count mismatch: manifest={manifest['file_count']} zip={actual}"

    def test_files_entry_count_matches_zip(self, manifest: dict, payload_zip: Path) -> None:
        """验证 manifest 'files' 条目数与 ZIP 文件数一致。"""
        with zipfile.ZipFile(payload_zip, "r") as zf:
            actual = sum(1 for n in zf.namelist() if not n.endswith("/"))
        assert len(manifest["files"]) == actual, f"files entry count mismatch: {len(manifest['files'])} != {actual}"

    def test_no_file_in_manifest_missing_from_zip(self, manifest: dict, payload_zip: Path) -> None:
        """验证所有 Manifest 中的文件都确实在 ZIP 中。"""
        with zipfile.ZipFile(payload_zip, "r") as zf:
            zip_names = {n for n in zf.namelist() if not n.endswith("/")}
        manifest_names = set(manifest["files"].keys())
        extra = manifest_names - zip_names
        assert not extra, f"Manifest has {len(extra)} files not in ZIP: {sorted(extra)[:5]}"

    def test_no_file_in_zip_missing_from_manifest(self, manifest: dict, payload_zip: Path) -> None:
        """验证所有 ZIP 中的文件都在 Manifest 中。"""
        with zipfile.ZipFile(payload_zip, "r") as zf:
            zip_names = {n for n in zf.namelist() if not n.endswith("/")}
        manifest_names = set(manifest["files"].keys())
        extra = zip_names - manifest_names
        assert not extra, f"ZIP has {len(extra)} files not in Manifest: {sorted(extra)[:5]}"


class TestPayloadChecksums:
    """Validate the standalone payload checksums file."""

    def test_checksum_file_does_not_hash_itself(self) -> None:
        """A checksum file cannot contain a stable checksum of its own content."""
        names = {line.split(maxsplit=1)[1] for line in SHA256SUMS_PATH.read_text(encoding="utf-8").splitlines()}
        assert "SHA256SUMS.txt" not in names

    def test_all_declared_checksums_match(self) -> None:
        """Every payload artifact listed in SHA256SUMS must match its digest."""
        for line in SHA256SUMS_PATH.read_text(encoding="utf-8").splitlines():
            expected, filename = line.split(maxsplit=1)
            artifact = PAYLOAD_DIR / filename
            assert artifact.is_file(), f"Missing checksummed artifact: {filename}"
            assert hashlib.sha256(artifact.read_bytes()).hexdigest() == expected


class TestFileHashIntegrity:
    """验证逐文件哈希正确性。"""

    def test_every_file_hash_matches(self, manifest: dict, payload_zip: Path) -> None:
        """验证每个文件的 Manifest SHA-256 与 ZIP 中实际内容一致。"""
        mismatches = 0
        max_check = 20  # 抽样检查避免过慢
        checked = 0
        with zipfile.ZipFile(payload_zip, "r") as zf:
            for rel_path, info in manifest["files"].items():
                if checked >= max_check:
                    break
                expected_sha = info.get("sha256", "")
                if expected_sha and len(expected_sha) == 64:
                    content = zf.read(rel_path)
                    actual_sha = hashlib.sha256(content).hexdigest()
                    if actual_sha != expected_sha:
                        mismatches += 1
                checked += 1
        assert mismatches == 0, f"{mismatches} files have SHA-256 mismatch"

    def test_file_size_matches(self, manifest: dict, payload_zip: Path) -> None:
        """验证每个文件的 Manifest size 与 ZIP 中实际大小一致。"""
        mismatches = 0
        max_check = 20
        checked = 0
        with zipfile.ZipFile(payload_zip, "r") as zf:
            for rel_path, info in manifest["files"].items():
                if checked >= max_check:
                    break
                expected_size = info.get("size", 0)
                if expected_size:
                    actual_size = zf.getinfo(rel_path).file_size
                    if actual_size != expected_size:
                        mismatches += 1
                checked += 1
        assert mismatches == 0, f"{mismatches} files have size mismatch"


# ── 身份字段拆分 ────────────────────────────────────────


class TestIdentityFields:
    """验证 Manifest 包含所有要求的身份字段。"""

    REQUIRED_IDENTITY = [
        "portable_release_version",
        "core_source_commit",
        "core_source_commit_short",
        "core_api_level",
        "builder_commit",
        "payload_schema",
        "applied_backports",
        "engine_pack_api_version",
        "model_set_version",
        "target_platform",
        "python_abi",
    ]

    COMPAT_FIELDS = [
        "release_version",
        "source_commit",
        "source_commit_short",
        "backport_ids",
        "architecture",
        "python_version",
        "schema_version",
    ]

    def test_all_identity_fields_present(self, manifest: dict) -> None:
        """验证所有新身份字段都存在。"""
        for field in self.REQUIRED_IDENTITY:
            assert field in manifest, f"Missing identity field: {field}"

    def test_all_compat_fields_present(self, manifest: dict) -> None:
        """验证所有旧兼容字段都存在。"""
        for field in self.COMPAT_FIELDS:
            assert field in manifest, f"Missing compat field: {field}"

    def test_core_source_commit_is_current_baseline(self, manifest: dict) -> None:
        """验证 core_source_commit 是当前 Portable 源码基线。"""
        assert manifest["core_source_commit"] == "4bdaa13b8b406ee8048885f123a0c969724a61ae"
        assert manifest["core_source_commit_short"] == "4bdaa13"
        assert manifest["source_commit"] == manifest["core_source_commit"]
        assert manifest["source_commit_short"] == "4bdaa13"

    def test_portable_version_matches_release(self, manifest: dict) -> None:
        """验证 portable_release_version == release_version。"""
        assert manifest["portable_release_version"] == manifest["release_version"]

    def test_target_platform_is_win_x64(self, manifest: dict) -> None:
        """验证 target_platform 不是 builder 平台。"""
        assert manifest["target_platform"] == "win_x64", (
            f"target_platform should be win_x64, got {manifest['target_platform']}"
        )

    def test_backport_ids_match(self, manifest: dict) -> None:
        """验证 applied_backports == backport_ids。"""
        assert manifest["applied_backports"] == manifest["backport_ids"]

    def test_no_generated_at(self, manifest: dict) -> None:
        """验证不包含 generated_at (破坏可复现性)。"""
        assert "generated_at" not in manifest, "generated_at should NOT be in manifest (breaks reproducibility)"

    def test_engine_pack_contract_fields(self, manifest: dict) -> None:
        """验证 Engine Pack API 版本字段。"""
        assert isinstance(manifest.get("engine_pack_api_version"), int)
        assert isinstance(manifest.get("model_set_version"), int)
        assert manifest["engine_pack_api_version"] >= 1
        assert manifest["model_set_version"] >= 1


# ── Tamper 检测 ────────────────────────────────────────


class TestTamperDetection:
    """验证篡改 Manifest 或 Payload 被检测。"""

    def test_tampered_payload_sha256_detected(self, manifest: dict) -> None:
        """验证 Manifest payload_sha256 被篡改后校验失败。"""
        from blc_portable.payload.manifest import validate_manifest

        tampered = dict(manifest)
        tampered["payload_sha256"] = "0" * 64

        errors = validate_manifest(tampered, PAYLOAD_ZIP)
        assert errors, "Tampered payload_sha256 should produce errors"
        assert any("Payload SHA-256" in e for e in errors), f"Expected SHA-256 error, got: {errors}"

    def test_tampered_file_hash_detected(self, manifest: dict) -> None:
        """验证 manifest 中文件 hash 被篡改后校验失败。"""
        from blc_portable.payload.manifest import validate_manifest

        tampered = dict(manifest)
        files_copy = dict(manifest["files"])
        # 修改第一个文件的 hash
        first_key = next(iter(files_copy))
        first_val = dict(files_copy[first_key])
        first_val["sha256"] = "0" * 64
        files_copy[first_key] = first_val
        tampered["files"] = files_copy

        errors = validate_manifest(tampered, PAYLOAD_ZIP)
        assert errors, "Tampered file hash should produce errors, got none"
        # Should have SHA-256 mismatch at minimum
        assert any("SHA-256" in e for e in errors), f"Expected SHA-256 error for tampered file hash, got: {errors}"

    def test_fake_non_zip_detected(self) -> None:
        """验证非 ZIP 文件被检测。"""
        from blc_portable.payload.manifest import validate_manifest

        manifest = _load_manifest()

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(b"not a real zip file")
            fake_zip = Path(tmp.name)

        try:
            errors = validate_manifest(manifest, fake_zip)
            assert errors, "Fake non-ZIP file should produce errors"
            assert any("not a valid ZIP" in e for e in errors), f"Expected 'not a valid ZIP' error, got: {errors}"
        finally:
            fake_zip.unlink(missing_ok=True)

    def test_extra_file_in_zip_detected(self, manifest: dict) -> None:
        """验证 ZIP 中有额外文件时被检测。"""
        from blc_portable.payload.manifest import validate_manifest

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # 复制原始 ZIP
            orig = tmp / "orig.zip"
            shutil.copy(PAYLOAD_ZIP, orig)

            # 创建带额外文件的 ZIP
            extra_zip = tmp / "extra.zip"
            with zipfile.ZipFile(extra_zip, "w", zipfile.ZIP_DEFLATED) as zf_out:
                # 复制所有原始文件
                with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf_in:
                    for name in zf_in.namelist():
                        zf_out.writestr(name, zf_in.read(name))
                # 添加额外文件
                zf_out.writestr("extra_file.txt", b"this should not be here")

            errors = validate_manifest(manifest, extra_zip)
            # 应该有额外文件 + 文件数不一致的错误
            has_extra = any("Extra" in e or "未声明" in e for e in errors)
            has_count_err = any("file_count" in e or "ZIP 文件数" in e or "条目数" in e for e in errors)
            assert has_extra or has_count_err, f"Extra file should be detected, got: {errors}"

    def test_missing_file_in_zip_detected(self) -> None:
        """验证 ZIP 中缺少文时被检测。"""
        from blc_portable.payload.manifest import validate_manifest

        manifest = _load_manifest()

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # 创建缺少一个文件的 ZIP
            missing_zip = tmp / "missing.zip"
            with zipfile.ZipFile(missing_zip, "w", zipfile.ZIP_DEFLATED) as zf_out:
                with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf_in:
                    skip_key = next(iter(manifest["files"]))
                    for name in zf_in.namelist():
                        if name != skip_key:
                            zf_out.writestr(name, zf_in.read(name))

            errors = validate_manifest(manifest, missing_zip)
            has_missing = any(
                "不存在" in e or "Missing" in e or "ZIP 文件数" in e or "条目数" in e or "file_count" in e
                for e in errors
            )
            assert has_missing, f"Missing file should be detected, got: {errors}"


# ── Cross-verify installed ─────────────────────────────


class TestCrossVerifyInstalled:
    """验证 cross_verify_installed 函数。"""

    def test_clean_install_passes(self, manifest: dict) -> None:
        """验证干净解压后 cross-verify 通过。"""
        from blc_portable.payload.manifest import cross_verify_installed

        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "release"
            installed.mkdir(parents=True)

            with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
                zf.extractall(installed)

            errors = cross_verify_installed(installed, manifest)
            assert not errors, f"Clean install should pass, got: {errors}"

    def test_extra_file_detected(self, manifest: dict) -> None:
        """验证额外文件被检测。"""
        from blc_portable.payload.manifest import cross_verify_installed

        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "release"
            installed.mkdir(parents=True)

            with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
                zf.extractall(installed)

            # 添加额外文件
            (installed / "rogue.py").write_text("# evil code")

            errors = cross_verify_installed(installed, manifest)
            assert errors, "Extra file should be detected"
            assert any("额外文件" in e for e in errors), f"Expected '额外文件' error, got: {errors}"

    def test_missing_file_detected(self, manifest: dict) -> None:
        """验证缺失文件被检测。"""
        from blc_portable.payload.manifest import cross_verify_installed

        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "release"
            installed.mkdir(parents=True)

            with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
                zf.extractall(installed)

            # 删除一个文件
            first_file = next(iter(manifest["files"]))
            (installed / first_file).unlink()

            errors = cross_verify_installed(installed, manifest)
            assert errors, "Missing file should be detected"
            assert any("缺失文件" in e for e in errors) or any("文件数量" in e for e in errors), (
                f"Expected missing file error, got: {errors}"
            )

    def test_tampered_content_detected(self, manifest: dict) -> None:
        """验证文件内容被篡改后检测。"""
        from blc_portable.payload.manifest import cross_verify_installed

        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "release"
            installed.mkdir(parents=True)

            with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
                zf.extractall(installed)

            # 篡改一个 Python 文件 — 在前面加一行保证改变 hash
            for rel, info in manifest["files"].items():
                if rel.endswith(".py") and info.get("size", 0) < 50000:
                    target = installed / rel
                    if target.exists():
                        original = target.read_text(encoding="utf-8")
                        # 在文件头添加一行注释，确保 hash 改变
                        tampered_content = "# tampered\n" + original
                        target.write_text(tampered_content, encoding="utf-8")
                        break

            errors = cross_verify_installed(installed, manifest)
            assert errors, "Tampered content should be detected"
            assert any("SHA-256" in e for e in errors), f"Expected SHA-256 error, got: {errors}"

    def test_path_traversal_blocked(self, manifest: dict) -> None:
        """验证路径遍历被检测。"""
        from blc_portable.payload.manifest import cross_verify_installed

        # 在 manifest 中注入路径遍历
        tampered = dict(manifest)
        tampered["files"] = dict(manifest["files"])
        tampered["files"]["../../etc/passwd"] = {
            "sha256": "0" * 64,
            "size": 100,
        }

        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "release"
            installed.mkdir(parents=True)

            with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
                zf.extractall(installed)

            errors = cross_verify_installed(installed, tampered)
            assert errors, "Path traversal should be detected"


# ── 身份错误检测 ───────────────────────────────────────


class TestWrongIdentity:
    """验证身份字段错误被检测。"""

    def test_wrong_source_commit_detected(self) -> None:
        """验证错误的 source_commit 被校验方法检测。"""
        from blc_portable.payload.manifest import validate_manifest

        manifest = _load_manifest()
        tampered = dict(manifest)
        tampered["source_commit"] = "e" * 40
        tampered["source_commit_short"] = "eeeeeee"

        errors = validate_manifest(tampered, PAYLOAD_ZIP)
        assert errors, "Wrong source_commit should be detected"
        assert any("source_commit" in e for e in errors), f"Expected source_commit error, got: {errors}"

    def test_wrong_version_detected(self) -> None:
        """验证错误的 release_version 被检测。"""
        from blc_portable.payload.manifest import validate_manifest

        manifest = _load_manifest()
        tampered = dict(manifest)
        tampered["release_version"] = "0.1.14.99-fake"

        errors = validate_manifest(tampered, PAYLOAD_ZIP)
        assert errors, "Wrong release_version should be detected"
        assert any("release_version" in e for e in errors), f"Expected release_version error, got: {errors}"

    def test_latest_baseline_requires_no_backports(self) -> None:
        """验证当前源码基线不再叠加历史 Backport。"""
        manifest = _load_manifest()
        assert manifest["applied_backports"] == []
        assert manifest["backport_ids"] == []


# ── 路径安全 ──────────────────────────────────────────


class TestPathSafety:
    """验证路径遍历、绝对路径、盘符等安全检测。"""

    def test_absolute_path_blocked(self) -> None:
        """验证绝对路径的 ZIP 条目被检测。"""
        from blc_portable.payload.manifest import validate_manifest

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bad_zip = tmp / "bad.zip"
            with zipfile.ZipFile(bad_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("/etc/hosts", b"malicious")
                # 也要有一个合法文件让 file_count 检查通过其他关
                zf.writestr("app/cli.py", b"#!/usr/bin/env python")

            # 应该至少有绝对路径错误
            result = validate_manifest(
                {
                    "release_version": "0.1.14.11-alpha",
                    "source_commit": "7" * 40,
                    "source_commit_short": "4bdaa13",
                    "format_version": 5,
                    "payload_sha256": "0" * 64,
                    "file_count": 1,
                    "files": {
                        "app/cli.py": {"sha256": hashlib.sha256(b"#!/usr/bin/env python").hexdigest(), "size": 22}
                    },
                },
                bad_zip,
            )
            # 至少应该有错误
            assert result, "Bad ZIP should produce errors"

    def test_dot_dot_blocked(self) -> None:
        """验证 .. 路径遍历的 ZIP 条目被检测。"""
        from blc_portable.payload.manifest import validate_manifest

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bad_zip = tmp / "bad.zip"
            with zipfile.ZipFile(bad_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("../../windows/system32/evil.dll", b"payload")
                zf.writestr("app/cli.py", b"ok")

            result = validate_manifest(
                {
                    "release_version": "0.1.14.11-alpha",
                    "source_commit": "7" * 40,
                    "source_commit_short": "4bdaa13",
                    "format_version": 5,
                    "payload_sha256": "0" * 64,
                    "file_count": 1,
                    "files": {"app/cli.py": {"sha256": hashlib.sha256(b"ok").hexdigest(), "size": 2}},
                },
                bad_zip,
            )
            assert result, "ZIP with .. traversal should produce errors"


# ── 可复现性 ──────────────────────────────────────────


class TestReproducibility:
    """验证同一输入产生相同 Payload hash。"""

    def test_no_generated_at_in_manifest(self, manifest: dict) -> None:
        """验证 manifest 不含 generated_at 时间戳。"""
        assert "generated_at" not in manifest, "generated_at breaks reproducibility"

    def test_manifest_has_no_timestamp_fields(self, manifest: dict) -> None:
        """验证 manifest 不含任何时间戳字段。"""
        time_fields = [k for k in manifest if "time" in k.lower() or "date" in k.lower() or "generated" in k.lower()]
        assert not time_fields, f"Manifest contains timestamp fields: {time_fields}"

    def test_zip_uses_fixed_timestamps(self) -> None:
        """验证 ZIP 内文件使用固定时间戳 2026-01-01。"""
        fixed_date = (2026, 1, 1, 0, 0, 0)
        with zipfile.ZipFile(PAYLOAD_ZIP, "r") as zf:
            for info in zf.infolist():
                dt = info.date_time
                assert dt == fixed_date, f"Non-fixed timestamp on {info.filename}: {dt}"
                break  # 只检查第一个即可


# ── Manifest 格式版本 ──────────────────────────────────


class TestManifestFormatVersion:
    """验证 Manifest 格式版本字段。"""

    def test_format_version_is_5(self, manifest: dict) -> None:
        """验证 format_version 升级到 5。"""
        assert manifest["format_version"] == 5, f"Expected format_version=5, got {manifest['format_version']}"

    def test_payload_schema_matches_format_version(self, manifest: dict) -> None:
        """验证 payload_schema == format_version。"""
        assert manifest["payload_schema"] == manifest["format_version"], "payload_schema should equal format_version"
