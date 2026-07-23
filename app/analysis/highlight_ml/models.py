"""可移植高光模型、校准器与 JSON 产物。"""

from __future__ import annotations

import base64
import importlib.util
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import numpy as np

from app.analysis.highlight_ml.metrics import EvaluationReport
from app.analysis.highlight_ml.preprocessing import FeaturePreprocessor, PreprocessorState


def _sigmoid(values: np.ndarray) -> np.ndarray:
    """数值稳定的 Sigmoid。"""
    clipped = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


class ProbabilityModel(Protocol):
    """二分类概率模型协议。"""

    def predict_proba(self, X: np.ndarray) -> np.ndarray:  # noqa: N803
        """返回正类概率。"""


@dataclass(slots=True)
class NumpyLogisticModel:
    """带类别平衡和 L2 正则的 NumPy Logistic 回归。"""

    coefficients: np.ndarray | None = None
    intercept: float = 0.0

    def fit(
        self,
        X: np.ndarray,
        y: np.ndarray,
        *,
        learning_rate: float = 0.08,
        max_iter: int = 1200,
        l2: float = 0.01,
    ) -> NumpyLogisticModel:  # noqa: N803
        """拟合二分类模型。"""
        matrix = np.asarray(X, dtype=np.float64)
        labels = np.asarray(y, dtype=np.float64).reshape(-1)
        if matrix.ndim != 2 or matrix.shape[0] != labels.size or labels.size == 0:
            raise ValueError("Logistic 训练矩阵形状无效")
        if set(np.unique(labels)) != {0.0, 1.0}:
            raise ValueError("Logistic 训练集必须同时包含正负类")
        coefficients = np.zeros(matrix.shape[1], dtype=np.float64)
        intercept = 0.0
        positive_weight = labels.size / (2.0 * float(np.sum(labels)))
        negative_weight = labels.size / (2.0 * float(np.sum(1.0 - labels)))
        sample_weights = np.where(labels == 1.0, positive_weight, negative_weight)
        weight_sum = float(np.sum(sample_weights))
        for iteration in range(max_iter):
            probabilities = _sigmoid(matrix @ coefficients + intercept)
            residual = (probabilities - labels) * sample_weights
            gradient = matrix.T @ residual / weight_sum + l2 * coefficients
            intercept_gradient = float(np.sum(residual) / weight_sum)
            step = learning_rate / np.sqrt(1.0 + iteration * 0.01)
            coefficients -= step * gradient
            intercept -= step * intercept_gradient
        self.coefficients = coefficients
        self.intercept = intercept
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:  # noqa: N803
        """返回正类概率。"""
        if self.coefficients is None:
            raise RuntimeError("Logistic 模型尚未拟合")
        matrix = np.asarray(X, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != self.coefficients.size:
            raise ValueError("Logistic 推理矩阵维度不一致")
        return _sigmoid(matrix @ self.coefficients + self.intercept)


@dataclass(slots=True)
class PlattCalibrator:
    """在独立校准折上拟合的概率校准器。"""

    slope: float = 1.0
    intercept: float = 0.0
    fitted: bool = False

    def fit(self, probabilities: np.ndarray, y: np.ndarray) -> PlattCalibrator:
        """拟合概率 logit 到标签的二次 Logistic 映射。"""
        probs = np.clip(np.asarray(probabilities, dtype=np.float64).reshape(-1), 1e-6, 1 - 1e-6)
        labels = np.asarray(y, dtype=np.float64).reshape(-1)
        if probs.shape != labels.shape or set(np.unique(labels)) != {0.0, 1.0}:
            return self
        logits = np.log(probs / (1.0 - probs))
        slope = 1.0
        intercept = 0.0
        for iteration in range(600):
            calibrated = _sigmoid(slope * logits + intercept)
            residual = calibrated - labels
            step = 0.05 / np.sqrt(1.0 + iteration * 0.02)
            slope -= step * float(np.mean(residual * logits))
            intercept -= step * float(np.mean(residual))
        self.slope = slope
        self.intercept = intercept
        self.fitted = True
        return self

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        """校准概率；未拟合时保持原值。"""
        probs = np.clip(np.asarray(probabilities, dtype=np.float64), 1e-6, 1 - 1e-6)
        if not self.fitted:
            return probs
        logits = np.log(probs / (1.0 - probs))
        return _sigmoid(self.slope * logits + self.intercept)


class RuleBaselineModel:
    """使用现有可解释信号的固定规则基线。"""

    _SIGNALS = {
        "audio_prominence": 1.0,
        "transcript_laughter_rate": 4.0,
        "transcript_exclamation_rate": 2.0,
        "danmaku_heat_ratio": 0.35,
        "danmaku_exclamation_rate": 1.0,
        "aux_laughter_count": 0.25,
        "aux_applause_count": 0.25,
        "aux_surprise_count": 0.25,
    }

    def __init__(self, feature_names: tuple[str, ...]) -> None:
        self.feature_names = feature_names

    def predict_proba(self, X: np.ndarray) -> np.ndarray:  # noqa: N803
        """对可用信号加权平均，完全不读取标签。"""
        matrix = np.asarray(X, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != len(self.feature_names):
            raise ValueError("规则模型推理矩阵维度不一致")
        numerator = np.zeros(matrix.shape[0], dtype=np.float64)
        denominator = np.zeros(matrix.shape[0], dtype=np.float64)
        for name, weight in self._SIGNALS.items():
            if name not in self.feature_names:
                continue
            index = self.feature_names.index(name)
            available_name = f"{name}__available"
            available = (
                matrix[:, self.feature_names.index(available_name)] > 0.5
                if available_name in self.feature_names
                else np.isfinite(matrix[:, index])
            )
            signal = np.clip(np.nan_to_num(matrix[:, index], nan=0.0) * weight, 0.0, 1.0)
            numerator += np.where(available, signal, 0.0)
            denominator += available.astype(np.float64)
        return np.where(denominator > 0, numerator / np.maximum(denominator, 1.0), 0.0)


def xgboost_available() -> bool:
    """返回可选 XGBoost 依赖是否已安装。"""
    return importlib.util.find_spec("xgboost") is not None


def train_xgboost(X: np.ndarray, y: np.ndarray, *, seed: int) -> tuple[dict[str, object], ProbabilityModel]:  # noqa: N803
    """训练可选 XGBoost，并以稳定模型 JSON 二进制嵌入产物。"""
    if not xgboost_available():
        raise RuntimeError("XGBoost 未安装；请安装 bili-live-cut[highlight-ml]")
    import xgboost as xgb

    labels = np.asarray(y, dtype=np.int8)
    positive = max(int(labels.sum()), 1)
    negative = max(labels.size - positive, 1)
    training = xgb.DMatrix(np.asarray(X, dtype=np.float32), label=labels)
    booster = xgb.train(
        {
            "objective": "binary:logistic",
            "eval_metric": "logloss",
            "max_depth": 4,
            "eta": 0.04,
            "subsample": 0.85,
            "colsample_bytree": 0.85,
            "min_child_weight": 2,
            "scale_pos_weight": negative / positive,
            "seed": seed,
            "nthread": 1,
        },
        training,
        num_boost_round=240,
    )
    raw = bytes(booster.save_raw(raw_format="json"))
    state = {"booster_json_base64": base64.b64encode(raw).decode("ascii")}
    return state, _XGBoostRuntime(state)


class _XGBoostRuntime:
    """从产物内嵌 Booster JSON 运行推理。"""

    def __init__(self, state: dict[str, object]) -> None:
        if not xgboost_available():
            raise RuntimeError("加载 XGBoost 产物需要安装 bili-live-cut[highlight-ml]")
        import xgboost as xgb

        encoded = state.get("booster_json_base64")
        if not isinstance(encoded, str):
            raise ValueError("XGBoost 产物缺少 booster_json_base64")
        self._booster = xgb.Booster()
        self._booster.load_model(bytearray(base64.b64decode(encoded, validate=True)))

    def predict_proba(self, X: np.ndarray) -> np.ndarray:  # noqa: N803
        """返回 Booster 正类概率。"""
        import xgboost as xgb

        return np.asarray(self._booster.predict(xgb.DMatrix(np.asarray(X, dtype=np.float32))), dtype=np.float64)


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """训练、预处理、校准、Schema 与指标的单一原子产物。"""

    model_type: str
    schema_version: str
    schema_fingerprint: str
    feature_names: tuple[str, ...]
    model_state: dict[str, object]
    preprocessor_state: PreprocessorState | None
    calibrator: PlattCalibrator
    threshold: float
    report: EvaluationReport
    training_summary: dict[str, object]
    created_at: str
    format_version: int = 1

    def to_dict(self) -> dict[str, object]:
        """转换为规范 JSON 对象。"""
        return {
            "format_version": self.format_version,
            "model_type": self.model_type,
            "schema_version": self.schema_version,
            "schema_fingerprint": self.schema_fingerprint,
            "feature_names": list(self.feature_names),
            "model_state": self.model_state,
            "preprocessor": self.preprocessor_state.to_dict() if self.preprocessor_state is not None else None,
            "calibration": {
                "slope": self.calibrator.slope,
                "intercept": self.calibrator.intercept,
                "fitted": self.calibrator.fitted,
            },
            "threshold": self.threshold,
            "evaluation": self.report.to_dict(),
            "training_summary": self.training_summary,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> ModelArtifact:
        """从 JSON 对象恢复并验证模型产物。"""
        try:
            if int(payload["format_version"]) != 1:
                raise ValueError("不支持的模型产物版本")
            evaluation = payload["evaluation"]
            if not isinstance(evaluation, dict):
                raise TypeError
            raw_per_room = evaluation.get("per_room", {})
            if not isinstance(raw_per_room, dict):
                raise TypeError
            report = EvaluationReport(
                metrics={str(key): float(value) for key, value in evaluation["metrics"].items()},  # type: ignore[union-attr]
                per_room={
                    int(room_id): {str(key): float(value) for key, value in values.items()}
                    for room_id, values in raw_per_room.items()
                    if isinstance(values, dict)
                },
            )
            calibration = payload["calibration"]
            if not isinstance(calibration, dict):
                raise TypeError
            preprocessor_payload = payload.get("preprocessor")
            preprocessor = (
                PreprocessorState.from_dict(preprocessor_payload) if isinstance(preprocessor_payload, dict) else None
            )
            artifact = cls(
                model_type=str(payload["model_type"]),
                schema_version=str(payload["schema_version"]),
                schema_fingerprint=str(payload["schema_fingerprint"]),
                feature_names=tuple(str(item) for item in payload["feature_names"]),  # type: ignore[union-attr]
                model_state=dict(payload["model_state"]),  # type: ignore[arg-type]
                preprocessor_state=preprocessor,
                calibrator=PlattCalibrator(
                    slope=float(calibration["slope"]),
                    intercept=float(calibration["intercept"]),
                    fitted=bool(calibration["fitted"]),
                ),
                threshold=float(payload["threshold"]),
                report=report,
                training_summary=dict(payload["training_summary"]),  # type: ignore[arg-type]
                created_at=str(payload["created_at"]),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("模型产物格式无效") from exc
        if len(artifact.schema_fingerprint) != 64 or not 0 <= artifact.threshold <= 1:
            raise ValueError("模型产物的 Schema 指纹或阈值无效")
        if (
            artifact.preprocessor_state is not None
            and artifact.preprocessor_state.feature_names != artifact.feature_names
        ):
            raise ValueError("模型产物的预处理特征顺序不一致")
        return artifact

    @classmethod
    def create(
        cls,
        *,
        model_type: str,
        schema_version: str,
        schema_fingerprint: str,
        feature_names: tuple[str, ...],
        model_state: dict[str, object],
        preprocessor_state: PreprocessorState | None,
        calibrator: PlattCalibrator,
        threshold: float,
        report: EvaluationReport,
        training_summary: dict[str, object],
    ) -> ModelArtifact:
        """创建带 UTC 时间戳的模型产物。"""
        return cls(
            model_type=model_type,
            schema_version=schema_version,
            schema_fingerprint=schema_fingerprint,
            feature_names=feature_names,
            model_state=model_state,
            preprocessor_state=preprocessor_state,
            calibrator=calibrator,
            threshold=threshold,
            report=report,
            training_summary=training_summary,
            created_at=datetime.now(UTC).isoformat(),
        )


class ArtifactPredictor:
    """从单一模型产物执行训练一致的推理。"""

    def __init__(self, artifact: ModelArtifact) -> None:
        self.artifact = artifact
        if artifact.model_type == "logistic":
            coefficients = artifact.model_state.get("coefficients")
            intercept = artifact.model_state.get("intercept")
            if not isinstance(coefficients, list) or intercept is None:
                raise ValueError("Logistic 产物状态无效")
            self._model: ProbabilityModel = NumpyLogisticModel(
                coefficients=np.asarray(coefficients, dtype=np.float64),
                intercept=float(intercept),
            )
        elif artifact.model_type == "rules":
            self._model = RuleBaselineModel(artifact.feature_names)
        elif artifact.model_type == "xgboost":
            self._model = _XGBoostRuntime(artifact.model_state)
        else:
            raise ValueError(f"未知模型类型: {artifact.model_type}")
        self._preprocessor = (
            FeaturePreprocessor(artifact.preprocessor_state) if artifact.preprocessor_state is not None else None
        )
        if artifact.model_type == "logistic":
            coefficients = artifact.model_state.get("coefficients")
            if not isinstance(coefficients, list) or len(coefficients) != len(artifact.feature_names):
                raise ValueError("Logistic 系数维度与特征 Schema 不一致")

    def predict_proba(self, X: np.ndarray) -> np.ndarray:  # noqa: N803
        """校验维度、应用预处理与概率校准。"""
        matrix = np.asarray(X, dtype=np.float64)
        if matrix.ndim != 2 or matrix.shape[1] != len(self.artifact.feature_names):
            raise ValueError("模型输入维度与产物 Schema 不一致")
        prepared = self._preprocessor.transform(matrix) if self._preprocessor is not None else matrix
        return self.artifact.calibrator.transform(self._model.predict_proba(prepared))
