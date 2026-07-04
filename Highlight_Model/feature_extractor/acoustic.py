"""声学特征提取器 (A1-A38)。

基于片段的 PCM 音频数据，提取 RMS 能量统计、频谱特征、
MFCC、基频、谐噪比、微扰等维度的特征。

依赖母仓库 :mod:`app.analysis.audio` 的 ``extract_pcm`` /
``compute_rms_envelope`` / ``find_silences`` 作为基础。
"""

from __future__ import annotations

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

# ---------------------------------------------------------------------------
# 特征名称表（与 README 中 A1-A38 一一对应）
# ---------------------------------------------------------------------------
_ACOUSTIC_NAMES = [
    # RMS 统计 (A1-A6)
    "rms_mean",
    "rms_median",
    "rms_std",
    "rms_p25",
    "rms_p75",
    "rms_p90",
    # 峰均比 (A7)
    "crest_factor",
    # 能量分布 (A8-A10)
    "energy_entropy",
    "short_term_energy_ratio",
    "rms_delta_max",
    # 静音特征 (A11-A14)
    "silence_ratio",
    "silence_count",
    "avg_silence_duration",
    "pause_before_peak",
    # 爆点斜率 (A15)
    "peak_slope",
    # 频谱特征 (A16-A18)
    "spectral_centroid",
    "spectral_bandwidth",
    "spectral_rolloff",
    # MFCC (A19-A31)
    *(f"mfcc_{i}" for i in range(1, 14)),
    # 过零率 (A32)
    "zero_crossing_rate",
    # 基频 (A33-A35)
    "pitch_mean",
    "pitch_std",
    "pitch_range",
    # 谐噪比 (A36)
    "harmonic_noise_ratio",
    # 微扰 (A37-A38)
    "jitter",
    "shimmer",
]


class AcousticExtractor(BaseFeatureExtractor):
    """声学特征提取器。

    从片段的 PCM 音频中提取 38 维声学特征，涵盖能量、频谱、
    基频、音色等维度。
    """

    @property
    def feature_names(self) -> list[str]:
        """返回声学特征名称列表。"""
        return list(_ACOUSTIC_NAMES)

    @property
    def n_features(self) -> int:
        """返回声学特征维数。"""
        return len(_ACOUSTIC_NAMES)

    def extract(self, segment_id: int) -> np.ndarray:
        """从指定片段提取声学特征向量。

        :param segment_id: ``raw_segments`` 主键。
        :returns: shape ``(38,)`` 的 float32 向量。
        """
        # TODO: 阶段 1 实现 — 调用母仓库 audio 模块 + librosa 频谱分析
        return np.zeros(self.n_features, dtype=np.float32)
