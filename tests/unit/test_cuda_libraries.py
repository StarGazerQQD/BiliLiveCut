from __future__ import annotations

import ctypes
import os
import sys
from importlib.metadata import PackageNotFoundError, PackagePath
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import cuda_libraries


class PropertyQuery:
    def __call__(self, field: int, pointer: object) -> int:
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int)).contents.value = (12, 9, 2)[field]
        return 0


class CudaBoundary:
    """仅模拟操作系统 DLL 加载、进程映射和已安装包元数据。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.packages: dict[str, Path] = {}
        self.loaded: set[Path] = set()
        self.calls: list[Path] = []

    def package(self, name: str) -> Path:
        path = (
            self.root / name / "lib" / ("nvrtc64_120_0.dll" if name == "nvidia-cuda-nvrtc-cu12" else "cublas64_12.dll")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"OS boundary fixture")
        if name == "nvidia-cublas-cu12":
            self.package("nvidia-cuda-nvrtc-cu12")
        else:
            path.with_name("nvrtc64_120_0.dll").write_bytes(b"OS boundary fixture")
            path.with_name("nvrtc-builtins64_129.dll").write_bytes(b"OS boundary fixture")
        if name != "nvidia-cuda-nvrtc-cu12":
            path.with_name("cublasLt64_12.dll").write_bytes(b"OS boundary fixture")
        self.packages[name] = path
        return path

    def distribution(self, name: str) -> SimpleNamespace:
        if name not in self.packages:
            raise PackageNotFoundError(name)
        path = self.packages[name]
        return SimpleNamespace(
            files=[PackagePath(file.name) for file in path.parent.glob("*.dll")],
            locate_file=lambda file: path.parent / str(file),
        )

    def load(self, path: str, *, winmode: int) -> SimpleNamespace:
        assert winmode == 0x00001100
        target = Path(path).resolve()
        if not target.is_file():
            raise OSError("DLL missing")
        self.calls.append(target)
        self.loaded.add(target)
        return SimpleNamespace(cublasGetProperty=PropertyQuery())


def install_boundary(root: Path, monkeypatch: pytest.MonkeyPatch) -> CudaBoundary:
    """子进程预检也复用相同外部边界，不替换真实 DLL 选择与校验逻辑。"""
    boundary = CudaBoundary(root)
    monkeypatch.setattr(
        cuda_libraries, "os", SimpleNamespace(name="nt", environ=os.environ, path=os.path, pathsep=os.pathsep)
    )
    monkeypatch.setattr(cuda_libraries, "distribution", boundary.distribution)
    monkeypatch.setattr(cuda_libraries, "_handles", {})
    monkeypatch.setattr(sys, "prefix", str(root))
    monkeypatch.setattr(ctypes, "WinDLL", boundary.load, raising=False)
    monkeypatch.setattr(
        cuda_libraries.psutil,
        "Process",
        lambda: SimpleNamespace(memory_maps=lambda: [SimpleNamespace(path=str(path)) for path in boundary.loaded]),
    )
    return boundary


@pytest.fixture
def dll_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CudaBoundary:
    return install_boundary(tmp_path, monkeypatch)


def test_cu130_loads_official_cublas12_without_changing_cublas13(dll_boundary: CudaBoundary) -> None:
    selected = dll_boundary.package("nvidia-cublas-cu12")
    dll_boundary.loaded.add(dll_boundary.root / "torch/lib/cublas64_13.dll")
    result = cuda_libraries.prepare_cublas(managed_only=True)
    assert result is not None and result.path == str(selected) and result.source == "nvidia-cublas-cu12"
    assert result.version == "12.9.2"
    assert dll_boundary.calls[-2:] == [selected.with_name("cublasLt64_12.dll"), selected]
    assert result.nvrtc_path == str(dll_boundary.packages["nvidia-cuda-nvrtc-cu12"])
    cuda_libraries.prepare_cublas(managed_only=True)
    assert len(dll_boundary.calls) == 4


@pytest.mark.parametrize("already_loaded", [False, True])
def test_cu126_reuses_same_torch_library_in_either_import_order(
    dll_boundary: CudaBoundary, already_loaded: bool
) -> None:
    selected = dll_boundary.package("torch")
    other = dll_boundary.package("nvidia-cublas-cu12")
    if already_loaded:
        dll_boundary.loaded.update((selected, selected.with_name("cublasLt64_12.dll")))
    result = cuda_libraries.prepare_cublas(managed_only=True)
    assert result is not None and result.path == str(selected) and result.source == "torch"
    assert other not in dll_boundary.loaded
    dll_boundary.loaded.add(selected)
    assert cuda_libraries.prepare_cublas(managed_only=True) == result


def test_duplicate_loaded_libraries_are_rejected_before_loading(dll_boundary: CudaBoundary) -> None:
    dll_boundary.loaded.update((dll_boundary.package("torch"), dll_boundary.package("nvidia-cublas-cu12")))
    with pytest.raises(RuntimeError, match="多个不同来源"):
        cuda_libraries.prepare_cublas(managed_only=True)
    assert dll_boundary.calls == []


def test_conflicting_companion_library_is_rejected(dll_boundary: CudaBoundary) -> None:
    dll_boundary.package("torch")
    other = dll_boundary.package("nvidia-cublas-cu12")
    dll_boundary.loaded.add(other.with_name("cublasLt64_12.dll"))
    with pytest.raises(RuntimeError, match="不同目录"):
        cuda_libraries.prepare_cublas(managed_only=True)


def test_source_system_cuda_is_preserved_but_managed_preflight_rejects_it(
    dll_boundary: CudaBoundary, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = dll_boundary.package("system")
    monkeypatch.setenv("CUDA_PATH", str(path.parent.parent))
    monkeypatch.setenv("PATH", str(path.parent))
    result = cuda_libraries.prepare_cublas()
    assert result is not None and result.source == "system" and result.path == str(path)
    with pytest.raises(RuntimeError, match="受管环境之外"):
        cuda_libraries.prepare_cublas(managed_only=True)


def test_missing_or_incomplete_managed_runtime_fails_clearly(dll_boundary: CudaBoundary) -> None:
    with pytest.raises(RuntimeError, match="缺少 cuBLAS"):
        cuda_libraries.prepare_cublas(managed_only=True)
    path = dll_boundary.package("nvidia-cublas-cu12")
    path.with_name("cublasLt64_12.dll").unlink()
    with pytest.raises(RuntimeError, match="文件缺失"):
        cuda_libraries.prepare_cublas(managed_only=True)


def test_non_windows_does_not_load_windows_libraries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cuda_libraries, "os", SimpleNamespace(name="posix"))
    assert cuda_libraries.prepare_cublas(managed_only=True) is None


@pytest.mark.parametrize("conflict", [False, True])
def test_bare_name_uses_safe_default_search_and_checks_actual_companion(
    dll_boundary: CudaBoundary, monkeypatch: pytest.MonkeyPatch, conflict: bool
) -> None:
    path = dll_boundary.package("system")
    monkeypatch.delenv("CUDA_PATH", raising=False)
    monkeypatch.setenv("PATH", "")

    def load(name: str, *, winmode: int) -> SimpleNamespace:
        if name != "cublas64_12.dll":
            return dll_boundary.load(name, winmode=winmode)
        assert name == "cublas64_12.dll" and winmode == 0x1000
        dll_boundary.loaded.add(path)
        dll_boundary.loaded.add((dll_boundary.root / "other" if conflict else path.parent) / "cublasLt64_12.dll")
        return SimpleNamespace(cublasGetProperty=PropertyQuery())

    monkeypatch.setattr(ctypes, "WinDLL", load)
    if conflict:
        with pytest.raises(RuntimeError, match="不同目录"):
            cuda_libraries.prepare_cublas()
    else:
        result = cuda_libraries.prepare_cublas()
        assert result is not None and result.path == str(path) and result.source == "system"


def test_nvrtc_from_another_runtime_is_rejected(dll_boundary: CudaBoundary) -> None:
    dll_boundary.package("nvidia-cublas-cu12")
    dll_boundary.loaded.add(dll_boundary.root / "foreign/nvrtc64_120_0.dll")
    with pytest.raises(RuntimeError, match="NVRTC 已加载不同来源"):
        cuda_libraries.prepare_cublas(managed_only=True)
