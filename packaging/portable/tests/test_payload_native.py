"""Windows Payload 原生模块构建契约测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest

_PORTABLE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PORTABLE_ROOT / "src"))

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _prepare_builder(monkeypatch: MonkeyPatch, tmp_path: Path) -> tuple[ModuleType, Path]:
    """将 Payload builder 指向隔离的伪仓库。"""
    from blc_portable.payload import builder

    portable_root = tmp_path / "packaging" / "portable"
    analysis_dir = tmp_path / "app" / "analysis"
    portable_root.mkdir(parents=True)
    analysis_dir.mkdir(parents=True)

    monkeypatch.setattr(builder, "PORTABLE_ROOT", portable_root)
    monkeypatch.setattr(builder.sys, "platform", "win32")
    monkeypatch.setattr(builder, "_windows_extension_suffix", lambda: ".cp312-win_amd64.pyd")
    monkeypatch.setattr(builder.importlib.util, "find_spec", lambda _name: object())
    return builder, analysis_dir


def test_payload_native_build_rejects_non_windows_host(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """固定为 win_x64 的 Payload 不得在其他平台生成。"""
    from blc_portable.payload import builder

    monkeypatch.setattr(builder.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="必须在 Windows 构建"):
        builder._compile_and_copy_native_modules(tmp_path / "staging")


def test_payload_native_build_rejects_false_success(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """构建命令返回零但目标文件缺失时必须失败。"""
    builder, _analysis_dir = _prepare_builder(monkeypatch, tmp_path)

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="缺少必需原生模块：c, cython, rust"):
        builder._compile_and_copy_native_modules(tmp_path / "staging")


def test_payload_native_build_requires_rust(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """Release 契约要求 Rust 时，构建器不得把 Rust 编译失败当作成功。"""
    builder, analysis_dir = _prepare_builder(monkeypatch, tmp_path)

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        script_name = Path(command[1]).name
        if script_name == "setup_c.py":
            (analysis_dir / "_c_speedups.cp312-win_amd64.pyd").write_bytes(b"c")
        elif script_name == "setup.py":
            (analysis_dir / "_speedups_round2.cp312-win_amd64.pyd").write_bytes(b"cython")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="缺少必需原生模块：rust"):
        builder._compile_and_copy_native_modules(tmp_path / "staging")


def test_payload_native_build_copies_only_current_windows_abi(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """只复制当前 ABI 的 Windows 模块，排除旧 ABI 与 Linux 产物。"""
    builder, analysis_dir = _prepare_builder(monkeypatch, tmp_path)
    expected = {
        "setup_c.py": analysis_dir / "_c_speedups.cp312-win_amd64.pyd",
        "setup.py": analysis_dir / "_speedups_round2.cp312-win_amd64.pyd",
        "build_rust.py": analysis_dir / "_rust_cluster.pyd",
    }
    (analysis_dir / "_speedups_round2.cp314-win_amd64.pyd").write_bytes(b"old")
    (analysis_dir / "_c_speedups.cpython-312-x86_64-linux-gnu.so").write_bytes(b"foreign")

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        script_name = Path(command[1]).name
        if script_name in {"setup.py", "setup_c.py"}:
            assert command[-2:] == ["--inplace", "--force"]
        expected[script_name].write_bytes(script_name.encode("ascii"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    staging_dir = tmp_path / "staging"
    result = builder._compile_and_copy_native_modules(staging_dir)

    assert result == {"c": True, "cython": True, "rust": True}
    copied = {path.name for path in (staging_dir / "app" / "analysis").iterdir()}
    assert copied == {
        "_c_speedups.cp312-win_amd64.pyd",
        "_speedups_round2.cp312-win_amd64.pyd",
        "_rust_cluster.pyd",
    }
