"""构建器测试 — Lite/Full EXE 构建约束。"""

from __future__ import annotations

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

    def test_lite_version_in_manifest(self) -> None:
        from blc_portable.builders.lite import RELEASE_VERSION as LITE_VERSION  # noqa: E402
        from blc_portable.payload.manifest import RELEASE_VERSION as MANIFEST_VERSION  # noqa: E402

        assert LITE_VERSION == MANIFEST_VERSION


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
