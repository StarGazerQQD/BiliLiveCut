"""元数据/画像特征提取器测试 (M1-M11)。"""

from __future__ import annotations

import pytest  # noqa: F401


class TestMetadataExtractor:
    """画像特征提取器单元测试。"""

    def test_feature_count(self) -> None:
        """验证输出特征维数正确。"""
        from Highlight_Model.feature_extractor.metadata import MetadataExtractor

        ext = MetadataExtractor()
        assert ext.n_features == 11
        assert len(ext.feature_names) == 11

    def test_extract_shape(self) -> None:
        """验证 extract 返回正确 shape。"""
        import numpy as np

        from Highlight_Model.feature_extractor.metadata import MetadataExtractor

        ext = MetadataExtractor()
        result = ext.extract(0)
        assert isinstance(result, np.ndarray)
        assert result.shape == (11,)
