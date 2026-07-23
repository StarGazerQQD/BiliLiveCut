"""从真实数据库训练、原子注册、盲审导出与线上漂移检查。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sqlmodel import Session, select

from app.analysis.highlight_ml.context import load_file_audio_snapshot
from app.analysis.highlight_ml.dataset import build_labeled_dataset
from app.analysis.highlight_ml.drift import DriftBaseline, DriftDetector
from app.analysis.highlight_ml.models import ArtifactPredictor
from app.analysis.highlight_ml.registry import ModelRegistry, _canonical_json
from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA
from app.analysis.highlight_ml.training import TrainingConfig, train_candidate_models
from app.analysis.highlight_ml.types import DatasetBundle
from app.db.models import SystemLog

_DRIFT_BASELINE_NAME = "drift-baseline.json"
_BLIND_REVIEW_NAME = "blind-review.json"


@dataclass(frozen=True, slots=True)
class TrainingRunSummary:
    """一次可审计的训练与注册结果。"""

    version: int
    role: str
    selected_model_type: str
    metrics: dict[str, float]
    n_samples: int
    n_positive: int
    blind_review_count: int
    registry_generation: int

    def to_dict(self) -> dict[str, object]:
        """返回 CLI/Web 可序列化摘要。"""
        return asdict(self)


def _blind_review_payload(bundle: DatasetBundle) -> dict[str, object]:
    queue = bundle.blind_review_queue
    return {
        "format_version": 1,
        "schema_version": bundle.schema_version,
        "schema_fingerprint": bundle.schema_fingerprint,
        "created_at": datetime.now(UTC).isoformat(),
        "items": [
            {
                "segment_id": item.segment_id,
                "session_id": item.session_id,
                "start_ts": item.start_ts.isoformat(),
                "end_ts": item.end_ts.isoformat(),
            }
            for item in queue
        ],
    }


def train_and_register(
    db: Session,
    *,
    registry_root: str | Path,
    config: TrainingConfig,
    as_shadow: bool = True,
    blind_review_limit: int = 100,
    blind_review_seed: int = 42,
) -> TrainingRunSummary:
    """构建真实标签集、比较候选模型并原子注册全部附件。"""
    if blind_review_limit < 0:
        raise ValueError("盲审队列数量不能为负数")
    bundle = build_labeled_dataset(
        db,
        audio_loader=load_file_audio_snapshot,
        blind_review_limit=blind_review_limit,
        blind_review_seed=blind_review_seed,
    )
    comparison = train_candidate_models(bundle, config)
    artifact = comparison.selected_artifact
    train_matrix = bundle.X[comparison.split.train]
    train_probabilities = ArtifactPredictor(artifact).predict_proba(train_matrix)
    baseline = DriftBaseline.fit(
        train_matrix,
        train_probabilities,
        bundle.feature_names,
        schema_fingerprint=bundle.schema_fingerprint,
    )
    attachments = {
        _DRIFT_BASELINE_NAME: _canonical_json(baseline.to_dict()),
        _BLIND_REVIEW_NAME: _canonical_json(_blind_review_payload(bundle)),
    }
    registry = ModelRegistry(registry_root, schema_fingerprint=bundle.schema_fingerprint)
    entry = registry.register(artifact, as_shadow=as_shadow, attachments=attachments)
    snapshot = registry.snapshot()
    if snapshot.champion_version == entry.version:
        role = "champion"
    elif snapshot.shadow_version == entry.version:
        role = "shadow"
    else:
        role = "unassigned"
    return TrainingRunSummary(
        version=entry.version,
        role=role,
        selected_model_type=comparison.selected_model_type,
        metrics=entry.metrics,
        n_samples=entry.n_samples,
        n_positive=entry.n_positive,
        blind_review_count=len(bundle.blind_review_queue),
        registry_generation=snapshot.generation,
    )


def _recent_prediction_matrix(
    db: Session,
    *,
    champion_version: int,
    limit: int,
) -> tuple[np.ndarray, np.ndarray]:
    rows = db.exec(
        select(SystemLog)
        .where(SystemLog.module == "highlight_ml", SystemLog.event == "highlight_ml_prediction")
        .order_by(SystemLog.created_at.desc())
        .limit(max(limit * 5, limit))
    ).all()
    vectors: list[np.ndarray] = []
    probabilities: list[float] = []
    for row in rows:
        try:
            context = json.loads(row.context_json or "{}")
            if not isinstance(context, dict) or int(context.get("champion_version", -1)) != champion_version:
                continue
            if context.get("schema_fingerprint") != DEFAULT_FEATURE_SCHEMA.fingerprint:
                continue
            raw_values = context.get("feature_values")
            probability = float(context["champion_probability"])
            if not isinstance(raw_values, dict) or not np.isfinite(probability):
                continue
            values = {
                str(name): None if value is None else float(value)
                for name, value in raw_values.items()
                if isinstance(name, str)
            }
            vectors.append(DEFAULT_FEATURE_SCHEMA.vectorize(values))
            probabilities.append(probability)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(vectors) >= limit:
            break
    if not vectors:
        return np.empty((0, len(DEFAULT_FEATURE_SCHEMA.feature_names))), np.empty(0)
    return np.vstack(vectors), np.asarray(probabilities, dtype=np.float64)


def check_champion_drift(
    db: Session,
    *,
    registry_root: str | Path,
    limit: int = 500,
    min_recent_samples: int = 20,
) -> dict[str, object]:
    """用当前 Champion 的原子训练基线检查近期线上预测。"""
    if limit < 1 or min_recent_samples < 1:
        raise ValueError("漂移检查样本参数必须为正数")
    registry = ModelRegistry(registry_root, schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    snapshot = registry.snapshot()
    if snapshot.champion_version is None:
        raise RuntimeError("模型注册表尚无 Champion")
    baseline_payload = json.loads(registry.load_attachment(snapshot.champion_version, _DRIFT_BASELINE_NAME))
    if not isinstance(baseline_payload, dict):
        raise RuntimeError("Champion 漂移基线格式无效")
    baseline = DriftBaseline.from_dict(baseline_payload)
    matrix, probabilities = _recent_prediction_matrix(
        db,
        champion_version=snapshot.champion_version,
        limit=limit,
    )
    report = DriftDetector(baseline, min_recent_samples=min_recent_samples).check(
        matrix,
        probabilities,
        schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint,
    )
    result = asdict(report)
    result["champion_version"] = snapshot.champion_version
    result["baseline_samples"] = baseline.n_samples
    return result
