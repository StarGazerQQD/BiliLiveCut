"""ZIP 安全测试 — 验证 safe_zip 模块的 Zip Slip/炸弹防御。"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

import pytest

# 添加模块路径
_portable_dir = Path(__file__).resolve().parent.parent  # portable/
_src_dir = _portable_dir / "src"
import sys

if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from blc_portable.archive.safe_zip import (  # noqa: E402
    _safe_relative_path,
    _is_reserved_name,
    _default_compression,
    safe_extract,
    iter_archive_members,
)


class TestZipSlip:
    """Zip Slip 防御测试。"""

    def test_absolute_path(self) -> None:
        with pytest.raises(RuntimeError, match="绝对路径"):
            _safe_relative_path(Path("/tmp/root"), "/etc/passwd")

    def test_unix_absolute(self) -> None:
        with pytest.raises(RuntimeError, match="绝对路径"):
            _safe_relative_path(Path("/tmp/root"), "/home/user/file.txt")

    def test_windows_drive_path(self) -> None:
        with pytest.raises(RuntimeError, match="盘符"):
            _safe_relative_path(Path("C:\\tmp"), "D:\\evil.exe")

    def test_parent_traversal(self) -> None:
        with pytest.raises(RuntimeError, match="路径遍历"):
            _safe_relative_path(Path("/tmp/root"), "../etc/passwd")

    def test_deep_traversal(self) -> None:
        with pytest.raises(RuntimeError, match="路径遍历"):
            _safe_relative_path(Path("/tmp/root"), "a/../../../../etc/passwd")

    def test_reserved_con(self) -> None:
        with pytest.raises(RuntimeError, match="保留设备名"):
            _safe_relative_path(Path("/tmp/root"), "CON/test.txt")

    def test_reserved_nul(self) -> None:
        with pytest.raises(RuntimeError, match="保留设备名"):
            _safe_relative_path(Path("/tmp/root"), "NUL")

    def test_reserved_com1(self) -> None:
        with pytest.raises(RuntimeError, match="保留设备名"):
            _safe_relative_path(Path("/tmp/root"), "COM1")

    def test_valid_path(self) -> None:
        result = _safe_relative_path(Path("/tmp/root"), "models/whisper/model.bin")
        assert str(result).endswith("model.bin")

    def test_normal_nested(self) -> None:
        result = _safe_relative_path(Path("/tmp/root"), "app/cli.py")
        assert "app" in str(result)


class TestIsReservedName:
    """保留设备名测试。"""

    def test_con(self) -> None:
        assert _is_reserved_name("CON")

    def test_nul(self) -> None:
        assert _is_reserved_name("NUL")

    def test_prn(self) -> None:
        assert _is_reserved_name("PRN")

    def test_com9(self) -> None:
        assert _is_reserved_name("COM9")

    def test_lpt1(self) -> None:
        assert _is_reserved_name("LPT1")

    def test_normal_file(self) -> None:
        assert not _is_reserved_name("model.bin")

    def test_case_insensitive(self) -> None:
        assert _is_reserved_name("con")
        assert _is_reserved_name("nul")


class TestCompressionChoice:
    """压缩策略测试。"""

    def test_model_bin_stored(self) -> None:
        assert _default_compression("model.bin") == zipfile.ZIP_STORED

    def test_pytorch_stored(self) -> None:
        assert _default_compression("model.pt") == zipfile.ZIP_STORED

    def test_json_deflated(self) -> None:
        assert _default_compression("manifest.json") == zipfile.ZIP_DEFLATED

    def test_txt_deflated(self) -> None:
        assert _default_compression("README.txt") == zipfile.ZIP_DEFLATED

    def test_pyd_stored(self) -> None:
        assert _default_compression("_speedups.pyd") == zipfile.ZIP_STORED

    def test_whl_stored(self) -> None:
        assert _default_compression("package-1.0-py3-none-any.whl") == zipfile.ZIP_STORED


class TestZipExtractCount:
    """ZIP 解压数量和炸弹检测。"""

    def test_normal_extract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "test.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("a.txt", "hello")
                zf.writestr("b.txt", "world")

            dest = Path(tmpdir) / "out"
            with zipfile.ZipFile(zip_path) as zf:
                extracted = safe_extract(zf, dest)
            assert len(extracted) == 2

    def test_rejects_symlink(self) -> None:
        """ZIP 中的符号链接应被拒绝。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            info = zipfile.ZipInfo("link.txt")
            info.external_attr = 0o120000 << 16  # symlink mode
            zip_path = Path(tmpdir) / "symlink.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr(info, "target")

            dest = Path(tmpdir) / "out"
            with zipfile.ZipFile(zip_path) as zf, pytest.raises(RuntimeError, match="符号链接"):
                safe_extract(zf, dest)


class TestZipBombCount:
    """压缩炸弹文件数检测。"""

    def test_too_many_files(self) -> None:
        """超过文件数上限应拒绝。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "bomb.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for i in range(1100):
                    zf.writestr(f"file_{i}.txt", "x")

            dest = Path(tmpdir) / "out"
            with zipfile.ZipFile(zip_path) as zf:
                with pytest.raises(RuntimeError, match="文件数"):
                    safe_extract(zf, dest, max_file_count=1000)
