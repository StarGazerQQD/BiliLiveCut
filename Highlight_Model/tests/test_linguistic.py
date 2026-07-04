"""语义/语言特征提取器测试 (L1-L21)。"""

from __future__ import annotations

import pytest  # noqa: F401


class TestLinguisticExtractor:
    """语义特征提取器单元测试。"""

    def test_feature_count(self) -> None:
        """验证输出特征维数正确。"""
        from Highlight_Model.feature_extractor.linguistic import LinguisticExtractor

        ext = LinguisticExtractor()
        assert ext.n_features == 21
        assert len(ext.feature_names) == 21

    def test_extract_shape(self) -> None:
        """验证 extract 返回正确 shape。"""
        import numpy as np

        from Highlight_Model.feature_extractor.linguistic import LinguisticExtractor

        ext = LinguisticExtractor()
        result = ext.extract(0)
        assert isinstance(result, np.ndarray)
        assert result.shape == (21,)
