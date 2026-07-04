"""训练数据集构建器 (v0.1.9.1-HL-alpha)。

从 ThresholdFeedback 表获取人工审批 (approved/rejected) 标签，
调用 FeatureExtractor 提取完整 98 维特征，组装训练集。
"""
from __future__ import annotations

import numpy as np

from Highlight_Model.dataset.preprocessor import FeaturePreprocessor


class DatasetBundle:
    """完整的训练/评估数据集。"""
    __slots__ = ("X", "y", "feature_names", "sample_ids")

    def __init__(self, X: np.ndarray, y: np.ndarray,
                 feature_names: list[str], sample_ids: list[int]) -> None:
        self.X = np.asarray(X, dtype=np.float32)
        self.y = np.asarray(y, dtype=np.int32)
        self.feature_names = feature_names
        self.sample_ids = sample_ids

    @property
    def n_samples(self) -> int:
        return int(self.X.shape[0])

    @property
    def n_features(self) -> int:
        return int(self.X.shape[1])

    @property
    def pos_ratio(self) -> float:
        return float(np.mean(self.y)) if self.n_samples > 0 else 0.0

    def split(self, test_ratio: float = 0.2, seed: int = 42):
        """随机划分训练/验证集。"""
        rng = np.random.RandomState(seed)
        idx = rng.permutation(self.n_samples)
        n_test = max(1, int(self.n_samples * test_ratio))
        test_idx, train_idx = idx[:n_test], idx[n_test:]
        return (
            DatasetBundle(self.X[train_idx], self.y[train_idx],
                          self.feature_names, [self.sample_ids[i] for i in train_idx]),
            DatasetBundle(self.X[test_idx], self.y[test_idx],
                          self.feature_names, [self.sample_ids[i] for i in test_idx]),
        )


class DatasetBuilder:
    """从 ThresholdFeedback 表构建训练集。

    正样本：action == "approved"
    负样本：action == "rejected"
    """
    def __init__(self, min_positive: int = 10) -> None:
        self.min_positive = min_positive
        self._preprocessor = FeaturePreprocessor()

    def build(self, room_id: int | None = None,
              preprocess: bool = True) -> DatasetBundle | None:
        """构建训练集。

        :param room_id: 可选，限定指定房间。
        :param preprocess: 是否对特征做标准化。
        """
        from Highlight_Model.dataset.shared import load_feedback, candidate_to_segment
        records = load_feedback(room_id)
        if len(records) < self.min_positive * 2:
            return None

        positives = [r for r in records if r["action"] == "approved"]
        if len(positives) < self.min_positive:
            return None

        from Highlight_Model.feature_extractor.base import FeatureExtractor
        extractor = FeatureExtractor()

        X_list, y_list, ids = [], [], []
        for rec in records:
            # 从 feedback 定位 segment_id（候选所在片段）
            seg_id = _candidate_to_segment(rec.get("candidate_id", 0))
            if seg_id is None:
                continue
            try:
                vec = extractor.extract(seg_id)
            except Exception:
                continue
            X_list.append(vec)
            y_list.append(1 if rec["action"] == "approved" else 0)
            ids.append(rec["candidate_id"])

        if len(X_list) == 0 or sum(y_list) < self.min_positive:
            return None

        X = np.stack(X_list)
        y = np.array(y_list, dtype=np.int32)
        if preprocess:
            X = self._preprocessor.fit_transform(X)

        return DatasetBundle(X, y, list(extractor.feature_names), ids)
