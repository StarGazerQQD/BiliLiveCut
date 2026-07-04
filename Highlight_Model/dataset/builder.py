"""训练数据集构建器。

从 ``ThresholdFeedback`` 表采集正负样本，调用
:class:`FeatureExtractor` 提取特征，组装为 ``(X, y)``
供训练使用。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class DatasetBundle:
    """一个完整的训练/评估数据集。

    :param X: 特征矩阵 shape ``(n_samples, n_features)``。
    :param y: 标签向量 shape ``(n_samples,)``，二值 {0,1}。
    :param feature_names: 特征名称列表。
    :param sample_ids: 每个样本对应的 candidate_id。
    """

    X: np.ndarray
    y: np.ndarray
    feature_names: list[str]
    sample_ids: list[int]


class DatasetBuilder:
    """从阈值反馈表构建训练数据集。

    :param min_samples: 最少正样本数，不足时无法训练。
    """

    def __init__(self, min_samples: int = 10) -> None:
        """初始化构建器。

        :param min_samples: 触发训练所需的最少正样本数。
        """
        self.min_samples = min_samples

    def build(self, room_id: int | None = None) -> DatasetBundle | None:
        """为指定房间（或全量）构建训练集。

        :param room_id: 直播间 id；为 ``None`` 时使用全部房间。
        :returns: :class:`DatasetBundle` 或 ``None``（样本不足时）。
        """
        # TODO: 阶段 2 实现
        return None
