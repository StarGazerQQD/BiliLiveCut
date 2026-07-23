"""训练与推理共享的无泄漏特征预处理。"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class PreprocessorState:
    """可 JSON 序列化的中位数填充与标准化参数。"""

    feature_names: tuple[str, ...]
    medians: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    continuous: tuple[bool, ...]

    def to_dict(self) -> dict[str, object]:
        """转换为模型产物中的 JSON 对象。"""
        return {
            "feature_names": list(self.feature_names),
            "medians": list(self.medians),
            "means": list(self.means),
            "scales": list(self.scales),
            "continuous": list(self.continuous),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> PreprocessorState:
        """从模型产物恢复并验证预处理状态。"""
        try:
            names = tuple(str(item) for item in payload["feature_names"])  # type: ignore[union-attr]
            medians = tuple(float(item) for item in payload["medians"])  # type: ignore[union-attr]
            means = tuple(float(item) for item in payload["means"])  # type: ignore[union-attr]
            scales = tuple(float(item) for item in payload["scales"])  # type: ignore[union-attr]
            continuous = tuple(bool(item) for item in payload["continuous"])  # type: ignore[union-attr]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("预处理状态格式无效") from exc
        lengths = {len(names), len(medians), len(means), len(scales), len(continuous)}
        if len(lengths) != 1 or not names:
            raise ValueError("预处理状态维度不一致")
        if any(scale <= 0 or not np.isfinite(scale) for scale in scales):
            raise ValueError("预处理缩放参数无效")
        return cls(names, medians, means, scales, continuous)


class FeaturePreprocessor:
    """只在训练折上拟合的中位数填充与 Z-score 标准化器。"""

    def __init__(self, state: PreprocessorState | None = None) -> None:
        self._state = state

    @property
    def state(self) -> PreprocessorState:
        """返回已拟合状态。"""
        if self._state is None:
            raise RuntimeError("预处理器尚未拟合")
        return self._state

    def fit(self, X: np.ndarray, feature_names: tuple[str, ...]) -> FeaturePreprocessor:  # noqa: N803
        """仅用传入训练矩阵拟合参数。"""
        matrix = np.asarray(X, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != len(feature_names) or matrix.shape[0] == 0:
            raise ValueError("训练矩阵形状与特征名不一致或为空")
        medians = np.zeros(matrix.shape[1], dtype=np.float64)
        for index in range(matrix.shape[1]):
            finite = matrix[np.isfinite(matrix[:, index]), index]
            medians[index] = float(np.median(finite)) if finite.size else 0.0
        filled = np.where(np.isfinite(matrix), matrix, medians)
        continuous = np.asarray([not name.endswith("__available") for name in feature_names], dtype=bool)
        means = np.zeros(matrix.shape[1], dtype=np.float64)
        scales = np.ones(matrix.shape[1], dtype=np.float64)
        means[continuous] = np.mean(filled[:, continuous], axis=0)
        computed = np.std(filled[:, continuous], axis=0)
        scales[continuous] = np.where(computed < 1e-8, 1.0, computed)
        self._state = PreprocessorState(
            feature_names=tuple(feature_names),
            medians=tuple(float(item) for item in medians),
            means=tuple(float(item) for item in means),
            scales=tuple(float(item) for item in scales),
            continuous=tuple(bool(item) for item in continuous),
        )
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:  # noqa: N803
        """应用已持久化参数，不重新拟合。"""
        state = self.state
        matrix = np.asarray(X, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != len(state.feature_names):
            raise ValueError("推理矩阵维度与预处理器不一致")
        medians = np.asarray(state.medians)
        filled = np.where(np.isfinite(matrix), matrix, medians)
        continuous = np.asarray(state.continuous, dtype=bool)
        transformed = filled.copy()
        transformed[:, continuous] = (filled[:, continuous] - np.asarray(state.means)[continuous]) / np.asarray(
            state.scales
        )[continuous]
        return transformed

    def fit_transform(self, X: np.ndarray, feature_names: tuple[str, ...]) -> np.ndarray:  # noqa: N803
        """拟合训练矩阵并立即转换。"""
        return self.fit(X, feature_names).transform(X)
