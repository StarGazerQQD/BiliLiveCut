"""语义/语言特征提取器 (L1-L21)。

基于片段的 ASR 转写文本和词级时间戳，提取语速、关键词、
情感、文本 embedding 等维度的特征。

依赖母仓库 :mod:`app.analysis.transcribe` 的 ``Transcript``
和 :mod:`app.analysis.keywords` 的 ``match_keywords``。
"""

from __future__ import annotations

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_LINGUISTIC_NAMES = [
    "text_length_chars",
    "word_count",
    "whisper_confidence",
    "speech_rate_wps",
    "speech_rate_peak_ratio",
    "pause_density",
    "keyword_hit_count",
    "keyword_density",
    "exclamation_ratio",
    "laughter_char_ratio",
    "sentiment_score",
    "emotion_joy",
    "emotion_surprise",
    "emotion_anger",
    "emotion_sadness",
    "emotion_fear",
    "topic_coherence",
    "info_density",
    "qa_pattern_flag",
    "filler_word_ratio",
    "text_embedding_dim",  # 降维后预留
]


class LinguisticExtractor(BaseFeatureExtractor):
    """语义/语言特征提取器。

    将 ASR 转写文本转换为 21 维语义特征。
    """

    @property
    def feature_names(self) -> list[str]:
        """返回语义特征名称列表。"""
        return list(_LINGUISTIC_NAMES)

    @property
    def n_features(self) -> int:
        """返回语义特征维数。"""
        return len(_LINGUISTIC_NAMES)

    def extract(self, segment_id: int) -> np.ndarray:
        """从指定片段提取语义特征向量。

        :param segment_id: ``raw_segments`` 主键。
        :returns: shape ``(21,)`` 的 float32 向量。
        """
        # TODO: 阶段 1 实现
        return np.zeros(self.n_features, dtype=np.float32)
