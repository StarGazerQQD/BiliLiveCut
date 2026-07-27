"""构建器测试 — Lite/Full EXE 构建约束。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_portable_dir = Path(__file__).resolve().parent.parent  # portable/
if str(_portable_dir / "src") not in sys.path:
    sys.path.insert(0, str(_portable_dir / "src"))


class TestLiteBuilder:
    def test_lite_has_release_version(self) -> None:
        from blc_portable.builders.lite import RELEASE_VERSION, build_exe  # noqa: E402

        assert RELEASE_VERSION == "0.1.15.2-alpha"
        assert callable(build_exe)

    def test_lite_rejects_everything_empty(self) -> None:
        from blc_portable.builders.lite import check_engine_pack_info  # noqa: E402

        info_path = _portable_dir / "resources" / "engine_pack_info.json"
        if info_path.exists():
            # Current engine_pack_info.json is a Fixture (4KB < 500MB)
            with pytest.raises(RuntimeError, match="too small"):
                check_engine_pack_info()
        else:
            with pytest.raises(RuntimeError):
                check_engine_pack_info()

    def test_lite_fixture_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from blc_portable.builders.lite import check_engine_pack_info  # noqa: E402

        monkeypatch.setenv("BLC_FIXTURE_BUILD", "1")
        # Should not raise
        check_engine_pack_info()

    def test_official_release_mode_is_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The no-pack release mode must be selected by an explicit CLI flag."""
        from blc_portable.builders import lite

        calls: list[bool] = []

        def fake_build_exe(*, without_engine_pack: bool = False) -> Path:
            calls.append(without_engine_pack)
            return Path("BiliLiveCut.exe")

        monkeypatch.setattr(lite, "build_exe", fake_build_exe)
        assert lite.main(["--without-engine-pack"]) == 0
        assert calls == [True]

    def test_lite_documented_commands_match_supported_modes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Lite usage documentation must not advertise rejected CLI flags."""
        from blc_portable.builders import lite

        calls: list[bool] = []

        def fake_build_exe(*, without_engine_pack: bool = False) -> Path:
            calls.append(without_engine_pack)
            return Path("BiliLiveCut.exe")

        monkeypatch.setattr(lite, "build_exe", fake_build_exe)
        documentation = lite.__doc__ or ""

        assert "--skip-payload" not in documentation
        assert "--without-engine-pack" in documentation
        assert lite.main([]) == 0
        assert lite.main(["--without-engine-pack"]) == 0
        assert calls == [False, True]

    def test_lite_version_in_manifest(self) -> None:
        from blc_portable.builders.lite import RELEASE_VERSION as LITE_VERSION  # noqa: E402
        from blc_portable.payload.manifest import RELEASE_VERSION as MANIFEST_VERSION  # noqa: E402

        assert LITE_VERSION == MANIFEST_VERSION

    def test_lite_bootstrap_wheels_are_hash_verified(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from blc_portable.builders import lite

        wheel = tmp_path / "fixture-1.0-py3-none-any.whl"
        wheel.write_bytes(b"audited wheel")
        config = tmp_path / "bootstrap-wheels.json"
        config.write_text(
            json.dumps(
                [
                    {
                        "wheel_filename": wheel.name,
                        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    }
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(lite, "BOOTSTRAP_WHEELS_CONFIG", config)

        lite.check_bootstrap_wheels(tmp_path)
        wheel.write_bytes(b"tampered")
        with pytest.raises(RuntimeError, match="hash mismatch"):
            lite.check_bootstrap_wheels(tmp_path)

    def test_pyinstaller_spec_embeds_bootstrap_wheels(self) -> None:
        spec = (_portable_dir / "specs" / "portable_launcher.spec").read_text(encoding="utf-8")

        assert '"bootstrap-wheels"' in spec
        assert "Lite bootstrap wheels are missing" in spec

    def test_pyinstaller_spec_embeds_project_license(self) -> None:
        """Lite EXE 必须内嵌仓库根目录的项目 LICENSE。"""
        spec = (_portable_dir / "specs" / "portable_launcher.spec").read_text(encoding="utf-8")

        assert '_project_license = _here.parent.parent / "LICENSE"' in spec
        assert '(str(_project_license), ".")' in spec

    def test_project_license_is_canonical_and_hashable(self) -> None:
        """Portable 构建器只接受带指定版权人的规范 MIT License。"""
        from blc_portable.project_license import PROJECT_LICENSE_ID, PROJECT_LICENSE_PATH, project_license_sha256

        assert PROJECT_LICENSE_ID == "MIT"
        assert "Copyright (c) 2026 StarGazerQQD" in PROJECT_LICENSE_PATH.read_text(encoding="utf-8")
        assert project_license_sha256() == hashlib.sha256(PROJECT_LICENSE_PATH.read_bytes()).hexdigest()

    def test_project_license_rejects_wrong_holder(self, tmp_path: Path) -> None:
        """错误版权声明不得进入 Portable 发布制品。"""
        from blc_portable.project_license import load_project_license

        invalid = tmp_path / "LICENSE"
        invalid.write_text(
            'MIT License\nCopyright (c) 2026 Someone Else\nPermission is hereby granted\nTHE SOFTWARE IS PROVIDED "AS IS"',
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="Project license is invalid"):
            load_project_license(invalid)


class TestFullBuilder:
    def test_full_has_release_version(self) -> None:
        from blc_portable.builders.full import RELEASE_VERSION  # noqa: E402

        assert RELEASE_VERSION == "0.1.15.2-alpha"

    def test_full_check_missing_components(self) -> None:
        """Full build without portable-python or wheels must raise RuntimeError."""
        from blc_portable.builders.full import build_full_bundle  # noqa: E402

        # No portable-python, no wheels, no ffmpeg -> must fail
        with pytest.raises(RuntimeError):
            build_full_bundle()

    def test_full_fixture_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLC_CI_BUILD", "1")  # Legacy support
        monkeypatch.setenv("BLC_FIXTURE_BUILD", "1")
        # Should not crash at import time
        from blc_portable.builders.full import build_full_bundle  # noqa: E402

        assert callable(build_full_bundle)

    def test_full_archive_hashes_are_streamed(self, tmp_path: Path) -> None:
        """Full 制品必须同时生成可复核的 SHA-256 与 CRC32。"""
        from blc_portable.builders.full import _compute_archive_hashes

        artifact = tmp_path / "artifact.zip"
        artifact.write_bytes(b"BiliLiveCut Portable Full\n" * 10_000)

        sha256, crc32 = _compute_archive_hashes(artifact)

        import hashlib
        import zlib

        content = artifact.read_bytes()
        assert sha256 == hashlib.sha256(content).hexdigest()
        assert crc32 == f"{zlib.crc32(content) & 0xFFFFFFFF:08X}"

    def test_full_omits_fixture_engine_pack_crc32(self, tmp_path: Path) -> None:
        """Full 发布清单不能引用测试用 Engine Pack 的 CRC32。"""
        import json

        from blc_portable.builders.full import _load_production_engine_pack_crc32

        info_path = tmp_path / "engine-pack-info.json"
        info_path.write_text(
            json.dumps({"artifact_class": "fixture", "crc32": "1234ABCD"}),
            encoding="utf-8",
        )

        assert _load_production_engine_pack_crc32(info_path) == ""

    def test_full_loads_production_engine_pack_crc32(self, tmp_path: Path) -> None:
        """Full 发布清单应保留生产 Engine Pack 的规范化 CRC32。"""
        import json

        from blc_portable.builders.full import _load_production_engine_pack_crc32

        info_path = tmp_path / "engine-pack-info.json"
        info_path.write_text(
            json.dumps({"artifact_class": "production", "crc32": "ffd3a024"}),
            encoding="utf-8",
        )

        assert _load_production_engine_pack_crc32(info_path) == "FFD3A024"

    def test_full_rejects_production_engine_pack_without_crc32(self, tmp_path: Path) -> None:
        """生产 Engine Pack 元数据缺少 CRC32 时必须阻止构建。"""
        import json

        from blc_portable.builders.full import _load_production_engine_pack_crc32

        info_path = tmp_path / "engine-pack-info.json"
        info_path.write_text(json.dumps({"artifact_class": "production"}), encoding="utf-8")

        with pytest.raises(RuntimeError, match="missing crc32"):
            _load_production_engine_pack_crc32(info_path)
