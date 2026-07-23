"""真实训练分布漂移检测测试。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.analysis.highlight_ml.drift import DriftBaseline, DriftDetector


def test_drift_baseline_persists_full_distribution_and_detects_shift(tmp_path: Path) -> None:
    """相同样本无告警，整体平移与概率变化触发告警，保存加载结果一致。"""
    rng = np.random.default_rng(42)
    matrix = rng.normal(0.0, 1.0, size=(200, 3))
    matrix[::10, 1] = np.nan
    probabilities = np.clip(0.5 + matrix[:, 0] * 0.1, 0.01, 0.99)
    fingerprint = "a" * 64
    baseline = DriftBaseline.fit(
        matrix,
        probabilities,
        ("one", "two", "three"),
        schema_fingerprint=fingerprint,
    )
    path = tmp_path / "drift.json"
    baseline.save(path)
    restored = DriftBaseline.load(path)
    detector = DriftDetector(restored, min_recent_samples=20)

    stable = detector.check(matrix.copy(), probabilities.copy(), schema_fingerprint=fingerprint)
    shifted = detector.check(matrix + 5.0, np.full(200, 0.95), schema_fingerprint=fingerprint)

    assert stable.status == "ok"
    assert stable.is_drifted is False
    assert shifted.status == "alert"
    assert shifted.is_drifted is True
    assert shifted.probability_psi >= 0.25
    assert shifted.shifted_features
    assert len(restored.probability.proportions) > 1


def test_drift_rejects_schema_mismatch_and_small_windows() -> None:
    """Schema 漂移和样本不足都不能伪装成正常状态。"""
    matrix = np.arange(60, dtype=np.float64).reshape(20, 3)
    probabilities = np.linspace(0.1, 0.9, 20)
    baseline = DriftBaseline.fit(matrix, probabilities, ("a", "b", "c"), schema_fingerprint="b" * 64)
    detector = DriftDetector(baseline, min_recent_samples=20)

    with pytest.raises(ValueError, match="Schema"):
        detector.check(matrix, probabilities, schema_fingerprint="c" * 64)
    with pytest.raises(ValueError, match="样本不足"):
        detector.check(matrix[:5], probabilities[:5], schema_fingerprint="b" * 64)
