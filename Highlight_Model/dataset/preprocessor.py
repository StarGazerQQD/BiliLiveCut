"""特征预处理器 (v0.1.9.1b-HL-Alpha)。

缺失值中位数填充 + StandardScaler 标准化，支持 fit/transform 和序列化。
"""
from __future__ import annotations

import numpy as np


class FeaturePreprocessor:
    """特征预处理管线。"""

    def __init__(self, impute_strategy: str = "median",
                 normalize: bool = True) -> None:
        self.impute_strategy = impute_strategy
        self.normalize = normalize
        self._impute_values: np.ndarray | None = None
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None
        self._is_fitted: bool = False

    def fit(self, X: np.ndarray) -> FeaturePreprocessor:
        X = np.asarray(X, dtype=np.float64)
        # 缺失值填充值
        if self.impute_strategy == "median":
            self._impute_values = np.nanmedian(X, axis=0)
        elif self.impute_strategy == "mean":
            self._impute_values = np.nanmean(X, axis=0)
        else:
            self._impute_values = np.zeros(X.shape[1])
        self._impute_values = np.where(
            np.isnan(self._impute_values), 0.0, self._impute_values
        )

        # 标准化参数（在填充后计算）
        X_filled = np.where(np.isnan(X), self._impute_values, X)
        if self.normalize:
            self._mean = np.mean(X_filled, axis=0)
            self._std = np.std(X_filled, axis=0)
            self._std = np.where(self._std < 1e-8, 1.0, self._std)
        self._is_fitted = True
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        if not self._is_fitted:
            raise RuntimeError("请先调用 fit()")
        X = np.asarray(X, dtype=np.float64)
        X = np.where(np.isnan(X), self._impute_values, X)
        if self.normalize:
            X = (X - self._mean) / self._std
        return X.astype(np.float32)

    def fit_transform(self, X: np.ndarray) -> np.ndarray:
        return self.fit(X).transform(X)
