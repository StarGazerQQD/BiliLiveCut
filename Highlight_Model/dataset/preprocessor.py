"""特征预处理器。

提供缺失值填充、归一化、类别编码等预处理管线。
"""

from __future__ import annotations

import numpy as np


class FeaturePreprocessor:
    """特征预处理管线。

    支持 fit/transform 模式，可序列化状态以供推理时复用。

    :param impute_strategy: 缺失值填充策略 (``"median"`` / ``"mean"`` / ``"zero"``)。
    :param normalize: 是否做标准化 (zero-mean unit-variance)。
    """

    def __init__(
        self,
        impute_strategy: str = "median",
        normalize: bool = True,
    ) -> None:
        """初始化预处理器。

        :param impute_strategy: 填充策略。
        :param normalize: 是否标准化。
        """
        self.impute_strategy = impute_strategy
        self.normalize = normalize
        self._impute_values: np.ndarray | None = None
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._is_fitted: bool = False

    def fit(self, X: np.ndarray) -> FeaturePreprocessor:
        """从数据中学习预处理参数。

        :param X: shape ``(n_samples, n_features)``。
        :returns: self。
        """
        # TODO: 阶段 2 实现
        self._is_fitted = True
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        """应用预处理。

        :param X: shape ``(n_samples, n_features)``。
        :returns: 预处理后的特征矩阵。
        :raises RuntimeError: 尚未 fit 时。
        """
        if not self._is_fitted:
            raise RuntimeError("请先调用 fit()")
        # TODO: 阶段 2 实现
        return X.astype(np.float32)

    def fit_transform(self, X: np.ndarray) -> np.ndarray:
        """拟合并转换。

        :param X: 特征矩阵。
        :returns: 预处理后的特征矩阵。
        """
        return self.fit(X).transform(X)
