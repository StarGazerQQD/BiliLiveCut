"""模型推理接口。

加载训练好的模型文件，提供 ``predict_proba`` 方法供
母仓库 ``score_segment()`` 可插拔接入。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from Highlight_Model.feature_extractor.base import FeatureExtractor

logger = logging.getLogger(__name__)


class ModelInference:
    """高光模型推理器。

    加载序列化模型，对外暴露与现有 ``score_segment`` 兼容的接口。

    :param model_path: 模型文件路径 (``.pkl`` / ``.json`` / ``.onnx``)。
    :param feature_extractor: 特征提取器实例。
    """

    def __init__(
        self,
        model_path: str | Path,
        feature_extractor: FeatureExtractor | None = None,
    ) -> None:
        """初始化推理器。

        :param model_path: 模型文件路径。
        :param feature_extractor: 特征提取器；为 ``None`` 时用默认全部子模块。
        """
        self.model_path = Path(model_path)
        self._extractor = feature_extractor or FeatureExtractor()
        self._model: object | None = None
        self._loaded: bool = False

    def load(self) -> None:
        """从磁盘加载模型。

        :raises FileNotFoundError: 模型文件不存在时。
        """
        # TODO: 阶段 5 实现
        if not self.model_path.exists():
            raise FileNotFoundError(f"模型文件不存在: {self.model_path}")
        self._loaded = True
        logger.info("模型加载成功: {}", self.model_path)

    def predict_proba(self, segment_id: int) -> float:
        """对指定片段预测高光概率。

        :param segment_id: ``raw_segments`` 主键。
        :returns: 0-1 的高光概率。
        :raises RuntimeError: 模型未加载时。
        """
        if not self._loaded:
            self.load()
        features = self._extractor.extract(segment_id)
        # TODO: 阶段 5 实现 — 调用 _model.predict_proba
        logger.debug("推理占位 segment={}", segment_id)
        return float(np.mean(features))  # 占位
