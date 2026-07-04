"""Highlight_Model 完整测试套件 (v0.1.8.2-HL-alpha)。"""
from __future__ import annotations

import numpy as np
import pytest


class TestAcousticExtractor:
    def test_feature_count(self) -> None:
        from Highlight_Model.feature_extractor.acoustic import AcousticExtractor
        ext = AcousticExtractor()
        assert ext.n_features == 38
        assert len(ext.feature_names) == 38

    def test_extract_empty(self) -> None:
        from Highlight_Model.feature_extractor.acoustic import AcousticExtractor
        ext = AcousticExtractor()
        result = ext.extract(-1)  # nonexistent segment
        assert isinstance(result, np.ndarray)
        assert result.shape == (38,)
        assert result.dtype == np.float32


class TestLinguisticExtractor:
    def test_feature_count(self) -> None:
        from Highlight_Model.feature_extractor.linguistic import LinguisticExtractor
        ext = LinguisticExtractor()
        assert ext.n_features == 21
        assert len(ext.feature_names) == 21

    def test_extract_empty(self) -> None:
        from Highlight_Model.feature_extractor.linguistic import LinguisticExtractor
        ext = LinguisticExtractor()
        result = ext.extract(-1)
        assert result.shape == (21,)


class TestDanmakuExtractor:
    def test_feature_count(self) -> None:
        from Highlight_Model.feature_extractor.danmaku import DanmakuExtractor
        ext = DanmakuExtractor()
        assert ext.n_features == 13

    def test_extract_empty(self) -> None:
        from Highlight_Model.feature_extractor.danmaku import DanmakuExtractor
        ext = DanmakuExtractor()
        result = ext.extract(-1)
        assert result.shape == (13,)


class TestTemporalExtractor:
    def test_feature_count(self) -> None:
        from Highlight_Model.feature_extractor.temporal import TemporalExtractor
        ext = TemporalExtractor()
        assert ext.n_features == 9


class TestMetadataExtractor:
    def test_feature_count(self) -> None:
        from Highlight_Model.feature_extractor.metadata import MetadataExtractor
        ext = MetadataExtractor()
        assert ext.n_features == 11


class TestFusionExtractor:
    def test_feature_count(self) -> None:
        from Highlight_Model.feature_extractor.fusion import FusionExtractor
        ext = FusionExtractor()
        assert ext.n_features == 6


class TestFeatureExtractor:
    def test_total_features(self) -> None:
        from Highlight_Model.feature_extractor.base import FeatureExtractor
        ext = FeatureExtractor()
        assert ext.total_features == 98

    def test_feature_names_match(self) -> None:
        from Highlight_Model.feature_extractor.base import FeatureExtractor
        ext = FeatureExtractor()
        assert len(ext.feature_names) == ext.total_features

    def test_extract_shape(self) -> None:
        from Highlight_Model.feature_extractor.base import FeatureExtractor
        ext = FeatureExtractor()
        result = ext.extract(-1)
        assert result.shape == (98,)
        assert result.dtype == np.float32

    def test_named_features(self) -> None:
        from Highlight_Model.feature_extractor.base import FeatureExtractor
        ext = FeatureExtractor()
        d = ext.named_features(-1)
        assert isinstance(d, dict)
        assert len(d) == 98


class TestFeaturePreprocessor:
    def test_not_fitted_raises(self) -> None:
        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor
        pp = FeaturePreprocessor()
        with pytest.raises(RuntimeError, match="请先调用 fit"):
            pp.transform(np.zeros((3, 5)))

    def test_fit_transform_shape(self) -> None:
        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor
        pp = FeaturePreprocessor()
        X = np.random.randn(10, 5).astype(np.float64)
        result = pp.fit_transform(X)
        assert result.dtype == np.float32
        assert result.shape == (10, 5)

    def test_handle_nan(self) -> None:
        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor
        pp = FeaturePreprocessor()
        X = np.array([[1.0, np.nan], [np.nan, 2.0], [3.0, 4.0]], dtype=np.float64)
        result = pp.fit_transform(X)
        assert not np.any(np.isnan(result))
        assert result.shape == (3, 2)


class TestDatasetBuilder:
    def test_build_empty_returns_none(self) -> None:
        from Highlight_Model.dataset.builder import DatasetBuilder
        builder = DatasetBuilder(min_positive=100)  # impossible threshold
        result = builder.build(room_id=-999)
        assert result is None

    def test_bundle_split(self) -> None:
        from Highlight_Model.dataset.builder import DatasetBundle
        X = np.random.randn(50, 5).astype(np.float32)
        y = np.random.randint(0, 2, 50).astype(np.int32)
        bundle = DatasetBundle(X, y, ["f0", "f1", "f2", "f3", "f4"], list(range(50)))
        train, val = bundle.split(test_ratio=0.2, seed=42)
        assert val.n_samples + train.n_samples == 50
        assert val.n_samples == 10


class TestModelInference:
    def test_not_loaded(self) -> None:
        from Highlight_Model.models.inference import ModelInference
        infer = ModelInference("nonexistent.pkl")
        with pytest.raises(FileNotFoundError):
            infer.load()
        assert not infer.is_loaded


class TestFeaturePreprocessorRoundtrip:
    """验证 fit_transform 后数据无 NaN 且 shape 正确。"""
    def test_all_mocks_pass(self) -> None:
        X = np.eye(4, dtype=np.float64)
        X[0, 1] = np.nan
        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor
        pp = FeaturePreprocessor()
        out = pp.fit_transform(X)
        assert out.shape == (4, 4)
        assert not np.any(np.isnan(out))
