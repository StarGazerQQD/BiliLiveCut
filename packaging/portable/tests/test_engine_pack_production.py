"""Engine Pack 当前生产契约、完整性与安装清单测试。"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

_portable_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_portable_dir / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_pack_helpers import (  # noqa: E402
    current_manifest,
    current_tree_manifest,
    external_metadata,
    installed_manifest,
)

_INFO_PATH = _portable_dir / "resources" / "engine_pack_info.json"
_ENGINES = ["whisper", "paraformer", "sensevoice", "funasr_nano"]


def _make_models(root: Path) -> None:
    """生成四引擎最小目录树。"""
    for engine_id in _ENGINES:
        target = root / "models" / engine_id
        target.mkdir(parents=True)
        (target / "model.bin").write_bytes(f"{engine_id}-data".encode())


def _crc32(path: Path) -> str:
    return f"{zlib.crc32(path.read_bytes()) & 0xFFFFFFFF:08X}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestFixtureImpersonationDetection:
    """Fixture 不能伪装为正式生产制品。"""

    def test_committed_info_is_current_fixture(self) -> None:
        from blc_portable.engine_pack.schema import ExternalMetadata

        raw = _INFO_PATH.read_bytes()
        assert raw.endswith(b"\n")
        metadata = ExternalMetadata.from_dict(json.loads(raw.decode("utf-8")))
        assert metadata.artifact_class == "fixture"

    def test_missing_artifact_class_rejected_by_schema(self) -> None:
        from blc_portable.engine_pack.schema import ExternalMetadata

        raw = external_metadata()
        raw.pop("artifact_class")
        with pytest.raises(ValueError, match="字段不符合当前格式"):
            ExternalMetadata.from_dict(raw)

    def test_invalid_artifact_class_rejected_by_schema(self) -> None:
        from blc_portable.engine_pack.schema import ExternalMetadata

        metadata = ExternalMetadata.from_dict(external_metadata(artifact_class="fake"))
        assert any("artifact_class" in error for error in metadata.validate())

    def test_old_metadata_field_rejected(self) -> None:
        from blc_portable.engine_pack.schema import ExternalMetadata

        raw = external_metadata()
        raw["schema_version"] = raw["format_version"]
        with pytest.raises(ValueError, match="字段不符合当前格式"):
            ExternalMetadata.from_dict(raw)

    def test_fixture_info_fails_production_check(self) -> None:
        from blc_portable.builders.lite import check_engine_pack_info

        with pytest.raises(RuntimeError, match="fixture|production|500"):
            check_engine_pack_info()

    def test_builder_rejects_tiny_production_archive(self) -> None:
        from blc_portable.engine_pack.builder import validate_production_metadata

        errors = validate_production_metadata("ABCDEF01", "a" * 64, "b" * 64, "c" * 64, "d" * 40, 1024)
        assert any("too small" in error for error in errors)


class TestEnginePackInfoFields:
    """外部元数据与内部内容清单使用唯一当前契约。"""

    def test_committed_info_is_exact_current_schema(self) -> None:
        from blc_portable.engine_pack.schema import SCHEMA_VERSION, ExternalMetadata

        raw = json.loads(_INFO_PATH.read_text(encoding="utf-8"))
        metadata = ExternalMetadata.from_dict(raw)
        assert metadata.format_version == SCHEMA_VERSION
        assert metadata.expected_engine_ids == _ENGINES
        assert metadata.crc32 == metadata.crc32.upper()

    def test_external_manifest_matches_packaged_content_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from blc_portable.engine_pack import builder
        from blc_portable.engine_pack.manifest import SOURCE_COMMIT_FULL

        dist_dir = tmp_path / "dist"
        resources_dir = tmp_path / "resources"
        monkeypatch.setattr(builder, "DIST_DIR", dist_dir)
        monkeypatch.setattr(builder, "RESOURCES_DIR", resources_dir)
        raw = current_manifest({"models/whisper/model.bin": {"size": 4, "sha256": "0" * 64}})
        source = tmp_path / "engine-pack-manifest.json"
        source.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        archive = tmp_path / f"BiliLiveCut-EnginePack-{builder.ENGINE_PACK_VERSION}.zip"
        archive.write_bytes(b"fixture")

        result = builder.write_output_files(
            crc32_val="1234ABCD",
            sha256_val="a" * 64,
            archive_path=archive,
            source_commit=SOURCE_COMMIT_FULL,
            content_manifest_path=source,
            is_fixture=True,
        )

        assert (dist_dir / "engine-pack-manifest.json").read_bytes() == source.read_bytes()
        assert (dist_dir / "engine-pack-info.json").read_bytes() == (
            resources_dir / "engine_pack_info.json"
        ).read_bytes()
        assert result["file_count"] == 1

    def test_content_manifest_uses_installer_contract(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.manifest import MANIFEST_FORMAT_VERSION, load_manifest

        path = tmp_path / "engine-pack-manifest.json"
        path.write_text(json.dumps(current_manifest()), encoding="utf-8")
        manifest = load_manifest(path)
        assert manifest.format_version == MANIFEST_FORMAT_VERSION
        assert "archive_crc32" not in manifest.to_dict()

    def test_committed_info_matches_current_model_lock(self) -> None:
        from blc_portable.model_lock import compute_model_lock_sha256

        info = json.loads(_INFO_PATH.read_text(encoding="utf-8"))
        assert info["model_lock_sha256"] == compute_model_lock_sha256(
            _portable_dir / "config" / "model_sources.lock.json"
        )


class TestZipCorruptionDetection:
    """ZIP 摘要和解压目录的逐文件校验。"""

    def test_archive_digest_mismatch(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_archive_metadata

        archive = tmp_path / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("test.txt", "hello")
        assert verify_archive_metadata(archive, _crc32(archive), _sha256(archive)) == []
        assert any("CRC32" in error for error in verify_archive_metadata(archive, "FFFFFFFF", _sha256(archive)))
        assert any("SHA-256" in error for error in verify_archive_metadata(archive, _crc32(archive), "b" * 64))

    def test_single_file_corruption_detected(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_extracted_tree

        _make_models(tmp_path)
        manifest = current_tree_manifest(tmp_path)
        assert verify_extracted_tree(tmp_path, manifest) == []
        (tmp_path / "models" / "whisper" / "model.bin").write_bytes(b"tampered")
        assert any("SHA-256" in error for error in verify_extracted_tree(tmp_path, manifest))

    def test_missing_and_extra_file_detected(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.verifier import verify_extracted_tree

        _make_models(tmp_path)
        manifest = current_tree_manifest(tmp_path)
        (tmp_path / "models" / "whisper" / "model.bin").unlink()
        assert any("缺失文件" in error for error in verify_extracted_tree(tmp_path, manifest))
        (tmp_path / "models" / "whisper" / "rogue.dll").write_bytes(b"evil")
        assert any("多余文件" in error for error in verify_extracted_tree(tmp_path, manifest))


class TestFullRehash:
    """当前已安装清单支持快速检查和完整重哈希。"""

    def _prepare(self, tmp_path: Path) -> tuple[Path, dict[str, object]]:
        models_dir = tmp_path / "models"
        _make_models(tmp_path)
        manifest = installed_manifest(models_dir)
        (models_dir / "engine-pack-installed.json").write_text(json.dumps(manifest), encoding="utf-8")
        return models_dir, manifest

    def test_full_rehash_missing_file(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.installer import check_installed_models

        models_dir, _ = self._prepare(tmp_path)
        (models_dir / "whisper" / "model.bin").unlink()
        ok, errors = check_installed_models(models_dir, full_rehash=True)
        assert not ok, errors
        assert any("missing" in error.lower() or "empty" in error.lower() for error in errors), errors

    def test_full_rehash_sha_mismatch(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.installer import check_installed_models

        models_dir, _ = self._prepare(tmp_path)
        (models_dir / "whisper" / "model.bin").write_bytes(b"tampered")
        ok, errors = check_installed_models(models_dir, full_rehash=True)
        assert not ok and any("SHA-256" in error for error in errors)

    def test_clean_and_quick_checks(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.installer import check_installed_models

        models_dir, manifest = self._prepare(tmp_path)
        assert check_installed_models(models_dir, full_rehash=True)[0]
        manifest["engines"]["whisper"]["files"]["model.bin"]["sha256"] = "0" * 64  # type: ignore[index]
        (models_dir / "engine-pack-installed.json").write_text(json.dumps(manifest), encoding="utf-8")
        assert check_installed_models(models_dir, full_rehash=False)[0]

    def test_old_installed_manifest_is_rejected_without_rewrite(self, tmp_path: Path) -> None:
        """旧已安装清单不得迁移为当前 schema，也不得被原地改写。"""
        from blc_portable.engine_pack.installer import check_installed_models

        models_dir, manifest = self._prepare(tmp_path)
        manifest["schema_version"] = 5
        manifest_path = models_dir / "engine-pack-installed.json"
        original = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        manifest_path.write_text(original, encoding="utf-8")

        ok, errors = check_installed_models(models_dir, full_rehash=True)

        assert ok is False
        assert any("schema unsupported: 5" in error for error in errors)
        assert manifest_path.read_text(encoding="utf-8") == original


class TestVerifierIntegration:
    """完整 Engine Pack 验证链。"""

    def test_verify_engine_pack_complete_passes(self, tmp_path: Path) -> None:
        from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION
        from blc_portable.engine_pack.verifier import verify_engine_pack_complete

        source = tmp_path / "source"
        source.mkdir()
        _make_models(source)
        manifest = current_tree_manifest(source)
        (source / "engine-pack-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        archive = tmp_path / "pack.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(source).as_posix())
        extracted = tmp_path / "extracted"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extracted)

        ok, errors = verify_engine_pack_complete(
            archive,
            extracted / "engine-pack-manifest.json",
            _crc32(archive),
            _sha256(archive),
            ENGINE_PACK_VERSION,
        )
        assert ok, errors


class TestProductionGates:
    """生产外部摘要必须完整。"""

    @pytest.mark.parametrize(
        ("field", "expected"),
        [("crc32", "CRC32"), ("sha256", "sha256"), ("model_lock_sha256", "model_lock_sha256")],
    )
    def test_empty_digest_blocked(self, field: str, expected: str) -> None:
        from blc_portable.engine_pack.schema import ExternalMetadata

        metadata = ExternalMetadata.from_dict(external_metadata(**{field: ""}))
        assert any(expected.lower() in error.lower() for error in metadata.validate())


class TestModelLockIntegrity:
    """模型锁记录当前版本所有可重现与许可证字段。"""

    def test_catalog_validation_passes(self) -> None:
        config_dir = str(_portable_dir / "config")
        if config_dir not in sys.path:
            sys.path.insert(0, config_dir)
        from model_catalog import load_engines, validate_catalog

        assert validate_catalog() == []
        for engine in load_engines():
            assert engine.resolved_revision
            assert engine.required_files
            assert engine.license.name
            assert isinstance(engine.license.redistribution_verified, bool)
