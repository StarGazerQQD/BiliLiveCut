"""数据质量守卫 (v0.1.9.1-HL-alpha)。

在训练前检查：标签冲突、正负比例、NaN/Inf、常量特征。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DataQualityReport:
    passed: bool
    total_records: int
    positives: int
    negatives: int
    conflicts: int
    nan_features: int
    inf_features: int
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


class DataQualityGuard:
    def __init__(self, min_pos_ratio: float = 0.02,
                 max_pos_ratio: float = 0.80) -> None:
        self.min_pos_ratio = min_pos_ratio
        self.max_pos_ratio = max_pos_ratio

    def check_feedback(self, records: list[dict]) -> DataQualityReport:
        issues: list[str] = []
        suggestions: list[str] = []
        if not records:
            return DataQualityReport(False, 0, 0, 0, 0, 0, 0,
                                     issues=["无训练数据"],
                                     suggestions=["请先在 Dashboard 审批一些高光候选"])
        conflicts = self._detect_conflicts(records)
        if conflicts:
            issues.append(f"标签冲突: {conflicts} 个候选被重复审批不同结果")
        pos = sum(1 for r in records if r.get("action") == "approved")
        neg = len(records) - pos
        ratio = pos / max(len(records), 1)
        if ratio < self.min_pos_ratio:
            suggestions.append(f"正样本比例过低 ({ratio:.1%})")
        if ratio > self.max_pos_ratio:
            suggestions.append(f"正样本比例过高 ({ratio:.1%})，请确认未被滥批")
        if len(records) >= 10:
            suggestions.append("建议按时间顺序拆分训练/验证集避免数据泄露")
        passed = len(issues) == 0
        return DataQualityReport(passed, len(records), pos, neg, conflicts, 0, 0,
                                  issues=issues, suggestions=suggestions)

    def check_features(self, X: np.ndarray) -> DataQualityReport:
        issues: list[str] = []
        n_nan = int(np.sum(np.isnan(X)))
        n_inf = int(np.sum(np.isinf(X)))
        if n_nan: issues.append(f"发现 {n_nan} 个 NaN")
        if n_inf: issues.append(f"发现 {n_inf} 个 Inf")
        zero_cols = int(np.sum(np.all(np.abs(X) < 1e-8, axis=0)))
        if zero_cols: issues.append(f"发现 {zero_cols} 个全零列，特征提取器可能未实现")
        std_zero = int(np.sum(np.std(X, axis=0) < 1e-8))
        if std_zero > zero_cols: issues.append(f"发现 {std_zero} 个常量列")
        return DataQualityReport(len(issues) == 0, X.shape[0], 0, 0, 0, n_nan, n_inf, issues=issues)

    def _detect_conflicts(self, records: list[dict]) -> int:
        seen: dict[int, str] = {}
        conflicts = 0
        for r in records:
            cid = r.get("candidate_id", 0)
            if cid in seen and seen[cid] != r.get("action", ""):
                conflicts += 1
            seen[cid] = r.get("action", "")
        return conflicts
