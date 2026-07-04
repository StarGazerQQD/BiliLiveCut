"""跨模态融合特征提取器测试 (C1-C6)。"""

from __future__ import annotations

import pytest  # noqa: F401


class TestFusionExtractor:
    """融合特征提取器单元测试。"""

    def test_feature_count(self) -> None:
        """验证输出特征维数正确。"""
        from Highlight_Model.feature_extractor.fusion import FusionExtractor

        ext = FusionExtractor()
        assert ext.n_features == 6
        assert len(ext.feature_names) == 6

    def test_extract_shape(self) -> None:
        """验证 extract 返回正确 shape。"""
        import numpy as np

        from Highlight_Model.feature_extractor.fusion import FusionExtractor

        ext = FusionExtractor()
        result = ext.extract(0)
        assert isinstance(result, np.ndarray)
        assert result.shape == (6,)
