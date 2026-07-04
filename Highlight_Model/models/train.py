"""训练入口脚本。

从指定房间的反馈数据构建训练集，训练 XGBoost/LightGBM 模型，
输出模型文件到 ``storage/models/``。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def train_model(
    room_id: int | None = None,
    model_type: str = "xgboost",
    output_path: str = "",
) -> str:
    """训练高光预测模型。

    :param room_id: 直播间 id（可选，None 为全量）。
    :param model_type: 模型类型 (``"xgboost"`` / ``"lightgbm"`` / ``"mlp"``)。
    :param output_path: 模型输出路径，默认 ``storage/models/``。
    :returns: 模型文件路径。
    """
    # TODO: 阶段 3 实现
    logger.info("训练入口占位 — 待阶段 3 实现。")
    return output_path
