"""预测分布漂移检测器 (v0.1.8.2.1-HL-alpha)。

PSI + 特征均值偏移检测，模型退化预警。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)
_DRIFT_PATH = Path("storage/models") / "drift_state.json"


@dataclass(slots=True)
class DriftReport:
    psi: float; psi_status: str; feature_shift_mean: float
    feature_shift_max: float; shifted_features: list[str]
    n_samples_recent: int; n_samples_baseline: int
    is_drifted: bool; checked_at: str = ""


class PredictionDriftDetector:
    def __init__(self, psi_warning: float = 0.1, psi_alert: float = 0.25,
                 feature_shift_threshold: float = 0.5, n_bins: int = 10) -> None:
        self.psi_warning = psi_warning; self.psi_alert = psi_alert
        self.feature_shift_threshold = feature_shift_threshold
        self.n_bins = n_bins
        self._baseline_proba: np.ndarray | None = None
        self._baseline_features: np.ndarray | None = None
        self._feature_names: list[str] = []
        self._load_state()

    def set_baseline(self, proba: np.ndarray, features: np.ndarray,
                     feature_names: list[str]) -> None:
        self._baseline_proba = np.asarray(proba, dtype=np.float32)
        self._baseline_features = np.asarray(features, dtype=np.float64)
        self._feature_names = list(feature_names)
        self._save_state()
        logger.info("漂移基线已设置 n=%d", len(proba))

    def check(self, recent_proba: np.ndarray, recent_features: np.ndarray) -> DriftReport:
        if self._baseline_proba is None:
            return DriftReport(0, "ok", 0, 0, [], len(recent_proba), 0, False,
                               datetime.now(timezone.utc).isoformat())
        psi = self._compute_psi(self._baseline_proba, recent_proba)
        psi_status = "ok" if psi < self.psi_warning else ("warning" if psi < self.psi_alert else "alert")
        if self._baseline_features is not None and recent_features.size > 0:
            shifts = np.abs(np.mean(self._baseline_features, 0) - np.mean(recent_features, 0)) / (np.std(self._baseline_features, 0) + 1e-8)
            shift_mean, shift_max = float(np.mean(shifts)), float(np.max(shifts))
            idx = np.where(shifts > self.feature_shift_threshold)[0]
            shifted_names = [self._feature_names[i] if i < len(self._feature_names) else f"f{i}" for i in idx]
        else:
            shift_mean, shift_max, shifted_names = 0.0, 0.0, []
        is_drifted = psi_status == "alert" or len(shifted_names) > 5
        report = DriftReport(round(psi, 4), psi_status, round(shift_mean, 4), round(shift_max, 4),
                             shifted_names[:10], len(recent_proba), len(self._baseline_proba),
                             is_drifted, datetime.now(timezone.utc).isoformat())
        if is_drifted:
            logger.warning("⚠ 漂移告警 PSI=%.3f(%s) 偏移特征=%d", psi, psi_status, len(shifted_names))
        return report

    def _compute_psi(self, baseline: np.ndarray, recent: np.ndarray) -> float:
        eps = 1e-6; breaks = np.linspace(0, 1, self.n_bins + 1)
        bh, _ = np.histogram(baseline, bins=breaks); rh, _ = np.histogram(recent, bins=breaks)
        bh, rh = bh / (bh.sum() + eps), rh / (rh.sum() + eps)
        return float(sum(max(b, eps) * np.log(max(b, eps) / max(r, eps)) for b, r in zip(bh, rh)))

    def _save_state(self) -> None:
        if self._baseline_proba is None: return
        _DRIFT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DRIFT_PATH.write_text(json.dumps({"baseline_n": int(len(self._baseline_proba)),
            "feature_names": self._feature_names,
            "baseline_proba_mean": float(np.mean(self._baseline_proba)),
            "baseline_proba_std": float(np.std(self._baseline_proba)),
            "saved_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_state(self) -> None:
        if _DRIFT_PATH.exists():
            try: json.loads(_DRIFT_PATH.read_text(encoding="utf-8"))
            except Exception: pass
