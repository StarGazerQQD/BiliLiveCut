"""ASR 资源峰值检测与低配预设。

在加载 ASR 模型前检测系统资源, 防止 OOM:
- GPU 显存检测: 检测可用显存是否足够加载模型
- CPU 内存检测: 检测可用内存
- 低配预设: 自动降级到更小模型

预设:
- high: whisper large-v3 / paraformer-zh (需要 ≥6GB VRAM 或 ≥8GB RAM)
- medium: whisper medium / SenseVoice-Small (需要 ≥3GB VRAM 或 ≥4GB RAM)
- low: whisper small / Fun-ASR-Nano (需要 ≥1GB VRAM 或 ≥2GB RAM)
- minimal: whisper tiny (需要 ≥500MB VRAM 或 ≥1GB RAM)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


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
    vram_free_mb: float = 0.0
    vram_total_mb: float = 0.0
    ram_free_mb: float = 0.0
    ram_total_mb: float = 0.0
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


def detect_resources() -> ResourceInfo:
    """检测当前系统可用资源。

    :returns: :class:`ResourceInfo` 快照。
    """
    info = ResourceInfo()

    # 系统内存检测
    try:
        import psutil

        mem = psutil.virtual_memory()
        info.ram_total_mb = round(mem.total / (1024**2), 1)
        info.ram_free_mb = round(mem.available / (1024**2), 1)
    except ImportError:
        # 无 psutil 时保守估计
        info.ram_total_mb = 4096.0
        info.ram_free_mb = 2048.0

    # GPU 显存检测
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            info.gpu_available = True
            info.gpu_name = parts[0].strip()
            info.vram_total_mb = float(parts[1].strip())
            info.vram_free_mb = float(parts[2].strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass

    return info


def recommend_preset(device: str = "cpu") -> tuple[str, AsrPreset]:
    """根据系统资源推荐 ASR 预设。

    :param device: 目标设备 (cpu / cuda / auto)。
    :returns: (preset_name, AsrPreset)。
    """
    info = detect_resources()

    if device == "cpu" or not info.gpu_available:
        # CPU 模式: 根据可用 RAM 选择
        if info.ram_free_mb >= _PRESETS["high"].min_ram_mb:
            preset_name = "high"
        elif info.ram_free_mb >= _PRESETS["medium"].min_ram_mb:
            preset_name = "medium"
        elif info.ram_free_mb >= _PRESETS["low"].min_ram_mb:
            preset_name = "low"
        else:
            preset_name = "minimal"
    else:
        # GPU 模式: 根据可用 VRAM 选择
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

    :param model: 模型名 (tiny/small/medium/large-v3)。
    :param device: 设备 (cpu/cuda)。
    :returns: (是否足够, 描述信息)。
    """
    info = detect_resources()

    # 模型资源需求估算
    model_requirements: dict[str, tuple[int, int]] = {
        "tiny": (500, 1000),
        "small": (1500, 2000),
        "medium": (3000, 4000),
        "large-v3": (6000, 8000),
    }

    vram_need, ram_need = model_requirements.get(model.lower(), model_requirements["small"])

    if device == "cuda" and info.gpu_available:
        if info.vram_free_mb < vram_need * 0.8:  # 留 20% 余量
            return False, (
                f"显存不足: 需要 {vram_need}MB, 当前可用 {info.vram_free_mb:.0f}MB "
                f"(总 {info.vram_total_mb:.0f}MB). 建议切换为 cpu 模式或更小模型。"
            )
    else:
        if info.ram_free_mb < ram_need * 0.8:
            return False, (
                f"内存不足: 需要 {ram_need}MB, 当前可用 {info.ram_free_mb:.0f}MB "
                f"(总 {info.ram_total_mb:.0f}MB). 建议使用更小模型。"
            )

    return True, f"资源充足 ({info.ram_free_mb:.0f}MB RAM / {info.vram_free_mb:.0f}MB VRAM)"
