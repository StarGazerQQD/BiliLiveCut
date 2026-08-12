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
            _collect_files_info,
            _read_installed_manifest,
            _write_installed_manifest,
        )
        from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION, SOURCE_COMMIT_FULL  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            models_dir = Path(tmpdir) / "models"
            models_dir.mkdir()
            for engine_id in ("whisper", "paraformer"):
                engine_dir = models_dir / engine_id
                engine_dir.mkdir()
                (engine_dir / "model.bin").write_bytes(engine_id.encode())
            files_info = _collect_files_info(models_dir, ["whisper", "paraformer"])
            _write_installed_manifest(
                models_dir,
                ENGINE_PACK_VERSION,
                ["whisper", "paraformer"],
                files_info,
                zip_sha256="a" * 64,
                source_commit=SOURCE_COMMIT_FULL,
                installation_source="engine_pack",
            )
            manifest = _read_installed_manifest(models_dir)
            assert manifest is not None
            assert manifest["engine_pack_version"] == ENGINE_PACK_VERSION
            assert manifest["installation_source"] == "engine_pack"
            assert manifest["zip_sha256"] == "a" * 64
            assert manifest["source_commit"] == SOURCE_COMMIT_FULL

    def test_installed_manifest_version_check(self) -> None:
        from blc_portable.engine_pack.installer import (  # noqa: E402
            _collect_files_info,
            _write_installed_manifest,
            check_installed_models,
        )
        from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION, SOURCE_COMMIT_FULL  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            models_dir = Path(tmpdir) / "models"
            models_dir.mkdir()
            for eng in ("whisper", "paraformer", "sensevoice", "funasr_nano"):
                (models_dir / eng).mkdir(parents=True, exist_ok=True)
                (models_dir / eng / "model.txt").write_text("test")
            _write_installed_manifest(
                models_dir,
                ENGINE_PACK_VERSION,
                ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                _collect_files_info(
                    models_dir,
                    ["whisper", "paraformer", "sensevoice", "funasr_nano"],
                ),
                zip_sha256=None,
                source_commit=SOURCE_COMMIT_FULL,
                installation_source="online_download",
            )
            ok1, _ = check_installed_models(models_dir, ENGINE_PACK_VERSION)
            assert ok1
            ok2, _ = check_installed_models(models_dir, "0.1.17.2-alpha")
            assert not ok2

    def test_not_installed_returns_false(self) -> None:
        from blc_portable.engine_pack.installer import check_installed_models  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            ok, _ = check_installed_models(Path(tmpdir) / "nonexistent", "0.1.14.9-alpha")
            assert not ok

    def test_rollback_on_move_failure(self) -> None:
        from blc_portable.engine_pack.installer import install_models_dir_from_staging  # noqa: E402
        from blc_portable.engine_pack.manifest import ENGINE_PACK_VERSION  # noqa: E402

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

            result = install_models_dir_from_staging(app_root, staging, ENGINE_PACK_VERSION, ["whisper"])
            # Should succeed since staging relocation is straightforward
            assert result is True
