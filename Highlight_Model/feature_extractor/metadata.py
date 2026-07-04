"""元数据/画像特征提取器 (M1-M11)。

基于直播间历史数据、主播画像、时间周期编码，
提取房间级统计和上下文特征。

依赖母仓库 :class:`app.db.models.LiveRoom`、
:class:`app.db.models.ThresholdFeedback` 等表。
"""

from __future__ import annotations

import numpy as np

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor

_METADATA_NAMES = [
    "streamer_id",
    "room_hist_highlight_rate",
    "room_approval_rate",
    "room_current_threshold",
    "room_auto_approve_threshold",
    "time_of_day_sin",
    "time_of_day_cos",
    "day_of_week_sin",
    "day_of_week_cos",
    "stream_duration_minutes",
    "config_hotword_count",
]


class MetadataExtractor(BaseFeatureExtractor):
    """元数据/画像特征提取器。

    将房间级元数据转换为 11 维画像特征。
    """

    @property
    def feature_names(self) -> list[str]:
        """返回画像特征名称列表。"""
        return list(_METADATA_NAMES)

    @property
    def n_features(self) -> int:
        """返回画像特征维数。"""
        return len(_METADATA_NAMES)

    def extract(self, segment_id: int) -> np.ndarray:
        """从指定片段提取画像特征向量。

        :param segment_id: ``raw_segments`` 主键。
        :returns: shape ``(11,)`` 的 float32 向量。
        """
        # TODO: 阶段 1 实现
        return np.zeros(self.n_features, dtype=np.float32)
