"""FeatureExtractor 统一调度器 + FeaturePreprocessor 测试。"""

from __future__ import annotations

import pytest  # noqa: F401


class TestFeatureExtractor:
    """统一调度器测试。"""

    def test_total_features(self) -> None:
        """验证总特征维数 = 38+21+13+9+11+6 = 98。"""
        from Highlight_Model.feature_extractor.base import FeatureExtractor

        ext = FeatureExtractor()
        assert ext.total_features == 98  # 38+21+13+9+11+6

    def test_feature_names_count(self) -> None:
        """验证特征名称数与总维数一致。"""
        from Highlight_Model.feature_extractor.base import FeatureExtractor

        ext = FeatureExtractor()
        assert len(ext.feature_names) == ext.total_features

    def test_extract_shape(self) -> None:
        """验证 extract 返回正确 shape。"""
        import numpy as np

        from Highlight_Model.feature_extractor.base import FeatureExtractor

        ext = FeatureExtractor()
        result = ext.extract(0)
        assert isinstance(result, np.ndarray)
        assert result.shape == (98,)
        assert result.dtype == np.float32


class TestFeaturePreprocessor:
    """预处理器测试。"""

    def test_not_fitted_raises(self) -> None:
        """验证未拟合时 transform 抛出异常。"""
        import numpy as np

        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor

        pp = FeaturePreprocessor()
        with pytest.raises(RuntimeError, match="请先调用 fit"):
            pp.transform(np.zeros((3, 5)))

    def test_fit_transform_returns_float32(self) -> None:
        """验证 fit_transform 返回 float32。"""
        import numpy as np

        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor

        pp = FeaturePreprocessor()
        X = np.random.randn(10, 5).astype(np.float64)
        result = pp.fit_transform(X)
        assert result.dtype == np.float32
        assert result.shape == (10, 5)
