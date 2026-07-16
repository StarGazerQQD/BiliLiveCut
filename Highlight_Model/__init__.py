"""Highlight_Model 公开接口。"""
from __future__ import annotations

from Highlight_Model.feature_extractor.base import BaseFeatureExtractor, FeatureExtractor
from Highlight_Model.models.inference import ModelInference
from Highlight_Model.models.registry import ModelRegistry, ModelVersion
from Highlight_Model.models.self_learn import SelfLearnEngine, SelfLearnResult
from Highlight_Model.models.drift import PredictionDriftDetector, DriftReport
from Highlight_Model.dataset.guard import DataQualityGuard, DataQualityReport

__all__ = [
    "BaseFeatureExtractor", "FeatureExtractor",
    "ModelInference", "ModelRegistry", "ModelVersion",
    "SelfLearnEngine", "SelfLearnResult",
    "PredictionDriftDetector", "DriftReport",
    "DataQualityGuard", "DataQualityReport",
]
