# ruff: noqa: D100
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from threading import RLock

import psutil

_CUBLAS = "cublas64_12.dll"
_CUBLAS_LT = "cublasLt64_12.dll"
_NVRTC = "nvrtc64_120_0.dll"
_handles: dict[str, ctypes.CDLL] = {}
_lock = RLock()


@dataclass(frozen=True)
class CublasRuntime:
    """实际进程中唯一的 CUDA 12 cuBLAS 模块及来源。"""

    path: str
    version: str
    source: str
    nvrtc_path: str | None = None
    nvrtc_builtins_path: str | None = None

    def payload(self) -> dict[str, str | None]:
        """返回可记录的动态库来源，不包含转写内容。"""
        return asdict(self)


def _loaded_paths(filename: str) -> set[Path]:
    return {
        Path(mapping.path).resolve()
        for mapping in psutil.Process().memory_maps()
        if Path(mapping.path).name.casefold() == filename.casefold()
    }


def _package_library(package: str, filename: str = _CUBLAS) -> Path | None:
    try:
        installed = distribution(package)
    except PackageNotFoundError:
        return None
    matches = [
        Path(installed.locate_file(file)).resolve()
        for file in installed.files or ()
        if file.name.casefold() == filename.casefold()
    ]
    if len(matches) > 1:
        raise RuntimeError(f"{package} 包含多个 {filename}，无法确定运行时")
    if not matches:
        return None
    path = matches[0]
    if not path.is_file() or filename == _CUBLAS and not path.with_name(_CUBLAS_LT).is_file():
        raise RuntimeError(f"{package} 的 cuBLAS 文件缺失，请修复当前环境的 CUDA 依赖")
    return path


def _prepare_nvrtc(source: str, cublas: Path, *, managed_only: bool) -> tuple[str | None, str | None]:
    package = "nvidia-cuda-nvrtc-cu12" if source == "nvidia-cublas-cu12" else source
    nvrtc = _package_library(package, _NVRTC) if source != "system" else cublas.with_name(_NVRTC)
    if nvrtc is None or not nvrtc.is_file():
        if managed_only or source == "nvidia-cublas-cu12":
            raise RuntimeError("CUDA 12 NVRTC 依赖缺失，请修复配套运行时")
        return None, None
    builtins = list(nvrtc.parent.glob("nvrtc-builtins64_12*.dll"))
    if len(builtins) != 1:
        raise RuntimeError("CUDA 12 NVRTC builtins 缺失或版本不唯一，请修复配套运行时")
    builtin = builtins[0].resolve()
    if source != "system" and _package_library(package, builtin.name) != builtin:
        raise RuntimeError("NVRTC builtins 不属于同一已安装包")
    for path in (builtin, nvrtc):
        if managed_only and not path.is_relative_to(Path(sys.prefix).resolve()):
            raise RuntimeError(f"拒绝使用受管环境之外的 NVRTC: {path}")
        loaded = _loaded_paths(path.name)
        if loaded and loaded != {path}:
            raise RuntimeError(f"NVRTC 已加载不同来源的 {path.name}，请重启并修复 CUDA 库路径")
        _retain_library(path)
        if _loaded_paths(path.name) != {path}:
            raise RuntimeError(f"NVRTC 实际加载路径与选择不一致: {path.name}")
    return str(nvrtc), str(builtin)


def _load_library(path: Path | str) -> ctypes.CDLL:
    # 绝对路径的依赖只从同目录和 Windows 默认安全目录查找。
    return ctypes.WinDLL(str(path), winmode=0x00001100 if isinstance(path, Path) else 0x00001000)


def _retain_library(path: Path | str) -> ctypes.CDLL:
    key = str(path)
    if key not in _handles:
        _handles[key] = _load_library(path)
    return _handles[key]


def _version(library: ctypes.CDLL) -> str:
    get_property = library.cublasGetProperty
    get_property.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    get_property.restype = ctypes.c_int
    parts: list[int] = []
    for field in range(3):
        value = ctypes.c_int()
        status = get_property(field, ctypes.byref(value))
        if status != 0:
            raise RuntimeError(f"cuBLAS 版本查询失败，status={status}")
        parts.append(value.value)
    if parts[0] != 12:
        raise RuntimeError(f"CTranslate2 需要 cuBLAS 12，实际版本为 {parts}")
    return ".".join(map(str, parts))


def _system_library() -> Path | str:
    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        candidate = Path(cuda_path) / "bin" / _CUBLAS
        if candidate.is_file():
            return candidate.resolve()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and (candidate := Path(directory) / _CUBLAS).is_file():
            return candidate.resolve()
    return _CUBLAS


def prepare_cublas(*, managed_only: bool = False) -> CublasRuntime | None:
    """核验并复用 CTranslate2 按名称加载的唯一 cuBLAS，不初始化 GPU。"""
    if os.name != "nt":
        return None
    with _lock:
        candidates = {
            name: path for name in ("torch", "nvidia-cublas-cu12") if (path := _package_library(name)) is not None
        }
        loaded = _loaded_paths(_CUBLAS)
        if len(loaded) > 1:
            raise RuntimeError("进程已加载多个不同来源的 cublas64_12.dll，请重启并修复 CUDA 库路径")
        selected = next(iter(loaded), None)
        if selected is None:
            selected = next(iter(candidates.values()), None)
        if selected is None:
            if managed_only:
                raise RuntimeError("受管环境缺少 cuBLAS 12，请重新运行 --torch-runtime cu126 或 cu130")
            selected = _system_library()
        source = next((name for name, path in candidates.items() if path == selected), "system")
        if managed_only and (
            source == "system"
            or not isinstance(selected, Path)
            or not selected.is_relative_to(Path(sys.prefix).resolve())
        ):
            raise RuntimeError(f"拒绝使用受管环境之外的 cuBLAS: {selected}")
        nvrtc_paths = (
            _prepare_nvrtc(source, selected, managed_only=managed_only) if isinstance(selected, Path) else (None, None)
        )
        if isinstance(selected, Path):
            companion = selected.with_name(_CUBLAS_LT)
            loaded_companions = _loaded_paths(_CUBLAS_LT)
            if loaded_companions and loaded_companions != {companion}:
                raise RuntimeError("cuBLAS 与 cuBLASLt 来自不同目录，请重启并修复 CUDA 库路径")
            if companion.is_file() and not loaded_companions:
                _retain_library(companion)
        try:
            library = _retain_library(selected)
        except OSError as exc:
            raise RuntimeError("CTranslate2 无法加载 cuBLAS 12，请安装锁定 CUDA 依赖或检查系统 CUDA_PATH") from exc
        actual = _loaded_paths(_CUBLAS)
        if len(actual) != 1 or isinstance(selected, Path) and actual != {selected}:
            raise RuntimeError("cuBLAS 实际加载路径与选择不一致，拒绝继续推理")
        actual_companions = _loaded_paths(_CUBLAS_LT)
        if actual_companions and actual_companions != {next(iter(actual)).with_name(_CUBLAS_LT)}:
            raise RuntimeError("cuBLAS 与 cuBLASLt 来自不同目录，请重启并修复 CUDA 库路径")
        if not isinstance(selected, Path):
            nvrtc_paths = _prepare_nvrtc(source, next(iter(actual)), managed_only=managed_only)
        return CublasRuntime(str(next(iter(actual))), _version(library), source, *nvrtc_paths)
