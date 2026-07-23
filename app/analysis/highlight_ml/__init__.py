"""高光机器学习的数据与特征公共层。"""

from app.analysis.highlight_ml.context import load_feature_context, load_file_audio_snapshot
from app.analysis.highlight_ml.dataset import build_labeled_dataset
from app.analysis.highlight_ml.drift import DriftBaseline, DriftDetector
from app.analysis.highlight_ml.features import extract_feature_record
from app.analysis.highlight_ml.models import ArtifactPredictor, ModelArtifact
from app.analysis.highlight_ml.online import OnlinePrediction, get_online_status, predict_online
from app.analysis.highlight_ml.operations import TrainingRunSummary, check_champion_drift, train_and_register
from app.analysis.highlight_ml.registry import ModelRegistry
from app.analysis.highlight_ml.runtime import HotReloadingPredictor
from app.analysis.highlight_ml.schema import DEFAULT_FEATURE_SCHEMA, FeatureSchema, FeatureSpec
from app.analysis.highlight_ml.training import TrainingConfig, train_candidate_models
from app.analysis.highlight_ml.types import DatasetBundle, SegmentFeatureContext

__all__ = [
    "DEFAULT_FEATURE_SCHEMA",
    "ArtifactPredictor",
    "DatasetBundle",
    "DriftBaseline",
    "DriftDetector",
    "FeatureSchema",
    "FeatureSpec",
    "HotReloadingPredictor",
    "ModelArtifact",
    "ModelRegistry",
    "OnlinePrediction",
    "SegmentFeatureContext",
    "TrainingConfig",
    "TrainingRunSummary",
    "build_labeled_dataset",
    "check_champion_drift",
    "extract_feature_record",
    "get_online_status",
    "load_feature_context",
    "load_file_audio_snapshot",
    "predict_online",
    "train_candidate_models",
    "train_and_register",
]
