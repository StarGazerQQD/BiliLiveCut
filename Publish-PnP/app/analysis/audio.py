"""音频特征提取。

通过 FFmpeg 将片段解码为 16kHz 单声道 PCM,在内存中计算:

* RMS 能量包络(用于音量峰值打分);
* 静音区间(用于把切片边界吸附到自然停顿,避免断在词中间)。

所有计算基于 numpy,不写临时文件(PCM 直接从 ffmpeg 的 stdout 读取)。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import numpy as np
from loguru import logger

from app.core.config import settings

# 解码目标参数:16kHz 足够语音分析,单声道降低数据量。
_SAMPLE_RATE = 16000


@dataclass(slots=True)
class AudioFeatures:
    """片段的音频特征。

    :param sample_rate: 采样率(Hz)。
    :param hop_s: RMS 包络的帧步长(秒)。
    :param times: 每个 RMS 帧的中心时间(秒),形状 ``(n,)``。
    :param rms: 每帧 RMS 能量(已归一化到峰值=1),形状 ``(n,)``。
    :param duration_s: 音频总时长(秒)。
    :param silences: 静音区间列表 ``[(start_s, end_s), ...]``。
    """

    sample_rate: int
    hop_s: float
    times: np.ndarray
    rms: np.ndarray
    duration_s: float
    silences: list[tuple[float, float]]

    def peak_offset(self) -> float:
        """返回能量最高帧对应的时间偏移(秒)。

        :returns: 峰值时间(秒);无数据时返回 0。
        """
        if self.rms.size == 0:
            return 0.0
        return float(self.times[int(np.argmax(self.rms))])

    def volume_score(self) -> float:
        """计算音量维度的高光分(0-1)。

        思路:峰值相对于中位数的"突出程度"。安静片段中位数低、突发声音
        峰值高,比值越大越可能是爆点。用 ``1 - median/peak`` 近似突出度。

        :returns: 归一化音量分(0-1)。
        """
        if self.rms.size == 0:
            return 0.0
        peak = float(np.max(self.rms))
        median = float(np.median(self.rms))
        if peak <= 1e-6:
            return 0.0
        prominence = 1.0 - (median / peak)
        return float(np.clip(prominence, 0.0, 1.0))


def extract_pcm(path: str) -> np.ndarray:
    """用 FFmpeg 把媒体文件解码为归一化的单声道 PCM 浮点数组。

    FFmpeg 参数说明:

    * ``-i path``:输入文件;
    * ``-vn``:忽略视频流,只处理音频;
    * ``-ac 1``:混为单声道;
    * ``-ar 16000``:重采样到 16kHz;
    * ``-f s16le``:输出 16-bit 小端有符号 PCM(裸流);
    * ``-acodec pcm_s16le``:对应编码器;
    * ``pipe:1``:写到 stdout,避免落临时文件。

    :param path: 媒体文件路径。
    :returns: 取值约在 ``[-1, 1]`` 的一维 ``float32`` 数组;无音频时为空数组。
    :raises RuntimeError: FFmpeg 执行失败时。
    """
    cmd = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(_SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
        raise RuntimeError(f"FFmpeg 解码音频失败: {stderr}") from exc

    if not proc.stdout:
        logger.warning("片段无音频数据: {}", path)
        return np.zeros(0, dtype=np.float32)

    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm


def compute_rms_envelope(
    pcm: np.ndarray,
    sample_rate: int = _SAMPLE_RATE,
    hop_s: float = 0.1,
) -> tuple[np.ndarray, np.ndarray]:
    """计算逐帧 RMS 能量包络,并归一化到峰值为 1。

    :param pcm: 单声道 PCM 浮点数组。
    :param sample_rate: 采样率。
    :param hop_s: 帧步长(秒),默认 100ms。
    :returns: ``(times, rms)``,均为一维数组;无数据时返回两个空数组。
    """
    if pcm.size == 0:
        return np.zeros(0), np.zeros(0)

    hop = max(1, int(sample_rate * hop_s))
    n_frames = pcm.size // hop
    if n_frames == 0:
        n_frames = 1
        frames = pcm[np.newaxis, :]
    else:
        frames = pcm[: n_frames * hop].reshape(n_frames, hop)

    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    peak = float(np.max(rms))
    if peak > 1e-9:
        rms = rms / peak  # 归一化便于跨片段比较与阈值设定

    times = (np.arange(n_frames) + 0.5) * hop_s
    return times, rms


def find_silences(
    times: np.ndarray,
    rms: np.ndarray,
    threshold_ratio: float = 0.15,
    min_silence_s: float = 0.3,
) -> list[tuple[float, float]]:
    """从 RMS 包络中检测静音区间。

    低于 ``threshold_ratio``(相对归一化峰值)且持续不短于 ``min_silence_s``
    的连续帧视为一段静音。这些区间用于将切片边界吸附到自然停顿处。

    :param times: 帧中心时间数组。
    :param rms: 归一化 RMS 数组。
    :param threshold_ratio: 静音阈值(占峰值比例)。
    :param min_silence_s: 最短静音时长(秒)。
    :returns: 静音区间列表 ``[(start_s, end_s), ...]``。
    """
    if rms.size == 0:
        return []

    hop_s = float(times[1] - times[0]) if times.size > 1 else 0.1
    quiet = rms < threshold_ratio
    silences: list[tuple[float, float]] = []
    start_idx: int | None = None

    for i, is_quiet in enumerate(quiet):
        if is_quiet and start_idx is None:
            start_idx = i
        elif not is_quiet and start_idx is not None:
            duration = (i - start_idx) * hop_s
            if duration >= min_silence_s:
                silences.append((float(times[start_idx]), float(times[i - 1])))
            start_idx = None

    # 收尾:末尾仍处于静音。
    if start_idx is not None:
        duration = (len(quiet) - start_idx) * hop_s
        if duration >= min_silence_s:
            silences.append((float(times[start_idx]), float(times[-1])))

    return silences


def analyze_audio(path: str, hop_s: float = 0.1) -> AudioFeatures:
    """对一个媒体文件做完整音频分析。

    :param path: 媒体文件路径。
    :param hop_s: RMS 帧步长(秒)。
    :returns: :class:`AudioFeatures`。
    """
    pcm = extract_pcm(path)
    times, rms = compute_rms_envelope(pcm, _SAMPLE_RATE, hop_s)
    duration = float(pcm.size / _SAMPLE_RATE) if pcm.size else 0.0
    silences = find_silences(times, rms)
    return AudioFeatures(
        sample_rate=_SAMPLE_RATE,
        hop_s=hop_s,
        times=times,
        rms=rms,
        duration_s=duration,
        silences=silences,
    )


def snap_to_silence(
    target_s: float,
    silences: list[tuple[float, float]],
    max_shift_s: float = 5.0,
) -> float:
    """把一个目标时间点吸附到最近静音区间的中点,避免切在词中间。

    若最近的静音中点距目标超过 ``max_shift_s``,则保持原值(不强行远移)。

    :param target_s: 期望的切点时间(秒)。
    :param silences: 静音区间列表。
    :param max_shift_s: 允许的最大吸附位移(秒)。
    :returns: 吸附后的切点时间(秒)。
    """
    if not silences:
        return target_s
    midpoints = [(s + e) / 2.0 for s, e in silences]
    nearest = min(midpoints, key=lambda m: abs(m - target_s))
    if abs(nearest - target_s) <= max_shift_s:
        return nearest
    return target_s
