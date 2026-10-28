"""Check the actual inference backend before honoring an explicit CUDA request."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, cast

from loguru import logger

from app.core.asr_failures import ASRDeviceError, DeviceFailure, cached_device_failure, remember_device_failure
from app.core.cuda_libraries import prepare_cublas
from app.core.sanitize import safe_exception_summary


class _Parameter(Protocol):
    device: object


class _TorchModule(Protocol):
    def parameters(self) -> Iterator[_Parameter]: ...


def cuda_device_index(device: str) -> int | None:
    """Parse an explicit CUDA device while leaving other backend choices intact."""
    if device == "cuda":
        return 0
    if not device.startswith("cuda"):
        return None
    prefix, separator, index = device.partition(":")
    if prefix != "cuda" or not separator or not index.isascii() or not index.isdecimal():
        raise ASRDeviceError("CUDA 设备必须为 cuda 或 cuda:非负整数")
    return int(index)


def require_torch_device(device: str, *, role: str) -> None:
    """Verify Torch build, driver, device and a real kernel before loading a model."""
    index = cuda_device_index(device)
    if index is None:
        return
    try:
        import torch
    except ImportError as exc:
        raise ASRDeviceError("未安装 PyTorch，无法使用 CUDA 转写") from exc
    build = str(torch.__version__)
    cuda_version = torch.version.cuda
    target = f"cuda:{index}"
    if (failure := cached_device_failure(target, build)) is not None:
        raise ASRDeviceError(failure.message, failure=failure)
    if cuda_version is None:
        message = (
            f"{role} 请求 {device}，但 PyTorch {build} 是 CPU 版。"
            "仅修改 ASR_*_DEVICE 不会安装 CUDA；Portable 请运行 --torch-runtime cu126 或 cu130，"
            "源码安装请按 docs/features/asr.md 的 CUDA 锁文件说明修复。"
        )
        failure = DeviceFailure(device=target, build=build, code="cpu_build", message=message)
        remember_device_failure(failure)
        raise ASRDeviceError(message, failure=failure)
    if not torch.cuda.is_available() or index >= torch.cuda.device_count():
        raise ASRDeviceError(
            f"{role} 请求 {device}，PyTorch {build} / CUDA {cuda_version} 无可用的对应 GPU。"
            "请检查 NVIDIA 驱动、显卡支持和 CUDA_VISIBLE_DEVICES；不会自动改用 CPU。"
        )
    try:
        probe = torch.zeros(1, device=f"cuda:{index}")
        probe.add_(1)
        torch.cuda.synchronize(index)
        del probe
    except (RuntimeError, AssertionError) as exc:
        if "no kernel image is available for execution on the device" in str(exc).lower():
            message = (
                f"{role} 的 {device} 无可执行 CUDA 内核，PyTorch={build} CUDA={cuda_version}。"
                "RTX 50 系列等 Blackwell 显卡请安装 --torch-runtime cu130 后重启服务。"
            )
            failure = DeviceFailure(device=target, build=build, code="unsupported_kernel", message=message)
            remember_device_failure(failure)
            raise ASRDeviceError(message, failure=failure) from exc
        raise ASRDeviceError(f"{role} 的 {device} 实际 CUDA 运算失败，请检查驱动和显卡架构支持") from exc
    logger.info("ASR CUDA 验证 role={} device={} torch={} cuda={}", role, device, build, cuda_version)


def confirm_torch_model_device(model: object, device: str, *, role: str) -> None:
    """Reject SDK fallback when the loaded model or its VAD/punctuation is on CPU."""
    index = cuda_device_index(device)
    if index is None:
        return
    for component in ("model", "vad_model", "punc_model"):
        module = getattr(model, component, None)
        if module is None and component != "model":
            continue
        if module is None or not callable(getattr(module, "parameters", None)):
            raise ASRDeviceError(f"{role}/{component} 实际设备不可核实，拒绝报告 CUDA 加载成功")
        parameter = next(cast(_TorchModule, module).parameters(), None)
        actual = str(parameter.device) if parameter is not None else "unknown"
        if actual != f"cuda:{index}":
            raise ASRDeviceError(f"{role}/{component} 请求 {device}，实际参数设备为 {actual}；拒绝静默回退")
    logger.info("ASR 模型实际设备 role={} device=cuda:{}", role, index)


def require_whisper_device(device: str) -> int | None:
    """Whisper uses CTranslate2; a CPU Torch build is not evidence about this backend."""
    index = cuda_device_index(device)
    if index is None and device != "auto":
        return None
    try:
        import ctranslate2
    except ImportError as exc:
        raise ASRDeviceError("未安装 CTranslate2，无法使用 Whisper CUDA") from exc
    count = ctranslate2.get_cuda_device_count()
    if device == "auto":
        if count == 0:
            return None
        index = 0
    if index is None or index >= count:
        raise ASRDeviceError(f"Whisper 请求 {device}，但 CTranslate2 未检测到对应 CUDA 设备；请检查 NVIDIA 驱动")
    try:
        runtime = prepare_cublas(managed_only=os.environ.get("BLC_PORTABLE") == "1")
    except (OSError, RuntimeError) as exc:
        raise ASRDeviceError("Whisper CUDA 运行库不可用，请检查 cuBLAS 12 安装与实际 DLL 来源") from exc
    if runtime is not None:
        logger.info("ASR CTranslate2 CUDA library {}", runtime.payload())
    try:
        ctranslate2.get_supported_compute_types("cuda", index)
    except RuntimeError as exc:
        raise ASRDeviceError("CTranslate2 CUDA 初始化失败，请检查 CUDA/cuDNN 运行库和 NVIDIA 驱动") from exc
    return index


def device_report(backend: str, device: str) -> dict[str, str | bool | None]:
    """Report package build and effective capability without downloading a model."""
    report: dict[str, str | bool | None] = {
        "backend": backend,
        "requested": device,
        "ok": False,
        "version": None,
        "cuda_build": None,
        "cuda_available": None,
    }
    try:
        if backend == "torch":
            import torch

            report.update(
                version=str(torch.__version__), cuda_build=torch.version.cuda, cuda_available=torch.cuda.is_available()
            )
            require_torch_device(device, role="diagnostic")
        elif backend == "ctranslate2":
            import ctranslate2

            report.update(version=ctranslate2.__version__, cuda_available=ctranslate2.get_cuda_device_count() > 0)
            require_whisper_device(device)
        else:
            raise ValueError("不支持的 ASR 后端")
        report["ok"] = True
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        report["error"] = safe_exception_summary(exc)
    return report


def probe_device(backend: str, device: str) -> dict[str, str | bool | None]:
    """Bound diagnostic imports/CUDA initialization in a separate interpreter."""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [sys.executable, "-m", "app.core.asr_devices", backend, device],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    report = json.loads(result.stdout)
    if (
        not isinstance(report, dict)
        or type(report.get("ok")) is not bool
        or any(
            not isinstance(key, str) or (value is not None and not isinstance(value, (str, bool)))
            for key, value in report.items()
        )
    ):
        raise ValueError("ASR 设备诊断输出无效")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m app.core.asr_devices torch|ctranslate2 cpu|cuda[:index]")
    print(json.dumps(device_report(sys.argv[1], sys.argv[2]), ensure_ascii=True))
