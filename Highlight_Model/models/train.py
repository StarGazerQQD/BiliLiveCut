"""模型训练入口 (v0.1.13.1-HL-Alpha)。

采用 XGBoost 作为主模型（纯本地，无 API 依赖，国产可控），
LightGBM 作为备选方案。支持从 ThresholdFeedback 表自动构建
训练集、训练、保存模型文件到 storage/models/。
"""
from __future__ import annotations

import json
import logging
import os
import pickle
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# 默认模型路径
_MODEL_DIR = Path(os.environ.get("ML_MODEL_DIR", "storage/models"))


def train_model(
    room_id: int | None = None,
    model_type: str = "xgboost",
    output_path: str = "",
    test_ratio: float = 0.2,
) -> tuple[str, dict]:
    """训练高光预测模型并保存到磁盘。

    :param room_id: 限定某房间（None=全量）。
    :param model_type: "xgboost" 或 "lightgbm"。
    :param output_path: 输出路径，默认 storage/models/highlight_model.xxx。
    :param test_ratio: 验证集比例。
    :returns: (模型路径, 评估指标字典)。
    :raises RuntimeError: 训练数据不足时。
    """
    from Highlight_Model.dataset.builder import DatasetBuilder

    builder = DatasetBuilder(min_positive=5)
    bundle = builder.build(room_id=room_id, preprocess=True)
    if bundle is None:
        raise RuntimeError(
            "训练数据不足，请先使用 Dashboard 审批一些高光候选"
            "（审批结果会自动记录到 threshold_feedback 表）。"
        )

    logger.info(
        "训练集: %d 样本, %d 正样本 (%.0f%%), %d 维特征",
        bundle.n_samples,
        int(bundle.y.sum()),
        bundle.pos_ratio * 100,
        bundle.n_features,
    )

    train_set, val_set = bundle.split(test_ratio=test_ratio)

    if model_type == "xgboost":
        model, metrics = _train_xgboost(train_set, val_set)
    else:
        model, metrics = _train_lightgbm(train_set, val_set)

    # 保存模型
    path = Path(output_path) if output_path else _model_path(model_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_model(model, path, bundle.feature_names, metrics)

    logger.info("模型已保存: %s (AUC=%.3f)", path, metrics.get("auc", 0.0))
    return str(path), metrics


def _train_xgboost(train, val) -> tuple:
    """XGBoost 训练（国产模型优先：CPU 友好，可控）。"""
    import xgboost as xgb

    dtrain = xgb.DMatrix(train.X, label=train.y)
    dval = xgb.DMatrix(val.X, label=val.y)

    scale_pos_weight = max(1.0, (len(train.y) - train.y.sum()) / max(train.y.sum(), 1))

    params = {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "max_depth": 6,
        "learning_rate": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "scale_pos_weight": scale_pos_weight,
        "seed": 42,
        "verbosity": 0,
    }
    evals = [(dtrain, "train"), (dval, "val")]
    model = xgb.train(params, dtrain, num_boost_round=200, evals=evals,
                       early_stopping_rounds=20, verbose_eval=False)

    y_pred = model.predict(dval)
    metrics = _compute_metrics(val.y, y_pred)
    return model, metrics


def _train_lightgbm(train, val) -> tuple:
    """LightGBM 训练（备选方案）。"""
    import lightgbm as lgb

    scale_pos_weight = max(1.0, (len(train.y) - train.y.sum()) / max(train.y.sum(), 1))

    params = {
        "objective": "binary",
        "metric": "auc",
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "scale_pos_weight": scale_pos_weight,
        "seed": 42,
        "verbose": -1,
    }
    dtrain = lgb.Dataset(train.X, label=train.y)
    dval = lgb.Dataset(val.X, label=val.y, reference=dtrain)

    model = lgb.train(params, dtrain, num_boost_round=200,
                       valid_sets=[dtrain, dval],
                       callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)])

    y_pred = model.predict(val.X)
    metrics = _compute_metrics(val.y, y_pred)
    return model, metrics


def _compute_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    """计算 AUC / F1 / Precision / Recall / Accuracy。"""
    try:
        from sklearn.metrics import (
            accuracy_score, auc, f1_score,
            precision_score, recall_score, roc_auc_score,
        )
    except ImportError:
        logger.warning("sklearn 未安装，仅返回简易指标")
        pred_bin = (y_pred > 0.5).astype(int)
        acc = float(np.mean(pred_bin == y_true))
        return {"accuracy": acc, "auc": 0.5}

    pred_bin = (y_pred > 0.5).astype(int)
    try:
        auc_val = float(roc_auc_score(y_true, y_pred))
    except Exception:
        auc_val = 0.5

    return {
        "auc": round(auc_val, 4),
        "f1": round(float(f1_score(y_true, pred_bin)), 4),
        "precision": round(float(precision_score(y_true, pred_bin)), 4),
        "recall": round(float(recall_score(y_true, pred_bin)), 4),
        "accuracy": round(float(accuracy_score(y_true, pred_bin)), 4),
    }


def _model_path(model_type: str) -> Path:
    ext = "pkl" if model_type == "xgboost" else "txt"
    return _MODEL_DIR / f"highlight_model.{ext}"


def _save_model(model, path: Path, feature_names: list[str], metrics: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(model, "save_model"):
        # XGBoost native format
        model.save_model(str(path))
    elif hasattr(model, "booster_"):
        # sklearn-style XGBClassifier
        import pickle as _pkl
        with open(path, "wb") as f:
            _pkl.dump(model, f)
    else:
        # LightGBM or other
        import pickle as _pkl
        with open(path, "wb") as f:
            _pkl.dump(model, f)

    # 保存元数据
    meta_path = path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps({
        "feature_names": feature_names,
        "n_features": len(feature_names),
        "metrics": metrics,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("模型元数据已保存: %s", meta_path)
