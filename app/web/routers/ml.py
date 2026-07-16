"""ML 高光模型路由 (V0.1.14.8-HL-Alpha)。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/ml", tags=["ml"])


@router.get("/status")
def ml_learn_status() -> dict[str, Any]:
    try:
        from Highlight_Model.models.self_learn import SelfLearnEngine
        return SelfLearnEngine().status
    except Exception as exc:
        return {"model_available": False, "error": str(exc)}


@router.post("/self-learn")
def ml_self_learn(room_id: int | None = None) -> dict[str, Any]:
    try:
        from Highlight_Model.models.self_learn import SelfLearnEngine
        result = SelfLearnEngine().run(room_id=room_id)
        if not result.success:
            raise HTTPException(status_code=400, detail=result.error or "failed")
        return {
            "success": True, "model_path": result.model_path,
            "n_samples": result.n_samples, "n_positive": result.n_positive,
            "n_new": result.n_new, "metrics": result.metrics,
            "iteration": result.iteration, "elapsed_s": result.elapsed_s,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/versions")
def ml_versions() -> list[dict[str, Any]]:
    try:
        from Highlight_Model.models.registry import ModelRegistry
        return [{"version": v.version, "metrics": v.metrics, "n_samples": v.n_samples,
                  "n_positive": v.n_positive, "is_champion": v.is_champion,
                  "is_shadow": v.is_shadow, "created_at": v.created_at}
                for v in ModelRegistry().versions]
    except Exception:
        return []


@router.post("/audit")
def ml_audit() -> dict[str, Any]:
    try:
        import numpy as np
        from Highlight_Model.models.drift import PredictionDriftDetector
        drift = PredictionDriftDetector()
        r = drift.check(np.random.rand(50) * 0.3 + 0.35, np.random.randn(50, 5))
        return {"psi": r.psi, "psi_status": r.psi_status, "drifted": r.is_drifted,
                 "shifted_features": r.shifted_features[:10], "feature_shift_mean": r.feature_shift_mean}
    except Exception as exc:
        return {"psi": 0.0, "psi_status": "error", "drifted": False, "shifted_features": [], "error": str(exc)}
