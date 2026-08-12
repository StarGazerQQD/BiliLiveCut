"""Engine Pack 当前 schema 与完整性验证测试。"""

from __future__ import annotations

import json
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

_portable_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_portable_dir / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_pack_helpers import current_manifest  # noqa: E402


class TestVerifier:
    """验证 ZIP 外部摘要、当前内部 Manifest 与目录树。"""

    def test_verify_archive_metadata_match(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import compute_sha256, verify_archive_metadata

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "hello world")
        crc = zlib.crc32(zip_path.read_bytes()) & 0xFFFFFFFF

        assert verify_archive_metadata(zip_path, f"{crc:08X}", compute_sha256(zip_path)) == []

    def test_verify_archive_metadata_crc_mismatch(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import compute_sha256, verify_archive_metadata

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "hello world")

        assert any(
            "CRC32" in error for error in verify_archive_metadata(zip_path, "AAAAAAAA", compute_sha256(zip_path))
        )

    def test_verify_archive_metadata_sha_mismatch(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_archive_metadata

        zip_path = tmp_path / "test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("a.txt", "hello world")

        assert any("SHA-256" in error for error in verify_archive_metadata(zip_path, "AAAAAAAA", "b" * 64))

    def test_verify_manifest_valid(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION
        from blc_portable.engine_pack.verifier import verify_archive_manifest

        raw = current_manifest({"models/whisper/model.bin": {"size": 100, "sha256": "a" * 64}})
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")

        assert verify_archive_manifest(manifest_path, ENGINE_PACK_VERSION) == []

    def test_verify_manifest_rejects_legacy_schema(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION
        from blc_portable.engine_pack.verifier import verify_archive_manifest

        raw = current_manifest({"models/whisper/model.bin": {"size": 100, "sha256": "a" * 64}})
        raw["schema_version"] = raw.pop("format_version")
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")

        errors = verify_archive_manifest(manifest_path, ENGINE_PACK_VERSION)
        assert any("无法解析" in error for error in errors)

    def test_verify_manifest_version_mismatch(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION
        from blc_portable.engine_pack.verifier import verify_archive_manifest

        raw = current_manifest({"models/whisper/model.bin": {"size": 100, "sha256": "a" * 64}})
        raw["engine_pack_version"] = "0.0.0-alpha"
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")

        assert verify_archive_manifest(manifest_path, ENGINE_PACK_VERSION)

    def test_verify_extracted_tree_missing_files(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_extracted_tree

        raw = current_manifest({"models/whisper/missing.txt": {"size": 10, "sha256": "b" * 64}})
        errors = verify_extracted_tree(tmp_path, raw)
        assert any("缺失文件" in error for error in errors)

    def test_verify_engine_pack_info_complete(self) -> None:
        from blc_portable.engine_pack.schema import SCHEMA_VERSION, ExternalMetadata

        info_path = _portable_dir / "resources" / "engine_pack_info.json"
        if not info_path.exists():
            pytest.skip("engine_pack_info.json not found")
        metadata = ExternalMetadata.from_dict(json.loads(info_path.read_text(encoding="utf-8")))
        assert metadata.format_version == SCHEMA_VERSION


class TestNoArchiveSelfHash:
    """内部 Manifest 不携带会产生自引用的归档摘要。"""

    def test_current_manifest_has_no_archive_hash_fields(self) -> None:
        raw = current_manifest()
        assert "archive_crc32" not in raw
        assert "archive_sha256" not in raw
        assert "schema_version" not in raw
