"""ASR 资源峰值检测与低配预设。

在加载 ASR 模型前估算系统资源，不能保证推理期间不会发生 OOM:
- GPU 显存检测: 检测可用显存是否足够加载模型
- CPU 内存检测: 检测可用内存
- 低配预设: 提供模型建议，不自动更换实际加载模型

预设:
- high: whisper large-v3 / paraformer-zh (需要 ≥6GB VRAM 或 ≥8GB RAM)
- medium: whisper medium / SenseVoice-Small (需要 ≥3GB VRAM 或 ≥4GB RAM)
- low: whisper small / Fun-ASR-Nano (需要 ≥1GB VRAM 或 ≥2GB RAM)
- minimal: whisper tiny (需要 ≥500MB VRAM 或 ≥1GB RAM)
"""

from __future__ import annotations

import csv
import ctypes
import math
import subprocess
import sys
from dataclasses import dataclass
from uuid import UUID

from loguru import logger

from app.core.asr_devices import cuda_device_index
from app.core.sanitize import safe_exception_summary


@dataclass
class ResourceInfo:
    """系统资源快照。

    :param gpu_available: 是否有可用 GPU。
    :param vram_free_mb: 可用显存 (MB)。
    :param vram_total_mb: 总显存 (MB)。
    :param ram_free_mb: 可用系统内存 (MB)。
    :param ram_total_mb: 总系统内存 (MB)。
    :param gpu_name: GPU 名称。
    """

    gpu_available: bool = False
    vram_free_mb: float | None = None
    vram_total_mb: float | None = None
    ram_free_mb: float | None = None
    ram_total_mb: float | None = None
    gpu_name: str = ""
    preset: str = "unknown"


@dataclass
class AsrPreset:
    """ASR 模型预设。

    :param name: 预设名称。
    :param model: 模型名称。
    :param min_vram_mb: 最低显存要求 (MB)。
    :param min_ram_mb: 最低内存要求 (MB)。
    :param estimated_load_time_s: 预估加载时间 (秒)。
    """

    name: str
    model: str
    min_vram_mb: int
    min_ram_mb: int
    estimated_load_time_s: int = 30


# 预设定义
_PRESETS: dict[str, AsrPreset] = {
    "high": AsrPreset("high", "large-v3", min_vram_mb=6000, min_ram_mb=8000, estimated_load_time_s=120),
    "medium": AsrPreset("medium", "medium", min_vram_mb=3000, min_ram_mb=4000, estimated_load_time_s=60),
    "low": AsrPreset("low", "small", min_vram_mb=1500, min_ram_mb=2000, estimated_load_time_s=30),
    "minimal": AsrPreset("minimal", "tiny", min_vram_mb=500, min_ram_mb=1000, estimated_load_time_s=15),
}


def _cuda_device_uuid(index: int) -> str:
    """使用 CUDA 逻辑序号取得 UUID，保留驱动的可见设备及排序规则。"""
    driver = ctypes.WinDLL("nvcuda.dll", winmode=0x00000800) if sys.platform == "win32" else ctypes.CDLL("libcuda.so.1")
    init = driver.cuInit
    init.argtypes = [ctypes.c_uint]
    init.restype = ctypes.c_int
    get_device = driver.cuDeviceGet
    get_device.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    get_device.restype = ctypes.c_int
    get_uuid = driver.cuDeviceGetUuid_v2
    uuid_type = ctypes.c_ubyte * 16
    get_uuid.argtypes = [ctypes.POINTER(uuid_type), ctypes.c_int]
    get_uuid.restype = ctypes.c_int
    device = ctypes.c_int()
    identifier = uuid_type()
    for name, function, arguments in (
        ("cuInit", init, (0,)),
        ("cuDeviceGet", get_device, (ctypes.byref(device), index)),
    ):
        status = function(*arguments)
        if status != 0:
            raise RuntimeError(f"{name} 查询 cuda:{index} 失败，status={status}")
    status = get_uuid(ctypes.byref(identifier), device.value)
    if status != 0:
        raise RuntimeError(f"cuDeviceGetUuid_v2 查询 cuda:{index} 失败，status={status}")
    return f"GPU-{UUID(bytes=bytes(identifier))}"


def detect_resources(*, cuda_index: int | None = None) -> ResourceInfo:
    """检测当前系统可用资源。

    :param cuda_index: 推理的 CUDA 逻辑序号；省略时仅提供首块物理 GPU 概览。
    :returns: :class:`ResourceInfo` 快照，无法核实的资源保持未知。
    """
    info = ResourceInfo()

    # 系统内存检测
    try:
        import psutil

        mem = psutil.virtual_memory()
        info.ram_total_mb = round(mem.total / (1024**2), 1)
        info.ram_free_mb = round(mem.available / (1024**2), 1)
    except ImportError:
        logger.warning("内存检测不可用: 未安装 psutil，资源量未知")

    # GPU 显存检测
    try:
        selection = [f"--id={_cuda_device_uuid(cuda_index)}"] if cuda_index is not None else []
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
                *selection,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            rows = list(csv.reader(result.stdout.splitlines()))
            if cuda_index is not None and len(rows) != 1:
                raise ValueError("所选 CUDA 设备的显存查询结果不唯一")
            parts = rows[0]
            if len(parts) != 3:
                raise ValueError("nvidia-smi 返回字段数量无效")
            total, free = float(parts[1]), float(parts[2])
            if not all(math.isfinite(value) and value >= 0 for value in (total, free)) or free > total:
                raise ValueError("nvidia-smi 返回显存数值无效")
            info.gpu_name, info.vram_total_mb, info.vram_free_mb = parts[0].strip(), total, free
            info.gpu_available = True
    except FileNotFoundError:
        pass  # 无 NVIDIA 工具是正常的 CPU 环境。
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError, RuntimeError) as exc:
        logger.warning("GPU 资源检测失败 cuda_index={}: {}", cuda_index, safe_exception_summary(exc))

    return info


def recommend_preset(device: str = "cpu") -> tuple[str, AsrPreset]:
    """根据系统资源推荐 ASR 预设。

    :param device: 目标设备 (cpu / cuda / auto)。
    :returns: (preset_name, AsrPreset)。
    """
    info = detect_resources()

    if device == "cpu" or not info.gpu_available:
        # CPU 模式: 根据可用 RAM 选择
        if info.ram_free_mb is not None and info.ram_free_mb >= _PRESETS["high"].min_ram_mb:
            preset_name = "high"
        elif info.ram_free_mb is not None and info.ram_free_mb >= _PRESETS["medium"].min_ram_mb:
            preset_name = "medium"
        elif info.ram_free_mb is not None and info.ram_free_mb >= _PRESETS["low"].min_ram_mb:
            preset_name = "low"
        else:
            preset_name = "minimal"
    else:
        # GPU 模式: 根据可用 VRAM 选择
        assert info.vram_free_mb is not None
        if info.vram_free_mb >= _PRESETS["high"].min_vram_mb:
            preset_name = "high"
        elif info.vram_free_mb >= _PRESETS["medium"].min_vram_mb:
            preset_name = "medium"
        elif info.vram_free_mb >= _PRESETS["low"].min_vram_mb:
            preset_name = "low"
        else:
            preset_name = "minimal"

    preset = _PRESETS[preset_name]
    info.preset = preset_name
    return preset_name, preset


def check_resources_sufficient(model: str = "small", device: str = "cpu") -> tuple[bool, str]:
    """检查资源是否足够加载指定模型。

    :param model: 实际加载标识，仅对已有预算的模型名作估算。
    :param device: 已解析的设备 (cpu/cuda/cuda:N)。
    :returns: (是否足够, 描述信息)。
    """
    index = cuda_device_index(device)
    if index is None and device != "cpu":
        return False, f"资源检查的设备尚未明确: {device}，需要 cpu 或具体 CUDA 设备"

    # 模型资源需求估算
    model_requirements: dict[str, tuple[int, int]] = {
        "tiny": (500, 1000),
        "small": (1500, 2000),
        "medium": (3000, 4000),
        "large-v3": (6000, 8000),
    }

    requirements = model_requirements.get(model.lower())
    if requirements is None:
        return False, f"模型 {model!r} 的资源需求未知，无法根据现有预算确认内存或显存是否足够"
    vram_need, ram_need = requirements
    info = detect_resources(cuda_index=index)

    if index is not None:
        if not info.gpu_available or info.vram_free_mb is None:
            return False, f"cuda:{index} 不可用或显存检测失败，请检查 NVIDIA 驱动和诊断日志"
        if info.vram_free_mb < vram_need * 0.8:
            return False, (
                f"显存不足: 需要 {vram_need}MB, 当前可用 {info.vram_free_mb:.0f}MB "
                f"(总 {info.vram_total_mb:.0f}MB). 建议切换为 cpu 模式或更小模型。"
            )
    else:
        if info.ram_free_mb is None:
            return False, "系统可用内存未知，请安装 psutil 后重新检测"
        if info.ram_free_mb < ram_need * 0.8:
            return False, (
                f"内存不足: 需要 {ram_need}MB, 当前可用 {info.ram_free_mb:.0f}MB "
                f"(总 {info.ram_total_mb:.0f}MB). 建议使用更小模型。"
            )

    return True, f"资源检查通过 (RAM={info.ram_free_mb}MB / VRAM={info.vram_free_mb}MB)"
