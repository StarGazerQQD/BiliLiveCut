"""按录制会话和时间切分的高光模型训练与比较。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from app.analysis.highlight_ml.metrics import EvaluationReport, evaluate_probabilities, select_f1_threshold
from app.analysis.highlight_ml.models import (
    ModelArtifact,
    NumpyLogisticModel,
    PlattCalibrator,
    RuleBaselineModel,
    train_xgboost,
    xgboost_available,
)
from app.analysis.highlight_ml.preprocessing import FeaturePreprocessor
from app.analysis.highlight_ml.types import DatasetBundle, LabeledSample


@dataclass(frozen=True, slots=True)
class SplitIndices:
    """按会话隔离的训练、校准与测试索引。"""

    train: np.ndarray
    calibration: np.ndarray
    test: np.ndarray
    train_sessions: tuple[int, ...]
    calibration_sessions: tuple[int, ...]
    test_sessions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    """训练与留出策略。"""

    test_fraction: float = 0.2
    calibration_fraction: float = 0.2
    audit_fraction: float = 0.2
    min_samples: int = 20
    min_positive: int = 5
    seed: int = 42
    include_xgboost: bool = True


@dataclass(frozen=True, slots=True)
class CandidateModelResult:
    """单个候选模型的产物或不可用原因。"""

    artifact: ModelArtifact | None
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TrainingComparison:
    """公平留出集上的候选模型比较与推荐结果。"""

    candidates: dict[str, CandidateModelResult]
    selected_model_type: str
    split: SplitIndices

    @property
    def selected_artifact(self) -> ModelArtifact:
        """返回推荐模型产物。"""
        artifact = self.candidates[self.selected_model_type].artifact
        if artifact is None:
            raise RuntimeError("推荐模型产物不可用")
        return artifact


def _ordered_session_indices(samples: tuple[LabeledSample, ...]) -> list[tuple[int, np.ndarray]]:
    by_session: dict[int, list[int]] = {}
    starts: dict[int, datetime] = {}
    for index, sample in enumerate(samples):
        by_session.setdefault(sample.session_id, []).append(index)
        starts[sample.session_id] = min(starts.get(sample.session_id, sample.segment_start_ts), sample.segment_start_ts)
    return [
        (session_id, np.asarray(by_session[session_id], dtype=np.int64))
        for session_id in sorted(by_session, key=lambda item: (starts[item], item))
    ]


def group_time_split(
    samples: tuple[LabeledSample, ...],
    *,
    test_fraction: float,
    calibration_fraction: float,
) -> SplitIndices:
    """按会话最早片段时间排序，整组切出校准集和最终测试集。"""
    if not 0 < test_fraction < 0.5 or not 0 <= calibration_fraction < 0.5:
        raise ValueError("切分比例无效")
    groups = _ordered_session_indices(samples)
    if len(groups) < 2:
        raise ValueError("至少需要两个录制会话才能无泄漏切分")
    test_group_count = max(1, int(np.ceil(len(groups) * test_fraction)))
    test_groups = groups[-test_group_count:]
    remaining = groups[:-test_group_count]
    if not remaining:
        raise ValueError("训练会话为空")
    calibration_group_count = 0
    if calibration_fraction > 0 and len(remaining) >= 2:
        calibration_group_count = max(1, int(np.ceil(len(remaining) * calibration_fraction)))
        calibration_group_count = min(calibration_group_count, len(remaining) - 1)
    calibration_groups = remaining[-calibration_group_count:] if calibration_group_count else []
    train_groups = remaining[:-calibration_group_count] if calibration_group_count else remaining

    def combine(selected: list[tuple[int, np.ndarray]]) -> np.ndarray:
        return np.concatenate([indices for _, indices in selected]) if selected else np.empty(0, dtype=np.int64)

    return SplitIndices(
        train=combine(train_groups),
        calibration=combine(calibration_groups),
        test=combine(test_groups),
        train_sessions=tuple(session_id for session_id, _ in train_groups),
        calibration_sessions=tuple(session_id for session_id, _ in calibration_groups),
        test_sessions=tuple(session_id for session_id, _ in test_groups),
    )


def _calibrate_and_evaluate(
    base_probabilities: tuple[np.ndarray, np.ndarray],
    y_calibration: np.ndarray,
    y_test: np.ndarray,
    room_test: np.ndarray,
    *,
    audit_fraction: float,
) -> tuple[PlattCalibrator, float, EvaluationReport]:
    calibration_probs, test_probs = base_probabilities
    calibrator = PlattCalibrator().fit(calibration_probs, y_calibration) if y_calibration.size else PlattCalibrator()
    calibrated_calibration = calibrator.transform(calibration_probs)
    threshold = select_f1_threshold(y_calibration, calibrated_calibration) if y_calibration.size else 0.5
    report = evaluate_probabilities(
        y_test,
        calibrator.transform(test_probs),
        room_ids=room_test,
        threshold=threshold,
        audit_fraction=audit_fraction,
    )
    return calibrator, threshold, report


def _summary(bundle: DatasetBundle, split: SplitIndices) -> dict[str, object]:
    return {
        "n_samples": len(bundle.samples),
        "n_positive": int(bundle.y.sum()),
        "train_samples": int(split.train.size),
        "calibration_samples": int(split.calibration.size),
        "test_samples": int(split.test.size),
        "train_sessions": list(split.train_sessions),
        "calibration_sessions": list(split.calibration_sessions),
        "test_sessions": list(split.test_sessions),
    }


def train_candidate_models(
    bundle: DatasetBundle,
    config: TrainingConfig | None = None,
) -> TrainingComparison:
    """在同一无泄漏留出集上训练并比较规则、Logistic 和可选 XGBoost。"""
    config = config or TrainingConfig()
    labels = bundle.y
    if labels.size < config.min_samples or int(labels.sum()) < config.min_positive:
        raise ValueError("明确人工标签数量不足，不能训练")
    if int(labels.sum()) == labels.size:
        raise ValueError("训练数据缺少负类")
    split = group_time_split(
        bundle.samples,
        test_fraction=config.test_fraction,
        calibration_fraction=config.calibration_fraction,
    )
    for name, indices in (("训练", split.train), ("测试", split.test)):
        if set(np.unique(labels[indices])) != {0, 1}:
            raise ValueError(f"{name}折必须同时包含正负类；请增加跨会话标注")

    matrix = bundle.X
    y_train = labels[split.train]
    y_calibration = labels[split.calibration]
    y_test = labels[split.test]
    room_test = np.asarray([bundle.samples[index].room_id for index in split.test], dtype=np.int64)
    summary = _summary(bundle, split)
    candidates: dict[str, CandidateModelResult] = {}

    rules = RuleBaselineModel(bundle.feature_names)
    rules_calibration = rules.predict_proba(matrix[split.calibration])
    rules_test = rules.predict_proba(matrix[split.test])
    calibrator, threshold, report = _calibrate_and_evaluate(
        (rules_calibration, rules_test),
        y_calibration,
        y_test,
        room_test,
        audit_fraction=config.audit_fraction,
    )
    candidates["rules"] = CandidateModelResult(
        ModelArtifact.create(
            model_type="rules",
            schema_version=bundle.schema_version,
            schema_fingerprint=bundle.schema_fingerprint,
            feature_names=bundle.feature_names,
            model_state={},
            preprocessor_state=None,
            calibrator=calibrator,
            threshold=threshold,
            report=report,
            training_summary=summary,
        )
    )

    preprocessor = FeaturePreprocessor()
    train_processed = preprocessor.fit_transform(matrix[split.train], bundle.feature_names)
    logistic = NumpyLogisticModel().fit(train_processed, y_train)
    logistic_calibration = logistic.predict_proba(preprocessor.transform(matrix[split.calibration]))
    logistic_test = logistic.predict_proba(preprocessor.transform(matrix[split.test]))
    calibrator, threshold, report = _calibrate_and_evaluate(
        (logistic_calibration, logistic_test),
        y_calibration,
        y_test,
        room_test,
        audit_fraction=config.audit_fraction,
    )
    if logistic.coefficients is None:
        raise RuntimeError("Logistic 训练未生成系数")
    candidates["logistic"] = CandidateModelResult(
        ModelArtifact.create(
            model_type="logistic",
            schema_version=bundle.schema_version,
            schema_fingerprint=bundle.schema_fingerprint,
            feature_names=bundle.feature_names,
            model_state={
                "coefficients": [float(item) for item in logistic.coefficients],
                "intercept": logistic.intercept,
            },
            preprocessor_state=preprocessor.state,
            calibrator=calibrator,
            threshold=threshold,
            report=report,
            training_summary=summary,
        )
    )

    if config.include_xgboost:
        if xgboost_available():
            state, xgboost_model = train_xgboost(matrix[split.train], y_train, seed=config.seed)
            calibrator, threshold, report = _calibrate_and_evaluate(
                (
                    xgboost_model.predict_proba(matrix[split.calibration]),
                    xgboost_model.predict_proba(matrix[split.test]),
                ),
                y_calibration,
                y_test,
                room_test,
                audit_fraction=config.audit_fraction,
            )
            candidates["xgboost"] = CandidateModelResult(
                ModelArtifact.create(
                    model_type="xgboost",
                    schema_version=bundle.schema_version,
                    schema_fingerprint=bundle.schema_fingerprint,
                    feature_names=bundle.feature_names,
                    model_state=state,
                    preprocessor_state=None,
                    calibrator=calibrator,
                    threshold=threshold,
                    report=report,
                    training_summary=summary,
                )
            )
        else:
            candidates["xgboost"] = CandidateModelResult(
                artifact=None,
                unavailable_reason="未安装 bili-live-cut[highlight-ml]",
            )

    available = {
        model_type: result.artifact for model_type, result in candidates.items() if result.artifact is not None
    }
    selected = max(
        available,
        key=lambda model_type: (
            available[model_type].report.metrics["pr_auc"],  # type: ignore[union-attr]
            -available[model_type].report.metrics["brier"],  # type: ignore[union-attr]
            model_type == "logistic",
        ),
    )
    return TrainingComparison(candidates=candidates, selected_model_type=selected, split=split)
