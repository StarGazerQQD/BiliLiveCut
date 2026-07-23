"""面向稀有高光与审核产能的模型评估指标。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """总体指标与房间级指标。"""

    metrics: dict[str, float]
    per_room: dict[int, dict[str, float]]

    def to_dict(self) -> dict[str, object]:
        """转换为可持久化 JSON 对象。"""
        return {
            "metrics": self.metrics,
            "per_room": {str(room_id): values for room_id, values in self.per_room.items()},
        }


def _validate(y_true: np.ndarray, probabilities: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    labels = np.asarray(y_true, dtype=np.int8).reshape(-1)
    probs = np.asarray(probabilities, dtype=np.float64).reshape(-1)
    if labels.size == 0 or labels.shape != probs.shape:
        raise ValueError("标签与概率必须为等长非空向量")
    if not np.isin(labels, [0, 1]).all() or not np.isfinite(probs).all():
        raise ValueError("标签必须为 0/1 且概率必须有限")
    return labels, np.clip(probs, 0.0, 1.0)


def average_precision(y_true: np.ndarray, probabilities: np.ndarray) -> float:
    """计算阶梯积分形式的 PR-AUC（Average Precision）。"""
    labels, probs = _validate(y_true, probabilities)
    positives = int(labels.sum())
    if positives == 0:
        return 0.0
    order = np.argsort(-probs, kind="stable")
    ranked = labels[order]
    cumulative = np.cumsum(ranked)
    precision = cumulative / np.arange(1, labels.size + 1)
    return float(np.sum(precision * ranked) / positives)


def roc_auc(y_true: np.ndarray, probabilities: np.ndarray) -> float:
    """使用平均秩处理并列分数的 ROC-AUC。"""
    labels, probs = _validate(y_true, probabilities)
    n_positive = int(labels.sum())
    n_negative = labels.size - n_positive
    if n_positive == 0 or n_negative == 0:
        return 0.5
    order = np.argsort(probs, kind="stable")
    sorted_probs = probs[order]
    ranks = np.empty(labels.size, dtype=np.float64)
    start = 0
    while start < labels.size:
        end = start + 1
        while end < labels.size and sorted_probs[end] == sorted_probs[start]:
            end += 1
        ranks[order[start:end]] = (start + 1 + end) / 2.0
        start = end
    rank_sum = float(np.sum(ranks[labels == 1]))
    return (rank_sum - n_positive * (n_positive + 1) / 2.0) / (n_positive * n_negative)


def recall_at_audit_fraction(y_true: np.ndarray, probabilities: np.ndarray, fraction: float) -> float:
    """计算审核量限制为样本比例时能找回的正类比例。"""
    labels, probs = _validate(y_true, probabilities)
    if not 0 < fraction <= 1:
        raise ValueError("审核比例必须位于 (0, 1]")
    positives = int(labels.sum())
    if positives == 0:
        return 0.0
    budget = max(1, math.ceil(labels.size * fraction))
    selected = np.argsort(-probs, kind="stable")[:budget]
    return float(labels[selected].sum() / positives)


def expected_calibration_error(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    *,
    n_bins: int = 10,
) -> float:
    """计算等宽概率箱的期望校准误差。"""
    labels, probs = _validate(y_true, probabilities)
    if n_bins < 2:
        raise ValueError("校准箱数量至少为 2")
    indices = np.minimum((probs * n_bins).astype(int), n_bins - 1)
    error = 0.0
    for bin_index in range(n_bins):
        mask = indices == bin_index
        if not np.any(mask):
            continue
        error += float(np.mean(mask)) * abs(float(np.mean(probs[mask])) - float(np.mean(labels[mask])))
    return error


def _threshold_metrics(labels: np.ndarray, probs: np.ndarray, threshold: float) -> dict[str, float]:
    predicted = probs >= threshold
    positive = labels == 1
    tp = int(np.sum(predicted & positive))
    fp = int(np.sum(predicted & ~positive))
    fn = int(np.sum(~predicted & positive))
    tn = int(np.sum(~predicted & ~positive))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": (tp + tn) / labels.size,
        "true_positive": float(tp),
        "false_positive": float(fp),
        "false_negative": float(fn),
        "true_negative": float(tn),
    }


def select_f1_threshold(y_true: np.ndarray, probabilities: np.ndarray) -> float:
    """只在校准集上选择 F1 最大的阈值，平局取更高阈值。"""
    labels, probs = _validate(y_true, probabilities)
    best_threshold = 0.5
    best_f1 = -1.0
    for threshold in sorted(set(float(item) for item in probs), reverse=True):
        f1 = _threshold_metrics(labels, probs, threshold)["f1"]
        if f1 > best_f1:
            best_f1 = f1
            best_threshold = threshold
    return float(np.clip(best_threshold, 0.01, 0.99))


def evaluate_probabilities(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    *,
    room_ids: np.ndarray | None = None,
    threshold: float = 0.5,
    audit_fraction: float = 0.2,
) -> EvaluationReport:
    """计算总体、审核预算与房间宏平均指标。"""
    labels, probs = _validate(y_true, probabilities)
    eps = 1e-12
    metrics = {
        "pr_auc": average_precision(labels, probs),
        "roc_auc": roc_auc(labels, probs),
        "brier": float(np.mean((probs - labels) ** 2)),
        "log_loss": float(-np.mean(labels * np.log(probs + eps) + (1 - labels) * np.log(1 - probs + eps))),
        "calibration_error": expected_calibration_error(labels, probs),
        "recall_at_audit_fraction": recall_at_audit_fraction(labels, probs, audit_fraction),
        "audit_fraction": audit_fraction,
        "threshold": threshold,
    }
    metrics.update(_threshold_metrics(labels, probs, threshold))

    per_room: dict[int, dict[str, float]] = {}
    if room_ids is not None:
        rooms = np.asarray(room_ids, dtype=np.int64).reshape(-1)
        if rooms.shape != labels.shape:
            raise ValueError("room_ids 与标签维度不一致")
        for room_id in sorted(set(int(item) for item in rooms)):
            mask = rooms == room_id
            room_labels = labels[mask]
            room_probs = probs[mask]
            room_metrics = _threshold_metrics(room_labels, room_probs, threshold)
            room_metrics["pr_auc"] = average_precision(room_labels, room_probs)
            room_metrics["recall_at_audit_fraction"] = recall_at_audit_fraction(room_labels, room_probs, audit_fraction)
            per_room[room_id] = room_metrics
        metrics["room_macro_recall"] = float(np.mean([item["recall"] for item in per_room.values()]))
        metrics["room_macro_pr_auc"] = float(np.mean([item["pr_auc"] for item in per_room.values()]))
        metrics["room_macro_recall_at_audit_fraction"] = float(
            np.mean([item["recall_at_audit_fraction"] for item in per_room.values()])
        )
    return EvaluationReport(metrics=metrics, per_room=per_room)
