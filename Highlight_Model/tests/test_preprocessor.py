"""Highlight_Model 完整测试 (v0.1.8.2.1-HL-alpha)。"""
from __future__ import annotations
import numpy as np
import pytest


class TestAcousticExtractor:
    def test_feature_count(self):
        from Highlight_Model.feature_extractor.acoustic import AcousticExtractor
        ext = AcousticExtractor(); assert ext.n_features == 38

    def test_extract_empty(self):
        from Highlight_Model.feature_extractor.acoustic import AcousticExtractor
        r = AcousticExtractor().extract(-1)
        assert r.shape == (38,) and r.dtype == np.float32


class TestLinguisticExtractor:
    def test_count(self):
        from Highlight_Model.feature_extractor.linguistic import LinguisticExtractor
        assert LinguisticExtractor().n_features == 21


class TestDanmakuExtractor:
    def test_count(self):
        from Highlight_Model.feature_extractor.danmaku import DanmakuExtractor
        assert DanmakuExtractor().n_features == 13


class TestTemporalExtractor:
    def test_count(self):
        from Highlight_Model.feature_extractor.temporal import TemporalExtractor
        assert TemporalExtractor().n_features == 9


class TestMetadataExtractor:
    def test_count(self):
        from Highlight_Model.feature_extractor.metadata import MetadataExtractor
        assert MetadataExtractor().n_features == 11


class TestFusionExtractor:
    def test_count(self):
        from Highlight_Model.feature_extractor.fusion import FusionExtractor
        assert FusionExtractor().n_features == 6


class TestFeatureExtractor:
    def test_total(self):
        from Highlight_Model.feature_extractor.base import FeatureExtractor
        assert FeatureExtractor().total_features == 98

    def test_extract(self):
        from Highlight_Model.feature_extractor.base import FeatureExtractor
        r = FeatureExtractor().extract(-1)
        assert r.shape == (98,) and r.dtype == np.float32


class TestFeaturePreprocessor:
    def test_not_fitted(self):
        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor
        with pytest.raises(RuntimeError, match="fit"):
            FeaturePreprocessor().transform(np.zeros((3, 5)))

    def test_fit_transform(self):
        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor
        X = np.random.randn(10, 5).astype(np.float64)
        r = FeaturePreprocessor().fit_transform(X)
        assert r.shape == (10, 5) and not np.any(np.isnan(r))


class TestDatasetBuilder:
    def test_empty(self):
        from Highlight_Model.dataset.builder import DatasetBuilder
        assert DatasetBuilder(min_positive=100).build(room_id=-999) is None

    def test_bundle_split(self):
        from Highlight_Model.dataset.builder import DatasetBundle
        X = np.random.randn(50, 5).astype(np.float32)
        y = np.random.randint(0, 2, 50).astype(np.int32)
        b = DatasetBundle(X, y, [f"f{i}" for i in range(5)], list(range(50)))
        t, v = b.split(0.2, 42)
        assert v.n_samples + t.n_samples == 50 and v.n_samples == 10


class TestSelfLearnEngine:
    def test_init(self):
        from Highlight_Model.models.self_learn import SelfLearnEngine
        e = SelfLearnEngine(min_positive=100)
        assert e.model_type == "xgboost" and "model_available" in e.status


class TestModelRegistry:
    def test_empty(self):
        from Highlight_Model.models.registry import ModelRegistry
        r = ModelRegistry(); assert r.champion is None
        assert not r.versions


class TestDriftDetector:
    def test_no_baseline(self):
        from Highlight_Model.models.drift import PredictionDriftDetector
        r = PredictionDriftDetector().check(np.random.rand(20), np.random.randn(20, 5))
        assert r.psi_status == "ok"

    def test_psi_normal(self):
        from Highlight_Model.models.drift import PredictionDriftDetector
        d = PredictionDriftDetector()
        p = np.random.beta(2, 3, 200)
        d.set_baseline(p, np.random.randn(200, 5), [])
        r = d.check(p + np.random.normal(0, 0.02, 200), np.random.randn(200, 5))
        assert r.psi < 0.3


class TestDataQualityGuard:
    def test_empty(self):
        from Highlight_Model.dataset.guard import DataQualityGuard
        assert not DataQualityGuard().check_feedback([]).passed

    def test_conflict(self):
        from Highlight_Model.dataset.guard import DataQualityGuard
        recs = [{"candidate_id": 1, "action": "approved"}, {"candidate_id": 1, "action": "rejected"}]
        assert DataQualityGuard().check_feedback(recs).conflicts == 1


class TestModelInference:
    def test_not_loaded(self):
        from Highlight_Model.models.inference import ModelInference
        with pytest.raises(FileNotFoundError):
            ModelInference("nonexistent.pkl").load()
