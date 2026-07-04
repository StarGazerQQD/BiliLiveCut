"""弹幕交互特征提取器 (D1-D13)。

基于片段时间窗内的弹幕数据，提取速率、爆发、情绪、
去重人数等维度的特征。

依赖母仓库 :mod:`app.analysis.highlight` 的弹幕评分函数
和 :mod:`app.db.models.Danmaku` 表。
"""

from __future__ import annotations

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_DANMAKU_NAMES = [
    "dm_window_count",
    "dm_window_rate",
    "dm_baseline_rate",
    "dm_rate_ratio",
    "dm_rate_acceleration",
    "dm_center_weighted_rate",
    "dm_burst_count",
    "dm_text_entropy",
    "dm_exclaim_ratio",
    "dm_meme_hit_ratio",
    "dm_high_value_ratio",
    "dm_viewer_unique",
    "dm_lead_lag_ms",
]


class DanmakuExtractor(BaseFeatureExtractor):
    """弹幕交互特征提取器。

    将片段时间窗内的弹幕数据转换为 13 维交互特征。
    """

    @property
    def feature_names(self) -> list[str]:
        """返回弹幕特征名称列表。"""
        return list(_DANMAKU_NAMES)

    @property
    def n_features(self) -> int:
        """返回弹幕特征维数。"""
        return len(_DANMAKU_NAMES)

    def extract(self, segment_id: int) -> np.ndarray:
        """从指定片段提取弹幕特征向量。

        :param segment_id: ``raw_segments`` 主键。
        :returns: shape ``(13,)`` 的 float32 向量。
        """
        # TODO: 阶段 1 实现
        return np.zeros(self.n_features, dtype=np.float32)
