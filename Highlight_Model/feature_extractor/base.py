"""特征提取器抽象基类与统一调度器。

:class:`BaseFeatureExtractor` 定义单个子模块的提取协议；
:class:`FeatureExtractor` 编排所有子模块，返回固定维度的特征向量。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class BaseFeatureExtractor(ABC):
    """单个特征子模块的基类。

    每个子类对应一个特征家族（声学/语义/弹幕/...），实现
    :meth:`extract` 返回本家族的 ``(n_features,)`` 向量。
    """

    @abstractmethod
    def extract(self, segment_id: int) -> np.ndarray:
        """从指定片段提取本家族的特征向量。

        :param segment_id: ``raw_segments`` 主键。
        :returns: shape ``(n_features,)`` 的 float32 向量。
        """
        ...

    @property
    @abstractmethod
    def feature_names(self) -> list[str]:
        """返回本家族所有特征的名称列表。"""
        ...

    @property
    @abstractmethod
    def n_features(self) -> int:
        """返回本家族的特征维数。"""
        ...


class FeatureExtractor:
    """统一的特征提取调度器。

    依次调用各子模块的 :meth:`extract`，拼接为完整特征向量，
    并维护特征名称到索引的映射。

    :param extractors: 按拼接顺序排列的子模块列表。
    """

    def __init__(self, extractors: list[BaseFeatureExtractor] | None = None) -> None:
        """初始化调度器。

        :param extractors: 子模块列表；为 ``None`` 时使用默认全部子模块。
        """
        if extractors is None:
            extractors = _default_extractors()
        self._extractors: list[BaseFeatureExtractor] = extractors
        self._names: list[str] = []
        self._offsets: list[int] = []
        offset = 0
        for ext in self._extractors:
            self._names.extend(ext.feature_names)
            self._offsets.append(offset)
            offset += ext.n_features

    @property
    def total_features(self) -> int:
        """总特征维数。"""
        if not self._extractors:
            return 0
        return self._offsets[-1] + self._extractors[-1].n_features

    @property
    def feature_names(self) -> list[str]:
        """所有特征的名称列表（按拼接顺序）。"""
        return list(self._names)

    def extract(self, segment_id: int) -> np.ndarray:
        """从指定片段提取完整特征向量。

        :param segment_id: ``raw_segments`` 主键。
        :returns: shape ``(total_features,)`` 的 float32 向量。
        """
        parts = [e.extract(segment_id) for e in self._extractors]
        return np.concatenate(parts).astype(np.float32)

    def named_features(self, segment_id: int) -> dict[str, float]:
        """以字典形式返回特征名→值。

        :param segment_id: 片段主键。
        :returns: ``{feature_name: value}``。
        """
        vec = self.extract(segment_id)
        return {name: float(vec[i]) for i, name in enumerate(self._names)}


def _default_extractors() -> list[BaseFeatureExtractor]:
    """构建默认的全部子模块列表。

    :returns: 按约定顺序排列的子模块实例。
    """
    from Highlight_Model.feature_extractor.acoustic import AcousticExtractor
    from Highlight_Model.feature_extractor.danmaku import DanmakuExtractor
    from Highlight_Model.feature_extractor.fusion import FusionExtractor
    from Highlight_Model.feature_extractor.linguistic import LinguisticExtractor
    from Highlight_Model.feature_extractor.metadata import MetadataExtractor
    from Highlight_Model.feature_extractor.temporal import TemporalExtractor

    return [
        AcousticExtractor(),
        LinguisticExtractor(),
        DanmakuExtractor(),
        TemporalExtractor(),
        MetadataExtractor(),
        FusionExtractor(),
    ]
