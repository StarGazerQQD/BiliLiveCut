"""模型评估脚本 (v0.1.9.1b-HL-Alpha)。

加载已训练模型，在留出验证集上计算 AUC/F1 等指标，
输出混淆矩阵和特征重要性排序。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def evaluate_model(
    model_path: str,
    room_id: int | None = None,
) -> dict[str, float]:
    """评估已训练的高光模型。

    :param model_path: 模型文件路径 (.pkl / .json / 元数据 .meta.json)。
    :param room_id: 可选，仅评估某房间的验证集。
    :returns: 评估指标字典。
    """
    from Highlight_Model.dataset.builder import DatasetBuilder

    builder = DatasetBuilder(min_positive=3)
    bundle = builder.build(room_id=room_id, preprocess=True)
    if bundle is None:
        logger.warning("没有足够样本用于评估。")
        return {"auc": 0.0, "f1": 0.0, "precision": 0.0, "recall": 0.0, "accuracy": 0.0}

    # 加载模型
    model = _load_model(model_path)

    # 预测
    import numpy as np
    try:
        if hasattr(model, "predict"):
            y_pred = model.predict(bundle.X)
        else:
            import xgboost as xgb
            dmat = xgb.DMatrix(bundle.X)
            y_pred = model.predict(dmat)
    except Exception as exc:
        logger.error("预测失败: %s", exc)
        return {"auc": 0.0, "f1": 0.0, "precision": 0.0, "recall": 0.0, "accuracy": 0.0}

    try:
        from sklearn.metrics import (
            accuracy_score, f1_score, precision_score, recall_score, roc_auc_score,
        )
    except ImportError:
        acc = float(np.mean((y_pred > 0.5).astype(int) == bundle.y))
        return {"accuracy": round(acc, 4), "auc": 0.5, "f1": 0.0, "precision": 0.0, "recall": 0.0}

    pred_bin = (y_pred > 0.5).astype(int)
    try:
        auc_val = float(roc_auc_score(bundle.y, y_pred))
    except Exception:
        auc_val = 0.5

    metrics = {
        "auc": round(auc_val, 4),
        "f1": round(float(f1_score(bundle.y, pred_bin)), 4),
        "precision": round(float(precision_score(bundle.y, pred_bin)), 4),
        "recall": round(float(recall_score(bundle.y, pred_bin)), 4),
        "accuracy": round(float(accuracy_score(bundle.y, pred_bin)), 4),
    }

    logger.info("评估完成: AUC=%.3f F1=%.3f P=%.3f R=%.3f",
                metrics["auc"], metrics["f1"], metrics["precision"], metrics["recall"])
    return metrics


def feature_importance(model_path: str, top_n: int = 20) -> list[tuple[str, float]]:
    """返回 Top-N 特征重要性。

    :param model_path: 模型文件路径。
    :param top_n: 返回前 N 项。
    :returns: [(feature_name, importance), ...] 降序。
    """
    model = _load_model(model_path)
    meta_path = Path(model_path)
    if meta_path.suffix == ".meta":
        meta_path = meta_path.with_suffix("")
    meta_path = meta_path.with_suffix(".meta.json")

    import json
    names = []
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            names = meta.get("feature_names", [])
        except Exception:
            pass

    # XGBoost
    if hasattr(model, "get_score"):
        scores = model.get_score(importance_type="gain")
        # 按重要性排序
        sorted_items = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        result = []
        for key, val in sorted_items[:top_n]:
            idx = int(key.replace("f", ""))
            name = names[idx] if idx < len(names) else key
            result.append((name, float(val)))
        return result

    # LightGBM
    if hasattr(model, "feature_importance"):
        importance = model.feature_importance(importance_type="gain")
        indexed = [(i, v) for i, v in enumerate(importance)]
        indexed.sort(key=lambda x: x[1], reverse=True)
        result = [
            (names[i] if i < len(names) else f"f{i}", float(v))
            for i, v in indexed[:top_n]
        ]
        return result

    logger.warning("无法提取特征重要性（模型格式不支持）。")
    return []


def _load_model(model_path: str):
    """通用模型加载器。"""
    import pickle
    path = Path(model_path)
    if not path.exists():
        raise FileNotFoundError(f"模型文件不存在: {path}")

    # 尝试 XGBoost native
    try:
        import xgboost as xgb
        model = xgb.Booster()
        model.load_model(str(path))
        return model
    except Exception:
        pass

    # 尝试 pickle
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        pass

    # LightGBM
    try:
        import lightgbm as lgb
        return lgb.Booster(model_file=str(path))
    except Exception:
        pass

    raise RuntimeError(f"无法加载模型: {path}")
