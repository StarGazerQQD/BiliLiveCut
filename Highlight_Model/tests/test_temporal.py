"""时序/上下文特征提取器测试 (T1-T9)。"""

from __future__ import annotations

import pytest  # noqa: F401


class TestTemporalExtractor:
    """时序特征提取器单元测试。"""

    def test_feature_count(self) -> None:
        """验证输出特征维数正确。"""
        from Highlight_Model.feature_extractor.temporal import TemporalExtractor

        ext = TemporalExtractor()
        assert ext.n_features == 9
        assert len(ext.feature_names) == 9

    def test_extract_shape(self) -> None:
        """验证 extract 返回正确 shape。"""
        import numpy as np

        from Highlight_Model.feature_extractor.temporal import TemporalExtractor

        ext = TemporalExtractor()
        result = ext.extract(0)
        assert isinstance(result, np.ndarray)
        assert result.shape == (9,)
