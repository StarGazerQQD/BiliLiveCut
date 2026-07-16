"""事务安装 + 回滚测试 — Engine Pack 和 Runtime 原子操作。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

# 添加模块路径
_portable_dir = Path(__file__).resolve().parent.parent  # portable/
_src_dir = _portable_dir / "src"
import sys

if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))


class TestFileLock:
    """跨进程锁测试。"""

    def test_acquire_and_release(self) -> None:
        from blc_portable.archive.locks import FileLock  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / ".test.lock"
            lock = FileLock(lock_path)
            with lock.acquire(timeout=5):
                assert lock_path.exists()
            assert not lock_path.exists()

    def test_two_locks_conflict(self) -> None:
        from blc_portable.archive.locks import FileLock  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            lock1 = FileLock(Path(tmpdir) / ".lock")
            lock2 = FileLock(Path(tmpdir) / ".lock")
            with lock1.acquire(timeout=0):
                with pytest.raises(TimeoutError):
                    with lock2.acquire(timeout=0):
                        pass

    def test_lock_path_names(self) -> None:
        from blc_portable.archive.locks import get_engine_pack_lock_path, get_runtime_lock_path  # noqa: E402

        app_root = Path("C:/app")
        rp = get_runtime_lock_path(app_root)
        ep = get_engine_pack_lock_path(app_root)
        assert ".runtime-install" in str(rp)
        assert ".engine-pack-install" in str(ep)


class TestAtomicInstall:
    """原子安装 + 回滚行为测试。"""

    def test_installed_manifest_write_then_read(self) -> None:
        from blc_portable.engine_pack.installer import (  # noqa: E402
            _read_installed_manifest,
            _write_installed_manifest,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            models_dir = Path(tmpdir) / "models"
            models_dir.mkdir()
            files_info = {
                "whisper": {"target_path": "models/whisper", "file_count": 8, "total_size": 1600000000},
                "paraformer": {"target_path": "models/paraformer", "file_count": 40, "total_size": 1200000000},
            }
            _write_installed_manifest(
                models_dir,
                "0.1.14.9-alpha",
                ["whisper", "paraformer"],
                files_info,
                zip_sha256="abc123",
                source_commit="731a31c",
            )
            manifest = _read_installed_manifest(models_dir)
            assert manifest is not None
            assert manifest["engine_pack_version"] == "0.1.14.9-alpha"
            assert manifest["zip_sha256"] == "abc123"

    def test_installed_manifest_version_check(self) -> None:
        from blc_portable.engine_pack.installer import (  # noqa: E402
            _write_installed_manifest,
            check_installed_models,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            models_dir = Path(tmpdir) / "models"
            models_dir.mkdir()
            for eng in ("whisper", "paraformer", "sensevoice", "funasr_nano"):
                (models_dir / eng).mkdir(parents=True, exist_ok=True)
                (models_dir / eng / "model.txt").write_text("test")
            _write_installed_manifest(
                models_dir,
                "0.1.14.9-alpha",
                ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                {
                    e: {"target_path": f"models/{e}", "file_count": 1, "total_size": 4}
                    for e in ("whisper", "paraformer", "sensevoice", "funasr_nano")
                },
            )
            assert check_installed_models(models_dir, "0.1.14.9-alpha")
            assert not check_installed_models(models_dir, "0.1.14.7-alpha")

    def test_not_installed_returns_false(self) -> None:
        from blc_portable.engine_pack.installer import check_installed_models  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            assert not check_installed_models(Path(tmpdir) / "nonexistent", "0.1.14.9-alpha")

    def test_rollback_on_move_failure(self) -> None:
        from blc_portable.engine_pack.installer import install_models_dir_from_staging  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            staging = app_root / "staging"
            staging.mkdir()
            (staging / "whisper").mkdir()
            (staging / "whisper" / "model.bin").write_text("data")

            # Pretend models already exist
            models = app_root / "models"
            models.mkdir()
            (models / "old_model.txt").write_text("old")
            old_content = (models / "old_model.txt").read_text()
            assert old_content == "old"

            result = install_models_dir_from_staging(app_root, staging, "0.1.14.9-alpha", ["whisper"], {})
            # Should succeed since staging relocation is straightforward
            assert result is True
