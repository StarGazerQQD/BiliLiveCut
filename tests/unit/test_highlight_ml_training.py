"""高光模型预处理、评估和训练比较测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np

from app.analysis.highlight_ml.metrics import evaluate_probabilities
from app.analysis.highlight_ml.models import ArtifactPredictor, ModelArtifact
from app.analysis.highlight_ml.preprocessing import FeaturePreprocessor, PreprocessorState
from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA
from app.analysis.highlight_ml.training import TrainingConfig, group_time_split, train_candidate_models
from app.analysis.highlight_ml.types import DatasetBundle, FeatureRecord, LabeledSample


def _synthetic_bundle(session_count: int = 8, samples_per_session: int = 6) -> DatasetBundle:
    schema = DEFAULT_FEATURE_SCHEMA
    samples: list[LabeledSample] = []
    base = datetime(2026, 1, 1)
    sample_index = 0
    for session_id in range(1, session_count + 1):
        for offset in range(samples_per_session):
            label = offset % 2
            values = {spec.name: 0.0 for spec in schema.specs}
            values.update(
                {
                    "duration_s": 60.0,
                    "session_elapsed_s": float(offset * 60),
                    "audio_prominence": 0.9 if label else 0.1,
                    "transcript_laughter_rate": 0.3 if label else 0.0,
                    "danmaku_heat_ratio": 3.0 if label else 0.5,
                }
            )
            record = FeatureRecord(
                segment_id=sample_index + 1,
                schema_version=schema.version,
                schema_fingerprint=schema.fingerprint,
                values=values,
            )
            samples.append(
                LabeledSample(
                    sample_id=f"candidate:{sample_index + 1}",
                    segment_id=sample_index + 1,
                    session_id=session_id,
                    room_id=100 + session_id % 2,
                    segment_start_ts=base + timedelta(days=session_id, minutes=offset),
                    label=label,
                    label_source="test",
                    observed_at=base + timedelta(days=session_id, hours=1),
                    features=record,
                )
            )
            sample_index += 1
    return DatasetBundle(
        schema_version=schema.version,
        schema_fingerprint=schema.fingerprint,
        feature_names=schema.feature_names,
        samples=tuple(samples),
        blind_review_queue=(),
    )


def test_preprocessor_fits_training_only_and_round_trips() -> None:
    """验证集极值不改变训练折中位数/均值，持久化后结果一致。"""
    train = np.asarray([[1.0, 1.0], [3.0, 0.0], [np.nan, 0.0]])
    validation = np.asarray([[1_000_000.0, 1.0]])
    preprocessor = FeaturePreprocessor().fit(train, ("value", "value__available"))
    state = preprocessor.state

    assert state.medians[0] == 2.0
    assert state.means[0] == 2.0
    assert state.means[1] == 0.0
    restored = FeaturePreprocessor(PreprocessorState.from_dict(state.to_dict()))
    np.testing.assert_allclose(preprocessor.transform(validation), restored.transform(validation))
    assert restored.transform(validation)[0, 1] == 1.0


def test_metrics_reward_ranking_and_report_room_macro() -> None:
    """完美排序得到满 PR/ROC-AUC，并输出审核预算与房间宏指标。"""
    labels = np.asarray([0, 1, 0, 1, 0, 1, 0, 1])
    probabilities = np.asarray([0.1, 0.9, 0.2, 0.8, 0.05, 0.95, 0.3, 0.7])
    rooms = np.asarray([1, 1, 1, 1, 2, 2, 2, 2])

    report = evaluate_probabilities(labels, probabilities, room_ids=rooms, audit_fraction=0.5)

    assert report.metrics["pr_auc"] == 1.0
    assert report.metrics["roc_auc"] == 1.0
    assert report.metrics["recall_at_audit_fraction"] == 1.0
    assert report.metrics["room_macro_pr_auc"] == 1.0
    assert set(report.per_room) == {1, 2}


def test_group_time_split_keeps_sessions_disjoint_and_ordered() -> None:
    """同一录制会话不会跨折，最终测试集来自时间最新会话。"""
    bundle = _synthetic_bundle()
    split = group_time_split(bundle.samples, test_fraction=0.25, calibration_fraction=0.25)

    assert set(split.train_sessions).isdisjoint(split.calibration_sessions)
    assert set(split.train_sessions).isdisjoint(split.test_sessions)
    assert set(split.calibration_sessions).isdisjoint(split.test_sessions)
    assert split.test_sessions == (7, 8)
    assert max(split.train_sessions) < min(split.calibration_sessions) < min(split.test_sessions)


def test_training_artifact_contains_preprocessing_and_round_trips_predictions() -> None:
    """训练产物封装预处理、模型、校准与指标，JSON 往返预测一致。"""
    bundle = _synthetic_bundle()
    comparison = train_candidate_models(
        bundle,
        TrainingConfig(test_fraction=0.25, calibration_fraction=0.25, include_xgboost=False),
    )
    logistic = comparison.candidates["logistic"].artifact
    rules = comparison.candidates["rules"].artifact

    assert logistic is not None and logistic.preprocessor_state is not None
    assert rules is not None
    assert comparison.selected_model_type in {"rules", "logistic"}
    assert logistic.report.metrics["pr_auc"] >= 0.9
    payload = json.loads(json.dumps(logistic.to_dict()))
    restored = ModelArtifact.from_dict(payload)
    original_probabilities = ArtifactPredictor(logistic).predict_proba(bundle.X[:5])
    restored_probabilities = ArtifactPredictor(restored).predict_proba(bundle.X[:5])
    np.testing.assert_allclose(original_probabilities, restored_probabilities)
