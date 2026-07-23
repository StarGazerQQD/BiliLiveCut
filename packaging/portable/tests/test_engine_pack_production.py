"""Engine Pack 生产构建、校验和发布流程测试 — 阶段 5。

覆盖:
- artifact_class 显式存在性 (缺失/空/fixture 不可伪装 production)
- production 尺寸门禁 (>=500MB)
- redistribution_verified 门禁
- engine_pack_info.json 所有必需字段
- check_engine_pack_info (lite.py) 严格校验
- schema.py ExternalMetadata.validate()
- installer.py check_installed_models full rehash mode
- verifier 逐文件校验/ZIP 损坏/单文件损坏/文件删除/额外文件
- fixture 冒充 production 检测
- 版本/API 兼容性
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest

# 路径设置
_src_dir = str(Path(__file__).resolve().parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)
_portable_dir = Path(__file__).resolve().parent.parent

_RESOURCES = _portable_dir / "resources"
_EP_INFO_PATH = _RESOURCES / "engine_pack_info.json"


# ── Helpers ────────────────────────────────────────────


def _make_mini_zip(tmp_dir: Path, name: str = "test.zip", files: dict | None = None) -> Path:
    """Create a small ZIP for testing.

    :param tmp_dir: temp directory.
    :param name: ZIP filename.
    :param files: {filename: content} dict.
    :returns: ZIP path.
    """
    zip_path = tmp_dir / name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, content in (files or {"test.txt": "hello"}).items():
            zf.writestr(fname, content)
    return zip_path


def _compute_crc32(path: Path) -> str:
    """Stream CRC32 of a file."""
    crc = 0
    with path.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            crc = zlib.crc32(chunk, crc)
    return f"{crc & 0xFFFFFFFF:08X}"


def _compute_sha256(path: Path) -> str:
    """Stream SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


# ── Fixture 冒充 Production 检测 ────────────────────────


class TestFixtureImpersonationDetection:
    """验证 fixture 不能伪装成 production。"""

    def test_committed_info_is_fixture_not_production(self) -> None:
        """仓库中提交的 engine_pack_info.json 必须是 fixture。"""
        if not _EP_INFO_PATH.exists():
            pytest.skip("engine_pack_info.json not present")
        info = json.loads(_EP_INFO_PATH.read_text(encoding="utf-8"))
        assert info.get("artifact_class") == "fixture", "Committed engine_pack_info.json must be artifact_class=fixture"

    def test_missing_artifact_class_rejected_by_schema(self) -> None:
        """无效 artifact_class 被 schema 拒绝。"""
        from blc_portable.engine_pack.schema import ExternalMetadata

        meta = ExternalMetadata(
            artifact_class="",
            crc32="ABCDEF01",
            sha256="a" * 64,
            content_manifest_sha256="b" * 64,
            size_bytes=100,
            expected_engine_ids=["whisper"],
        )
        errors = meta.validate()
        assert any("artifact_class" in e.lower() for e in errors), (
            f"Missing artifact_class should be rejected: {errors}"
        )

    def test_invalid_artifact_class_rejected_by_schema(self) -> None:
        """错误的 artifact_class 被 schema 拒绝。"""
        from blc_portable.engine_pack.schema import ExternalMetadata

        meta = ExternalMetadata(
            artifact_class="fake",
            crc32="ABCDEF01",
            sha256="a" * 64,
            content_manifest_sha256="b" * 64,
            size_bytes=100,
            expected_engine_ids=["whisper"],
        )
        errors = meta.validate()
        assert any("invalid" in e.lower() for e in errors), f"Invalid artifact_class should be rejected: {errors}"

    def test_fixture_ep_info_fails_production_check(self) -> None:
        """Fixture 的 engine_pack_info 不通过 production 校验。"""
        from blc_portable.builders.lite import check_engine_pack_info

        with pytest.raises(RuntimeError, match="fixture|production|500|artifact_class"):
            check_engine_pack_info()

    def test_artifact_class_default_not_production(self) -> None:
        """artifact_class 若缺失，不可默认为 production。"""
        # schema.py: ExternalMetadata 的 artifact_class 默认值现在是空字符串
        from blc_portable.engine_pack.schema import ExternalMetadata

        meta = ExternalMetadata()
        assert meta.artifact_class == "", "artifact_class default must be empty, not 'production'"

    def test_builder_rejects_tiny_production_archive(self) -> None:
        """正式构建器必须拒绝小体积占位包。"""
        from blc_portable.engine_pack.builder import validate_production_metadata

        errors = validate_production_metadata(
            "ABCDEF01",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 40,
            1024,
        )
        assert any("too small" in error for error in errors)


# ── engine_pack_info.json 必需字段 ──────────────────────


class TestEnginePackInfoFields:
    """验证 engine_pack_info.json 包含所有必需字段。"""

    REQUIRED = [
        "format_version",
        "artifact_class",
        "engine_pack_version",
        "engine_pack_api_version",
        "model_set_version",
        "filename",
        "size_bytes",
        "crc32",
        "sha256",
        "content_manifest_sha256",
        "model_lock_sha256",
        "source_commit",
        "builder_commit",
        "build_timestamp",
        "expected_engine_ids",
    ]

    def test_all_required_fields_present(self) -> None:
        """验证 engine_pack_info.json 所有必需字段存在。"""
        if not _EP_INFO_PATH.exists():
            pytest.skip("engine_pack_info.json not present")
        info = json.loads(_EP_INFO_PATH.read_text(encoding="utf-8"))
        for field in self.REQUIRED:
            assert field in info, f"Missing required field: {field}"

    def test_crc32_format_valid(self) -> None:
        """CRC32 必须是 8 位大写 hex。"""
        if not _EP_INFO_PATH.exists():
            pytest.skip("engine_pack_info.json not present")
        info = json.loads(_EP_INFO_PATH.read_text(encoding="utf-8"))
        crc = info["crc32"]
        assert len(crc) == 8, f"CRC32 length: {len(crc)}"
        assert all(c in "0123456789ABCDEF" for c in crc)

    def test_expected_engine_ids_complete(self) -> None:
        """expected_engine_ids 必须是四个引擎。"""
        if not _EP_INFO_PATH.exists():
            pytest.skip("engine_pack_info.json not present")
        info = json.loads(_EP_INFO_PATH.read_text(encoding="utf-8"))
        assert set(info["expected_engine_ids"]) == {"whisper", "paraformer", "sensevoice", "funasr_nano"}

    def test_external_manifest_matches_packaged_content_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """外部内容 Manifest 必须与写入 ZIP 的版本逐字一致，且不得把自身加入文件清单。"""
        from blc_portable.engine_pack import builder

        dist_dir = tmp_path / "dist"
        resources_dir = tmp_path / "resources"
        monkeypatch.setattr(builder, "DIST_DIR", dist_dir)
        monkeypatch.setattr(builder, "RESOURCES_DIR", resources_dir)

        content_manifest = {
            "format_version": 4,
            "engine_pack_version": "0.1.15.2-alpha",
            "total_files": 1,
            "fixture": True,
            "engines": [],
            "files": {"models/fixture/model.bin": {"size": 4, "sha256": "0" * 64}},
        }
        content_manifest_path = tmp_path / "engine-pack-manifest.json"
        content_manifest_path.write_text(json.dumps(content_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        archive_path = tmp_path / "fixture.zip"
        archive_path.write_bytes(b"fixture")

        result = builder.write_output_files(
            crc32_val="1234ABCD",
            sha256_val="a" * 64,
            archive_path=archive_path,
            source_commit="f2c291df2409bdf83dbf8f8a30d6b3ee1d44e8e0",
            content_manifest_path=content_manifest_path,
            is_fixture=True,
        )

        assert (dist_dir / "engine-pack-manifest.json").read_bytes() == content_manifest_path.read_bytes()
        assert (dist_dir / "engine-pack-info.json").read_bytes() == (
            resources_dir / "engine_pack_info.json"
        ).read_bytes()
        assert result["file_count"] == 1
        assert "engine-pack-manifest.json" not in content_manifest["files"]

    def test_content_manifest_uses_installer_contract(self, tmp_path: Path) -> None:
        """生产构建器的内部 Manifest 必须能被实际安装器加载。"""
        from blc_portable.engine_pack.manifest import load_manifest

        content_manifest = {
            "format_version": 4,
            "engine_pack_version": "0.1.15.2-alpha",
            "portable_release_version": "0.1.15.2-alpha",
            "source_commit": "f2c291df2409bdf83dbf8f8a30d6b3ee1d44e8e0",
            "source_commit_short": "f2c291d",
            "engines": [
                {
                    "engine_id": engine_id,
                    "engine_name": engine_id,
                    "model_id": f"fixture/{engine_id}",
                    "hub": "huggingface",
                    "revision": "fixture",
                    "target_path": f"models/{engine_id}",
                }
                for engine_id in ("whisper", "paraformer", "sensevoice", "funasr_nano")
            ],
            "total_files": 0,
            "files": {},
        }
        manifest_path = tmp_path / "engine-pack-manifest.json"
        manifest_path.write_text(json.dumps(content_manifest), encoding="utf-8")

        manifest = load_manifest(manifest_path)

        assert manifest.format_version == 4
        assert manifest.archive_crc32 == ""


# ── 版本/API 兼容性 ────────────────────────────────────


class TestVersionAPICompatibility:
    """验证版本和 API 兼容性检测。"""

    def test_engine_pack_version_matches_manifest(self) -> None:
        """engine_pack_info.json 和 main manifest 版本一致。"""
        if not _EP_INFO_PATH.exists():
            pytest.skip("engine_pack_info.json not present")
        info = json.loads(_EP_INFO_PATH.read_text(encoding="utf-8"))
        ep_ver = info["engine_pack_version"]

        from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION

        assert ep_ver == ENGINE_PACK_VERSION, f"Version mismatch: ep_info={ep_ver} manifest.py={ENGINE_PACK_VERSION}"

    def test_engine_pack_api_version_exists(self) -> None:
        """engine_pack_api_version 必须 >= 1。"""
        if not _EP_INFO_PATH.exists():
            pytest.skip("engine_pack_info.json not present")
        info = json.loads(_EP_INFO_PATH.read_text(encoding="utf-8"))
        api_ver = info.get("engine_pack_api_version", 0)
        assert api_ver >= 1, f"engine_pack_api_version={api_ver}"

    def test_model_set_version_exists(self) -> None:
        """model_set_version 必须 >= 1。"""
        if not _EP_INFO_PATH.exists():
            pytest.skip("engine_pack_info.json not present")
        info = json.loads(_EP_INFO_PATH.read_text(encoding="utf-8"))
        ms_ver = info.get("model_set_version", 0)
        assert ms_ver >= 1, f"model_set_version={ms_ver}"


# ── ZIP 损坏检测 ──────────────────────────────────────


class TestZipCorruptionDetection:
    """验证 ZIP 损坏、单文件损坏、文件删除检测。"""

    def test_corrupt_zip_detected_by_crc32(self) -> None:
        """CRC32 校验检测到损坏的 ZIP。"""
        from blc_portable.engine_pack.verifier import verify_archive_metadata

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _make_mini_zip(Path(tmp))
            sha = _compute_sha256(zip_path)
            real_crc = _compute_crc32(zip_path)

            # 伪造 CRC32 -> 错误
            errors = verify_archive_metadata(zip_path, "FFFFFFFF", sha)
            assert any("CRC32" in e for e in errors), f"Fake CRC32 should be detected: {errors}"

            # 正确 CRC32 -> 无错误
            errors = verify_archive_metadata(zip_path, real_crc, sha)
            assert not errors, f"Correct CRC32 should pass: {errors}"

    def test_corrupt_zip_detected_by_sha256(self) -> None:
        """SHA-256 校验检测到损坏的 ZIP。"""
        from blc_portable.engine_pack.verifier import verify_archive_metadata

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = _make_mini_zip(Path(tmp))
            real_crc = _compute_crc32(zip_path)

            errors = verify_archive_metadata(zip_path, real_crc, "b" * 64)
            assert any("SHA-256" in e for e in errors), f"Fake SHA-256 should be detected: {errors}"

    def test_single_file_corruption_detected(self) -> None:
        """单文件内容损坏被 extract tree 检测。"""
        from blc_portable.engine_pack.verifier import verify_extracted_tree

        with tempfile.TemporaryDirectory() as tmp:
            extracted = Path(tmp) / "extracted"
            extracted.mkdir()
            (extracted / "models").mkdir()
            (extracted / "models" / "whisper").mkdir()
            file_path = extracted / "models" / "whisper" / "model.bin"
            file_path.write_bytes(b"original content")

            sha = hashlib.sha256(b"original content").hexdigest()

            manifest: dict[str, Any] = {
                "engines": [{"engine_id": "whisper", "target_path": "models/whisper"}],
                "files": {
                    "models/whisper/model.bin": {"size": 16, "sha256": sha},
                },
            }

            # Clean → pass
            errors = verify_extracted_tree(extracted, manifest)
            assert not errors, f"Clean tree should pass: {errors}"

            # Corrupt → fail
            file_path.write_bytes(b"tampered content here")
            errors = verify_extracted_tree(extracted, manifest)
            assert errors, "Tampered content should be detected"
            assert any("SHA-256" in e for e in errors), f"Expected SHA-256 error: {errors}"

    def test_missing_file_detected(self) -> None:
        """缺失文件被 extract tree 检测。"""
        from blc_portable.engine_pack.verifier import verify_extracted_tree

        with tempfile.TemporaryDirectory() as tmp:
            extracted = Path(tmp) / "extracted"
            extracted.mkdir()

            manifest: dict[str, Any] = {
                "engines": [],
                "files": {
                    "models/whisper/missing.bin": {"size": 100, "sha256": "a" * 64},
                },
            }
            errors = verify_extracted_tree(extracted, manifest)
            assert any("缺失文件" in e for e in errors), f"Missing file should be detected: {errors}"

    def test_extra_file_detected(self) -> None:
        """额外文件被 extract tree 检测。"""
        from blc_portable.engine_pack.verifier import verify_extracted_tree

        with tempfile.TemporaryDirectory() as tmp:
            extracted = Path(tmp) / "extracted"
            extracted.mkdir()
            extracted_sub = extracted / "models" / "whisper"
            extracted_sub.mkdir(parents=True)
            (extracted_sub / "model.bin").write_bytes(b"data")
            (extracted_sub / "rogue.dll").write_bytes(b"evil")

            manifest: dict[str, Any] = {
                "engines": [{"engine_id": "whisper", "target_path": "models/whisper"}],
                "files": {
                    "models/whisper/model.bin": {
                        "size": 4,
                        "sha256": hashlib.sha256(b"data").hexdigest(),
                    },
                },
            }
            errors = verify_extracted_tree(extracted, manifest)
            assert any("多余文件" in e for e in errors), f"Extra file should be detected: {errors}"


# ── installer check_installed_models 完整重哈希 ─────────


class TestFullRehash:
    """验证 installer 完整重哈希模式。"""

    def test_full_rehash_missing_file(self) -> None:
        """完整重哈希检测缺失文件。"""
        from blc_portable.engine_pack.installer import check_installed_models

        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)

            for eng in ["whisper", "paraformer", "sensevoice", "funasr_nano"]:
                (models_dir / eng).mkdir()
                (models_dir / eng / "model.bin").write_bytes(b"data")

            (models_dir / "engine-pack-installed.json").write_text(
                json.dumps(
                    {
                        "engine_pack_version": "0.1.14.11-alpha",
                        "engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                        "files": {
                            "whisper": {
                                "files": {
                                    "model.bin": {"size": 4, "sha256": hashlib.sha256(b"data").hexdigest()},
                                    "missing.bin": {"size": 10, "sha256": "b" * 64},
                                }
                            },
                            "paraformer": {"files": {}},
                            "sensevoice": {"files": {}},
                            "funasr_nano": {"files": {}},
                        },
                    }
                )
            )

            ok, errors = check_installed_models(models_dir, "0.1.14.11-alpha", full_rehash=True)
            assert not ok, "Should fail on missing file"
            assert any("Missing" in e for e in errors), f"Expected missing file error: {errors}"

    def test_full_rehash_sha_mismatch(self) -> None:
        """完整重哈希检测 SHA-256 不匹配。"""
        from blc_portable.engine_pack.installer import check_installed_models

        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)

            for eng in ["whisper", "paraformer", "sensevoice", "funasr_nano"]:
                (models_dir / eng).mkdir()
                (models_dir / eng / "model.bin").write_bytes(b"data")

            (models_dir / "engine-pack-installed.json").write_text(
                json.dumps(
                    {
                        "engine_pack_version": "0.1.14.11-alpha",
                        "engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                        "files": {
                            "whisper": {
                                "files": {
                                    "model.bin": {"size": 4, "sha256": "0" * 64},
                                }
                            },
                            "paraformer": {"files": {}},
                            "sensevoice": {"files": {}},
                            "funasr_nano": {"files": {}},
                        },
                    }
                )
            )

            ok, errors = check_installed_models(models_dir, "0.1.14.11-alpha", full_rehash=True)
            assert not ok, "Should fail on SHA mismatch"
            assert any("SHA-256" in e for e in errors), f"Expected SHA-256 error: {errors}"

    def test_full_rehash_passes_clean(self) -> None:
        """完整重哈希通过干净安装。"""
        from blc_portable.engine_pack.installer import check_installed_models

        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)

            for eng in ["whisper", "paraformer", "sensevoice", "funasr_nano"]:
                (models_dir / eng).mkdir()
                content = f"{eng}-data".encode()
                (models_dir / eng / "model.bin").write_bytes(content)

            files_section = {}
            for eng in ["whisper", "paraformer", "sensevoice", "funasr_nano"]:
                content = f"{eng}-data".encode()
                files_section[eng] = {
                    "files": {
                        "model.bin": {
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    }
                }

            (models_dir / "engine-pack-installed.json").write_text(
                json.dumps(
                    {
                        "engine_pack_version": "0.1.14.11-alpha",
                        "engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                        "files": files_section,
                    }
                )
            )

            ok, errors = check_installed_models(models_dir, "0.1.14.11-alpha", full_rehash=True)
            assert ok, f"Clean install should pass: {errors}"

    def test_quick_check_without_rehash(self) -> None:
        """快速检查 (无 full_rehash) 不对文件重哈希。"""
        from blc_portable.engine_pack.installer import check_installed_models

        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)

            for eng in ["whisper", "paraformer", "sensevoice", "funasr_nano"]:
                (models_dir / eng).mkdir()
                (models_dir / eng / "model.bin").write_bytes(b"data")

            # Bad SHA but quick mode doesn't check files
            (models_dir / "engine-pack-installed.json").write_text(
                json.dumps(
                    {
                        "engine_pack_version": "0.1.14.11-alpha",
                        "engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                        "files": {
                            "whisper": {
                                "files": {
                                    "model.bin": {"size": 4, "sha256": "0" * 64},
                                }
                            },
                            "paraformer": {"files": {}},
                            "sensevoice": {"files": {}},
                            "funasr_nano": {"files": {}},
                        },
                    }
                )
            )

            # Quick mode should pass (doesn't rehash files)
            ok, _ = check_installed_models(models_dir, "0.1.14.11-alpha", full_rehash=False)
            assert ok, "Quick check should pass regardless of file hash"


# ── Verifier 集成测试 ──────────────────────────────────


class TestVerifierIntegration:
    """验证 verifier 的集成功能。"""

    def test_verify_engine_pack_complete_passes(self, tmp_path: Path) -> None:
        """完整验证通过干净的 Engine Pack。"""
        from blc_portable.engine_pack.verifier import verify_engine_pack_complete

        d = tmp_path / "pack"
        d.mkdir()
        all_engines = ("whisper", "paraformer", "sensevoice", "funasr_nano")
        for eng in all_engines:
            (d / "models" / eng).mkdir(parents=True)
            (d / "models" / eng / "model.bin").write_bytes(b"data")

        sha = hashlib.sha256(b"data").hexdigest()
        manifest = {
            "schema_version": 4,
            "engine_pack_version": "0.1.14.11-alpha",
            "total_files": 4,
            "engines": [{"engine_id": eng, "target_path": f"models/{eng}"} for eng in all_engines],
            "files": {f"models/{eng}/model.bin": {"size": 4, "sha256": sha} for eng in all_engines},
        }
        manifest_path = d / "engine-pack-manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        # Create ZIP with all files
        zip_path = tmp_path / "ep.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(d.rglob("*")):
                if p.is_file() and p != zip_path:
                    zf.write(p, p.relative_to(d).as_posix())

        sha256 = _compute_sha256(zip_path)
        crc32 = _compute_crc32(zip_path)

        # Extract ZIP to verify destination
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        ok, errors = verify_engine_pack_complete(
            zip_path,
            extract_dir / "engine-pack-manifest.json",
            crc32,
            sha256,
            "0.1.14.11-alpha",
        )
        assert ok, f"Complete verify should pass: {errors}"

    def test_verify_engine_pack_complete_crc_mismatch(self, tmp_path: Path) -> None:
        """CRC 不匹配被完整验证检测。"""
        from blc_portable.engine_pack.verifier import verify_engine_pack_complete

        d = tmp_path / "pack"
        d.mkdir()

        manifest = {
            "schema_version": 4,
            "engine_pack_version": "0.1.14.11-alpha",
            "total_files": 0,
            "engines": [],
            "files": {},
        }
        manifest_path = d / "engine-pack-manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        zip_path = _make_mini_zip(tmp_path, files={"hello.txt": "world"})

        sha256 = _compute_sha256(zip_path)

        ok, errors = verify_engine_pack_complete(zip_path, manifest_path, "FFFFFFFF", sha256, "0.1.14.11-alpha")
        assert not ok, "CRC mismatch should fail"


# ── Production 构建门禁 ────────────────────────────────


class TestProductionGates:
    """验证 production 构建的各种门禁。"""

    def test_empty_crc32_blocked(self) -> None:
        """空 CRC32 被 schema 检测。"""
        from blc_portable.engine_pack.schema import ExternalMetadata

        meta = ExternalMetadata(
            artifact_class="production",
            sha256="a" * 64,
            content_manifest_sha256="b" * 64,
            size_bytes=100,
            expected_engine_ids=["whisper"],
        )
        errors = meta.validate()
        assert any("CRC32" in e for e in errors), f"Empty CRC32 should be rejected: {errors}"

    def test_empty_sha256_blocked(self) -> None:
        """空 SHA-256 被 schema 检测。"""
        from blc_portable.engine_pack.schema import ExternalMetadata

        meta = ExternalMetadata(
            artifact_class="production",
            crc32="ABCDEF01",
            content_manifest_sha256="b" * 64,
            size_bytes=100,
            expected_engine_ids=["whisper"],
        )
        errors = meta.validate()
        assert any("SHA-256" in e for e in errors), f"Empty SHA-256 should be rejected: {errors}"

    def test_empty_model_lock_sha256_blocked(self) -> None:
        """空 model_lock_sha256 被 schema 检测。"""
        from blc_portable.engine_pack.schema import ExternalMetadata

        meta = ExternalMetadata(
            artifact_class="production",
            crc32="ABCDEF01",
            sha256="a" * 64,
            content_manifest_sha256="b" * 64,
            size_bytes=100,
            expected_engine_ids=["whisper"],
        )
        errors = meta.validate()
        assert any("model_lock_sha256" in e for e in errors), f"Empty model_lock_sha256 should be rejected: {errors}"


# ── 模型 lock 完整性 ───────────────────────────────────


class TestModelLockIntegrity:
    """验证 model_sources.lock.json 完整性。"""

    def test_model_lock_has_resolved_revision(self) -> None:
        """每个 engine 都有 resolved_revision。"""
        lock_path = _portable_dir / "config" / "model_sources.lock.json"
        if not lock_path.exists():
            pytest.skip("model_sources.lock.json not found")
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        for engine in data["engines"]:
            rev = engine.get("resolved_revision", "")
            assert rev, f"Engine {engine['engine_id']} missing resolved_revision"

    def test_model_lock_has_required_files(self) -> None:
        """每个 engine 都有 required_files 列表。"""
        lock_path = _portable_dir / "config" / "model_sources.lock.json"
        if not lock_path.exists():
            pytest.skip("model_sources.lock.json not found")
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        for engine in data["engines"]:
            rf = engine.get("required_files", [])
            assert rf, f"Engine {engine['engine_id']} has no required_files"

    def test_model_lock_has_license_info(self) -> None:
        """每个 engine 都有许可证信息。"""
        lock_path = _portable_dir / "config" / "model_sources.lock.json"
        if not lock_path.exists():
            pytest.skip("model_sources.lock.json not found")
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        for engine in data["engines"]:
            lic = engine.get("license", {})
            assert lic.get("name"), f"Engine {engine['engine_id']} missing license name"
            assert "redistribution_verified" in lic, f"Engine {engine['engine_id']} missing redistribution_verified"


# ── Catalog validation ──────────────────────────────────


class TestCatalogValidation:
    """验证 model_catalog.py 校验逻辑。"""

    def test_validate_catalog_passes(self) -> None:
        """validate_catalog 对当前数据返回空错误。"""
        config_dir = str(_portable_dir / "config")
        if config_dir not in sys.path:
            sys.path.insert(0, config_dir)
        from model_catalog import validate_catalog

        errors = validate_catalog()
        assert not errors, f"Catalog validation should pass: {errors}"

    def test_redistribution_verified_field_exists(self) -> None:
        """每个 engine 都有 redistribution_verified 字段。"""
        config_dir = str(_portable_dir / "config")
        if config_dir not in sys.path:
            sys.path.insert(0, config_dir)
        from model_catalog import load_engines

        for e in load_engines():
            assert hasattr(e, "redistribution_verified"), (
                f"Engine {e.engine_id} missing redistribution_verified attribute"
            )
            assert isinstance(e.redistribution_verified, bool), (
                f"Engine {e.engine_id} redistribution_verified should be bool"
            )
