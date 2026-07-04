"""时序/上下文特征提取器 (T1-T9)。

基于片段在整场直播中的位置及其与相邻片段的关系，
提取时长、进度比、邻段差异、滑动窗口统计等特征。

依赖母仓库 :class:`app.db.models.RawSegment` 的
``start_ts`` / ``end_ts`` / ``seq`` 字段。
"""

from __future__ import annotations

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_TEMPORAL_NAMES = [
    "segment_duration_s",
    "segment_size_bytes",
    "session_elapsed_ratio",
    "time_since_last_highlight_s",
    "neighbor_volume_diff",
    "neighbor_dm_diff",
    "rolling_volume_avg",
    "rolling_dm_avg",
    "feature_change_rate",
]


class TemporalExtractor(BaseFeatureExtractor):
    """时序/上下文特征提取器。

    将片段的时间属性转换为 9 维时序特征。
    """

    @property
    def feature_names(self) -> list[str]:
        """返回时序特征名称列表。"""
        return list(_TEMPORAL_NAMES)

    @property
    def n_features(self) -> int:
        """返回时序特征维数。"""
        return len(_TEMPORAL_NAMES)

    def extract(self, segment_id: int) -> np.ndarray:
        """从指定片段提取时序特征向量。

        :param segment_id: ``raw_segments`` 主键。
        :returns: shape ``(9,)`` 的 float32 向量。
        """
        # TODO: 阶段 1 实现
        return np.zeros(self.n_features, dtype=np.float32)
