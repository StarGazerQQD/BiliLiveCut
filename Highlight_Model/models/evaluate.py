"""模型评估脚本。

计算 AUC、F1、Precision、Recall 等指标，
输出混淆矩阵和特征重要性排序。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def evaluate_model(
    model_path: str,
    room_id: int | None = None,
) -> dict[str, float]:
    """评估已训练的高光模型。

    :param model_path: 模型文件路径。
    :param room_id: 按房间过滤评估集（可选）。
    :returns: 评估指标字典 (auc, f1, precision, recall, accuracy)。
    """
    # TODO: 阶段 4 实现
    logger.info("评估入口占位 — 待阶段 4 实现。")
    return {"auc": 0.0, "f1": 0.0, "precision": 0.0, "recall": 0.0, "accuracy": 0.0}
