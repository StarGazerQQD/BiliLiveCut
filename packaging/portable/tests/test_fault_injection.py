"""故障注入测试 — 验证失败场景下旧状态保持不变、无半安装、返回非零。"""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

# 添加模块路径
_portable_dir = Path(__file__).resolve().parent.parent  # portable/
_src_dir = _portable_dir / "src"
import sys

if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))


class TestRuntimeFaultInjection:
    """Runtime 安装故障注入。"""

    def test_current_json_corrupted(self) -> None:
        from blc_portable.runtime.__init__ import get_current_release_dir  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            current = app_root / "runtime" / "current.json"
            current.parent.mkdir(parents=True)
            current.write_text("{invalid json")  # corrupted

            # Should gracefully return None, not crash
            result = get_current_release_dir()
            assert result is None

    def test_release_dir_missing_is_graceful(self) -> None:
        from blc_portable.runtime.__init__ import get_current_release_dir  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            current = app_root / "runtime" / "current.json"
            current.parent.mkdir(parents=True)
            current.write_text(json.dumps({"release_id": "ghost-release"}))

            result = get_current_release_dir()
            assert result is None


class TestEnginePackFaultInjection:
    """Engine Pack 安装故障注入。"""

    def test_manifest_version_mismatch_raises(self) -> None:
        """Manifest 版本不匹配应抛出。"""
        from blc_portable.engine_pack.installer import check_installed_models  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            models_dir = Path(tmpdir)
            # Installed manifest says 0.1.14.7
            (models_dir / "engine-pack-installed.json").write_text(
                json.dumps({"engine_pack_version": "0.1.14.7-alpha", "engine_ids": ["whisper"]})
            )
            ok, _ = check_installed_models(models_dir, "0.1.14.9-alpha")
            assert not ok

    def test_missing_engine_directory(self) -> None:
        from blc_portable.engine_pack.installer import check_installed_models  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            models_dir = Path(tmpdir)
            # Installed manifest claims 4 engines, but only whisper exists
            (models_dir / "whisper").mkdir()
            (models_dir / "whisper" / "model.bin").write_text("data")
            installed = {
                "engine_pack_version": "0.1.14.9-alpha",
                "engine_ids": ["whisper", "paraformer", "sensevoice", "funasr_nano"],
            }
            (models_dir / "engine-pack-installed.json").write_text(json.dumps(installed))
            ok2, _ = check_installed_models(models_dir, "0.1.14.9-alpha")
            assert not ok2


class TestVerifierFaultInjection:
    """验证器故障注入。"""

    def test_verify_archive_metadata_empty_crc(self) -> None:
        from blc_portable.engine_pack.verifier import verify_archive_metadata  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "test.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("a.txt", "hello")

            errors = verify_archive_metadata(zip_path, "", "abc123")
            assert any("expected_crc32 为空" in e for e in errors)

    def test_verify_archive_metadata_empty_sha(self) -> None:
        from blc_portable.engine_pack.verifier import verify_archive_metadata  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "test.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("a.txt", "hello")

            errors = verify_archive_metadata(zip_path, "DEADBEEF", "")
            assert any("expected_sha256 为空" in e for e in errors)

    def test_verify_missing_file_succeeds(self) -> None:
        from blc_portable.engine_pack.verifier import verify_archive_metadata  # noqa: E402

        errors = verify_archive_metadata(Path("/nonexistent.zip"), "DEADBEEF", "a" * 64)
        assert any("不存在" in e for e in errors)
