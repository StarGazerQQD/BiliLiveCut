"""训练注册、盲审附件与线上漂移闭环测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from app.analysis.highlight_ml import operations
from app.analysis.highlight_ml.models import ArtifactPredictor
from app.analysis.highlight_ml.registry import ModelRegistry
from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA
from app.analysis.highlight_ml.training import TrainingConfig
from app.analysis.highlight_ml.types import BlindReviewItem
from app.db.models import SystemLog
from app.db.session import get_session
from tests.unit.test_highlight_ml_training import _synthetic_bundle

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_train_register_blind_review_and_drift(
    temp_db: None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """一次训练原子发布模型、基线和盲审队列，并可检查真实日志漂移。"""
    now = datetime.now(UTC)
    bundle = replace(
        _synthetic_bundle(),
        blind_review_queue=(
            BlindReviewItem(
                segment_id=999,
                session_id=99,
                start_ts=now,
                end_ts=now + timedelta(seconds=60),
            ),
        ),
    )
    monkeypatch.setattr(operations, "build_labeled_dataset", lambda *_args, **_kwargs: bundle)
    registry_root = tmp_path / "models"
    with get_session() as db:
        summary = operations.train_and_register(
            db,
            registry_root=registry_root,
            config=TrainingConfig(include_xgboost=False),
            blind_review_limit=1,
        )
    assert summary.role == "champion"
    assert summary.blind_review_count == 1

    registry = ModelRegistry(registry_root, schema_fingerprint=DEFAULT_FEATURE_SCHEMA.fingerprint)
    blind_payload = json.loads(registry.load_attachment(summary.version, "blind-review.json"))
    assert blind_payload["items"][0]["segment_id"] == 999
    artifact = registry.load_artifact(summary.version)
    probabilities = ArtifactPredictor(artifact).predict_proba(bundle.X[:10])

    with get_session() as db:
        for sample, probability in zip(bundle.samples[:10], probabilities, strict=True):
            db.add(
                SystemLog(
                    module="highlight_ml",
                    event="highlight_ml_prediction",
                    message="prediction",
                    context_json=json.dumps(
                        {
                            "champion_version": summary.version,
                            "champion_probability": float(probability),
                            "schema_fingerprint": bundle.schema_fingerprint,
                            "feature_values": sample.features.values,
                        }
                    ),
                )
            )
    with get_session() as db:
        report = operations.check_champion_drift(
            db,
            registry_root=registry_root,
            limit=10,
            min_recent_samples=5,
        )
    assert report["champion_version"] == summary.version
    assert report["n_recent"] == 10
    assert report["status"] in {"ok", "warning", "alert"}
