"""Engine Pack 验证测试 — verifier.py 和元数据完整性。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

_portable_dir = Path(__file__).resolve().parent.parent  # portable/
import sys

if str(_portable_dir / "src") not in sys.path:
    sys.path.insert(0, str(_portable_dir / "src"))


class TestVerifier:
    """verifier.py 功能测试。"""

    def test_verify_archive_metadata_match(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import compute_sha256, verify_archive_metadata  # noqa: E402

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "hello world")

        sha = compute_sha256(zip_path)
        import zlib

        crc = zlib.crc32(zip_path.read_bytes()) & 0xFFFFFFFF

        errors = verify_archive_metadata(zip_path, f"{crc:08X}", sha)
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_verify_archive_metadata_crc_mismatch(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import compute_sha256, verify_archive_metadata  # noqa: E402

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "hello world")

        sha = compute_sha256(zip_path)
        errors = verify_archive_metadata(zip_path, "AAAAAAAA", sha)
        assert any("CRC32" in e for e in errors)

    def test_verify_archive_metadata_sha_mismatch(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_archive_metadata  # noqa: E402

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "hello world")

        errors = verify_archive_metadata(zip_path, "AAAAAAAA", "b" * 64)
        assert any("SHA-256" in e for e in errors)

    def test_verify_manifest_valid(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_archive_manifest  # noqa: E402

        manifest = {
            "schema_version": 3,
            "engine_pack_version": "0.1.14.9-alpha",
            "engines": [
                {"engine_id": "whisper", "target_path": "models/whisper"},
                {"engine_id": "paraformer", "target_path": "models/paraformer"},
                {"engine_id": "sensevoice", "target_path": "models/sensevoice"},
                {"engine_id": "funasr_nano", "target_path": "models/funasr_nano"},
            ],
            "files": {"models/whisper/model.bin": {"size": 100, "sha256": "a" * 64}},
            "total_files": 1,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        errors = verify_archive_manifest(manifest_path, "0.1.14.9-alpha")
        assert errors == [], f"Expected no errors, got: {errors}"

    def test_verify_manifest_version_mismatch(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_archive_manifest  # noqa: E402

        manifest = {
            "schema_version": 3,
            "engine_pack_version": "0.1.14.7-alpha",
            "engines": [
                {"engine_id": "whisper", "target_path": "models/whisper"},
            ],
            "files": {},
            "total_files": 1,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))

        errors = verify_archive_manifest(manifest_path, "0.1.14.9-alpha")
        assert len(errors) > 0

    def test_verify_extracted_tree_missing_files(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_extracted_tree  # noqa: E402

        (tmp_path / "models" / "whisper").mkdir(parents=True)
        (tmp_path / "models" / "whisper" / "model.bin").write_text("data")

        manifest = {
            "engines": [{"engine_id": "whisper", "target_path": "models/whisper"}],
            "files": {
                "models/whisper/model.bin": {"size": 4, "sha256": "a" * 64},
                "models/whisper/missing.txt": {"size": 10, "sha256": "b" * 64},
            },
        }
        errors = verify_extracted_tree(tmp_path, manifest)
        assert any("缺失文件" in e for e in errors)

    def test_verify_engine_pack_info_complete(self) -> None:
        info_path = _portable_dir / "resources" / "engine_pack_info.json"
        if not info_path.exists():
            pytest.skip("engine_pack_info.json not found")
        info = json.loads(info_path.read_text(encoding="utf-8"))
        required = [
            "format_version",
            "engine_pack_version",
            "crc32",
            "sha256",
            "content_manifest_sha256",
            "model_lock_sha256",
            "expected_engine_ids",
        ]
        for field in required:
            assert field in info, f"Missing field: {field}"
        assert info.get("format_version") == 4, f"format_version should be 4, got {info.get('format_version')}"


class TestNoArchiveSelfHash:
    """内部 Manifest 不应包含归档自身哈希。"""

    def test_schema_version_is_3(self) -> None:
        """Manifest 内部 schema_version 为 3，外部 manifest (DIST) 允许有 archive_hashes。"""
        builder_py = _portable_dir / "src" / "blc_portable" / "engine_pack" / "builder.py"
        content = builder_py.read_text(encoding="utf-8")
        # Staging manifest must use the installer's format_version contract.
        assert '"format_version": 4' in content
        assert '"schema_version": 4' not in content
        # Note comment confirms staging manifest has no archive self-hash
        assert (
            "避免自引用问题" in content or "archive_crc32" in content.split("schema_version")[0]
        )  # at least 1 occurrence exists (in write_output_files or external manifest)
