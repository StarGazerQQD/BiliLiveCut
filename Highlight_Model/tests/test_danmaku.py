"""弹幕交互特征提取器测试 (D1-D13)。"""

from __future__ import annotations

import pytest  # noqa: F401


class TestDanmakuExtractor:
    """弹幕特征提取器单元测试。"""

    def test_feature_count(self) -> None:
        """验证输出特征维数正确。"""
        from Highlight_Model.feature_extractor.danmaku import DanmakuExtractor

        ext = DanmakuExtractor()
        assert ext.n_features == 13
        assert len(ext.feature_names) == 13

    def test_extract_shape(self) -> None:
        """验证 extract 返回正确 shape。"""
        import numpy as np

        from Highlight_Model.feature_extractor.danmaku import DanmakuExtractor

        ext = DanmakuExtractor()
        result = ext.extract(0)
        assert isinstance(result, np.ndarray)
        assert result.shape == (13,)
