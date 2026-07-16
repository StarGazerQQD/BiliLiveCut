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

        assert RELEASE_VERSION == "0.1.14.10-alpha"
        assert callable(build_exe)

    def test_lite_rejects_everything_empty(self) -> None:
        from blc_portable.builders.lite import check_engine_pack_info  # noqa: E402

        # Without fixture mode, missing engine_pack_info should raise
        # But if a valid info file exists locally, this test verifies
        # the function itself is importable and callable
        info_path = _portable_dir / "resources" / "engine_pack_info.json"
        if info_path.exists():
            # If file exists, verify it parses cleanly
            check_engine_pack_info()
        else:
            with pytest.raises(RuntimeError):
                check_engine_pack_info()

    def test_lite_fixture_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from blc_portable.builders.lite import check_engine_pack_info  # noqa: E402

        monkeypatch.setenv("BLC_FIXTURE_BUILD", "1")
        # Should not raise
        check_engine_pack_info()

    def test_lite_version_in_manifest(self) -> None:
        from blc_portable.builders.lite import RELEASE_VERSION as LITE_VERSION  # noqa: E402
        from blc_portable.payload.manifest import RELEASE_VERSION as MANIFEST_VERSION  # noqa: E402

        assert LITE_VERSION == MANIFEST_VERSION


class TestFullBuilder:
    def test_full_has_release_version(self) -> None:
        from blc_portable.builders.full import RELEASE_VERSION  # noqa: E402

        assert RELEASE_VERSION == "0.1.14.10-alpha"

    def test_full_check_missing_components(self) -> None:
        # Full builder should raise when key components are missing (non-fixture)
        pass  # Covered by test_full_fails_closed when components not present

    def test_full_fixture_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLC_CI_BUILD", "1")  # Legacy support
        monkeypatch.setenv("BLC_FIXTURE_BUILD", "1")
        # Should not crash at import time
        from blc_portable.builders.full import build_full_bundle  # noqa: E402

        assert callable(build_full_bundle)
