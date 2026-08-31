"""事务安装 + 回滚测试 — Engine Pack 和 Runtime 原子操作。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pytest import MonkeyPatch

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
            install_engine_from_staging,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            models_dir = app_root / "models"
            for engine_id in ("whisper", "paraformer"):
                engine_dir = app_root / "staging" / engine_id
                engine_dir.mkdir(parents=True)
                (engine_dir / "model.bin").write_bytes(engine_id.encode())
                install_engine_from_staging(
                    app_root,
                    engine_id,
                    engine_dir,
                    installation_source="engine_pack",
                )
            manifest = _read_installed_manifest(models_dir)
            assert manifest is not None
            assert manifest["schema_version"] == 6
            assert manifest["engines"]["whisper"]["installation_source"] == "engine_pack"
            assert manifest["engines"]["whisper"]["zip_sha256"] is None
            assert "engine_pack_version" not in manifest
            assert "source_commit" not in manifest

    def test_installed_manifest_uses_only_content_identity(self) -> None:
        from blc_portable.engine_pack.installer import (  # noqa: E402
            check_installed_models,
            install_engine_from_staging,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            models_dir = app_root / "models"
            for eng in ("whisper", "paraformer", "sensevoice", "funasr_nano"):
                staged = app_root / "staging" / eng
                staged.mkdir(parents=True, exist_ok=True)
                (staged / "model.txt").write_text("test")
                install_engine_from_staging(app_root, eng, staged)
            ok, _ = check_installed_models(models_dir)
            assert ok

    def test_not_installed_returns_false(self) -> None:
        from blc_portable.engine_pack.installer import check_installed_models  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            ok, _ = check_installed_models(Path(tmpdir) / "nonexistent")
            assert not ok

    def test_rollback_on_manifest_failure(self, monkeypatch: MonkeyPatch) -> None:
        from blc_portable.engine_pack import installer  # noqa: E402

        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            source = app_root / "staging" / "whisper"
            source.mkdir(parents=True)
            (source / "model.bin").write_text("new")
            target = app_root / "models" / "whisper"
            target.mkdir(parents=True)
            (target / "model.bin").write_text("old")

            def fail_record(*_args: object, **_kwargs: object) -> dict[str, object]:
                raise RuntimeError("injected manifest failure")

            monkeypatch.setattr(installer, "_new_engine_record", fail_record)

            with pytest.raises(RuntimeError, match="injected manifest failure"):
                installer.install_engine_from_staging(app_root, "whisper", source)

            assert (target / "model.bin").read_text() == "old"
            assert (source / "model.bin").read_text() == "new"
