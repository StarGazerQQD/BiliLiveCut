"""基于真实训练分布的概率、特征和缺失率漂移检测。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from app.analysis.highlight_ml.registry import _atomic_write, _canonical_json


def _histogram(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    finite = values[np.isfinite(values)]
    counts, _ = np.histogram(finite, bins=edges)
    total = int(counts.sum())
    return counts.astype(np.float64) / total if total else np.zeros(counts.size, dtype=np.float64)


def _quantile_edges(values: np.ndarray, n_bins: int) -> np.ndarray:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.asarray([-1e308, 1e308], dtype=np.float64)
    internal = np.unique(np.quantile(finite, np.linspace(0.0, 1.0, n_bins + 1)[1:-1]))
    return np.concatenate(([-1e308], internal, [1e308])).astype(np.float64)


def _psi(expected: np.ndarray, actual: np.ndarray) -> float:
    epsilon = 1e-6
    exp = np.clip(expected, epsilon, None)
    act = np.clip(actual, epsilon, None)
    return float(np.sum((act - exp) * np.log(act / exp)))


@dataclass(frozen=True, slots=True)
class FeatureBaseline:
    """单特征真实训练分布摘要。"""

    name: str
    edges: tuple[float, ...]
    proportions: tuple[float, ...]
    mean: float
    std: float
    missing_rate: float


@dataclass(frozen=True, slots=True)
class DriftBaseline:
    """训练特征与预测概率的可复算漂移基线。"""

    schema_fingerprint: str
    n_samples: int
    probability: FeatureBaseline
    features: tuple[FeatureBaseline, ...]
    created_at: str

    @classmethod
    def fit(
        cls,
        X: np.ndarray,
        probabilities: np.ndarray,
        feature_names: tuple[str, ...],
        *,
        schema_fingerprint: str,
        n_bins: int = 10,
    ) -> DriftBaseline:  # noqa: N803
        """从真实训练矩阵拟合分位箱、均值方差和缺失率。"""
        matrix = np.asarray(X, dtype=np.float64)
        probs = np.asarray(probabilities, dtype=np.float64).reshape(-1)
        if matrix.ndim != 2 or matrix.shape != (probs.size, len(feature_names)) or probs.size == 0:
            raise ValueError("漂移基线矩阵形状无效")

        def summarize(name: str, values: np.ndarray) -> FeatureBaseline:
            finite = values[np.isfinite(values)]
            edges = _quantile_edges(values, n_bins)
            return FeatureBaseline(
                name=name,
                edges=tuple(float(item) for item in edges),
                proportions=tuple(float(item) for item in _histogram(values, edges)),
                mean=float(np.mean(finite)) if finite.size else 0.0,
                std=float(np.std(finite)) if finite.size else 0.0,
                missing_rate=float(1.0 - finite.size / values.size),
            )

        return cls(
            schema_fingerprint=schema_fingerprint,
            n_samples=probs.size,
            probability=summarize("__probability__", probs),
            features=tuple(summarize(name, matrix[:, index]) for index, name in enumerate(feature_names)),
            created_at=datetime.now(UTC).isoformat(),
        )

    def to_dict(self) -> dict[str, object]:
        """转换为可持久化 JSON 对象。"""

        def encode(item: FeatureBaseline) -> dict[str, object]:
            return {
                "name": item.name,
                "edges": list(item.edges),
                "proportions": list(item.proportions),
                "mean": item.mean,
                "std": item.std,
                "missing_rate": item.missing_rate,
            }

        return {
            "format_version": 1,
            "schema_fingerprint": self.schema_fingerprint,
            "n_samples": self.n_samples,
            "probability": encode(self.probability),
            "features": [encode(item) for item in self.features],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> DriftBaseline:
        """从 JSON 对象恢复漂移基线。"""

        def decode(item: object) -> FeatureBaseline:
            if not isinstance(item, dict):
                raise TypeError
            return FeatureBaseline(
                name=str(item["name"]),
                edges=tuple(float(value) for value in item["edges"]),  # type: ignore[union-attr]
                proportions=tuple(float(value) for value in item["proportions"]),  # type: ignore[union-attr]
                mean=float(item["mean"]),
                std=float(item["std"]),
                missing_rate=float(item["missing_rate"]),
            )

        try:
            if payload.get("format_version") != 1:
                raise ValueError
            features = payload["features"]
            if not isinstance(features, list):
                raise TypeError
            return cls(
                schema_fingerprint=str(payload["schema_fingerprint"]),
                n_samples=int(payload["n_samples"]),
                probability=decode(payload["probability"]),
                features=tuple(decode(item) for item in features),
                created_at=str(payload["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("漂移基线格式无效") from exc

    def save(self, path: str | Path) -> None:
        """原子保存完整基线。"""
        _atomic_write(Path(path), _canonical_json(self.to_dict()))

    @classmethod
    def load(cls, path: str | Path) -> DriftBaseline:
        """加载完整基线，损坏时失败关闭。"""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"漂移基线损坏: {path}") from exc
        return cls.from_dict(payload)


@dataclass(frozen=True, slots=True)
class FeatureDrift:
    """单特征漂移明细。"""

    name: str
    psi: float
    standardized_mean_shift: float
    missing_rate_shift: float


@dataclass(frozen=True, slots=True)
class DriftReport:
    """近期真实样本相对训练基线的漂移报告。"""

    probability_psi: float
    status: str
    is_drifted: bool
    n_recent: int
    shifted_features: tuple[FeatureDrift, ...]
    checked_at: str


class DriftDetector:
    """使用 PSI、均值偏移和缺失率变化检测漂移。"""

    def __init__(
        self,
        baseline: DriftBaseline,
        *,
        warning_psi: float = 0.1,
        alert_psi: float = 0.25,
        mean_shift_threshold: float = 1.0,
        missing_shift_threshold: float = 0.2,
        min_recent_samples: int = 20,
    ) -> None:
        self.baseline = baseline
        self.warning_psi = warning_psi
        self.alert_psi = alert_psi
        self.mean_shift_threshold = mean_shift_threshold
        self.missing_shift_threshold = missing_shift_threshold
        self.min_recent_samples = min_recent_samples

    def check(
        self,
        X: np.ndarray,
        probabilities: np.ndarray,
        *,
        schema_fingerprint: str,
    ) -> DriftReport:  # noqa: N803
        """检查真实近期矩阵；样本不足或 Schema 不同会明确失败。"""
        matrix = np.asarray(X, dtype=np.float64)
        probs = np.asarray(probabilities, dtype=np.float64).reshape(-1)
        if schema_fingerprint != self.baseline.schema_fingerprint:
            raise ValueError("漂移检查 Schema 与基线不一致")
        if matrix.ndim != 2 or matrix.shape != (probs.size, len(self.baseline.features)):
            raise ValueError("近期漂移矩阵形状无效")
        if probs.size < self.min_recent_samples:
            raise ValueError(f"近期样本不足，至少需要 {self.min_recent_samples} 条")
        probability_actual = _histogram(probs, np.asarray(self.baseline.probability.edges))
        probability_psi = _psi(np.asarray(self.baseline.probability.proportions), probability_actual)
        details: list[FeatureDrift] = []
        for index, baseline in enumerate(self.baseline.features):
            values = matrix[:, index]
            finite = values[np.isfinite(values)]
            actual = _histogram(values, np.asarray(baseline.edges))
            psi = _psi(np.asarray(baseline.proportions), actual)
            mean = float(np.mean(finite)) if finite.size else 0.0
            mean_shift = abs(mean - baseline.mean) / max(baseline.std, 1e-8)
            missing_shift = abs((1.0 - finite.size / values.size) - baseline.missing_rate)
            if (
                psi >= self.warning_psi
                or mean_shift >= self.mean_shift_threshold
                or missing_shift >= self.missing_shift_threshold
            ):
                details.append(
                    FeatureDrift(
                        name=baseline.name,
                        psi=psi,
                        standardized_mean_shift=mean_shift,
                        missing_rate_shift=missing_shift,
                    )
                )
        details.sort(key=lambda item: (item.psi, item.standardized_mean_shift, item.missing_rate_shift), reverse=True)
        is_alert = probability_psi >= self.alert_psi or any(
            item.psi >= self.alert_psi
            or item.standardized_mean_shift >= self.mean_shift_threshold * 2
            or item.missing_rate_shift >= self.missing_shift_threshold * 2
            for item in details
        )
        is_warning = probability_psi >= self.warning_psi or bool(details)
        return DriftReport(
            probability_psi=probability_psi,
            status="alert" if is_alert else ("warning" if is_warning else "ok"),
            is_drifted=is_alert,
            n_recent=probs.size,
            shifted_features=tuple(details[:20]),
            checked_at=datetime.now(UTC).isoformat(),
        )
