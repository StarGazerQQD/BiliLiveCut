"""特征提取模块 — BiliLiveCut ML 高光模型。

提供从 ``RawSegment`` 中提取多维特征的统一接口，分为六个子模块：

* :mod:`~Highlight_Model.feature_extractor.acoustic` — 声学特征 (A1-A38)
* :mod:`~Highlight_Model.feature_extractor.linguistic` — 语义/语言特征 (L1-L21)
* :mod:`~Highlight_Model.feature_extractor.danmaku` — 弹幕交互特征 (D1-D13)
* :mod:`~Highlight_Model.feature_extractor.temporal` — 时序/上下文特征 (T1-T9)
* :mod:`~Highlight_Model.feature_extractor.metadata` — 元数据/画像特征 (M1-M11)
* :mod:`~Highlight_Model.feature_extractor.fusion` — 跨模态融合特征 (C1-C6)

所有子模块通过 :class:`FeatureExtractor` 统一调度，输出固定维度的特征向量。
"""

from __future__ import annotations

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor, FeatureExtractor

__all__ = ["BaseFeatureExtractor", "FeatureExtractor"]
