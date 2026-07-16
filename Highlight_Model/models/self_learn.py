"""自学习引擎 (v0.1.13.1-HL-Alpha)。

自动从 ThresholdFeedback 表提取所有已审批样本的特征，
训练 XGBoost/LightGBM 模型，保存到 storage/models/。

支持：
- 全量训练：使用全部 ThresholdFeedback 数据从头训练
- 增量训练：从现有模型的基础上追加新数据微调（warm-start）
- 定时触发：可设置间隔自动执行（通过 CLI/Web 控制）
- 状态追踪：训练进度、指标、最后训练时间持久化

国产模型策略：XGBoost 主模型（纯本地 CPU，零 API 费用）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# 模型与状态存储路径
_MODEL_DIR = Path("storage/models")
_MODEL_PATH = _MODEL_DIR / "highlight_model.pkl"
_META_PATH = _MODEL_DIR / "highlight_model.meta.json"
_STATE_PATH = _MODEL_DIR / "self_learn_state.json"


@dataclass(slots=True)
class SelfLearnResult:
    """一次自学习迭代的结果。"""

    success: bool
    model_path: str = ""
    n_samples: int = 0
    n_positive: int = 0
    n_new: int = 0  # 本迭代新增样本数
    metrics: dict[str, float] = field(default_factory=dict)
    iteration: int = 0
    elapsed_s: float = 0.0
    error: str = ""


class SelfLearnEngine:
    """自学习引擎 — 自动从反馈表构建数据集并训练模型。

    :param min_positive: 最少正样本数（低于此值不训练）。
    :param model_type: "xgboost" 或 "lightgbm"。
    :param test_ratio: 验证集比例。
    :param auto_incremental: 是否启用增量训练（持续追加新样本）。
    """

    def __init__(
        self,
        min_positive: int = 5,
        model_type: str = "xgboost",
        test_ratio: float = 0.2,
        auto_incremental: bool = True,
    ) -> None:
        self.min_positive = min_positive
        self.model_type = model_type
        self.test_ratio = test_ratio
        self.auto_incremental = auto_incremental
        self._state = _load_state()

    # ------------------------------------------------------------------ #
    # 主入口 — 触发一次自学习迭代
    # ------------------------------------------------------------------ #
    def run(self, room_id: int | None = None) -> SelfLearnResult:
        """执行一次自学习迭代。

        :param room_id: 可选，限定某房间。
        :returns: 本次迭代结果。
        """
        t0 = time.monotonic()
        result = SelfLearnResult(success=False, iteration=self._state.get("iteration", 0) + 1)

        # 1) 收集数据
        from Highlight_Model.dataset.shared import load_feedback, candidate_to_segment
        records = load_feedback(room_id)
        if len(records) < self.min_positive * 2:
            result.error = f"样本不足（需 ≥{self.min_positive * 2}，当前 {len(records)}）"
            logger.warning(result.error)
            _save_result(result)
            return result

        positives = [r for r in records if r["action"] == "approved"]
        if len(positives) < self.min_positive:
            result.error = f"正样本不足（需 ≥{self.min_positive}，当前 {len(positives)}）"
            logger.warning(result.error)
            _save_result(result)
            return result

        # 数据质量检查
        try:
            from Highlight_Model.dataset.guard import DataQualityGuard
            guard = DataQualityGuard()
            qr = guard.check_feedback(records)
            if qr.conflicts > len(records) * 0.1:  # >10% 冲突 → 警告
                logger.warning("数据质量: 标签冲突=%d/%d", qr.conflicts, len(records))
            for issue in qr.issues:
                logger.warning("数据质量问题: %s", issue)
        except Exception:
            pass

        # 2) 提取特征
        from Highlight_Model.feature_extractor.base import FeatureExtractor
        extractor = FeatureExtractor()

        X_list, y_list, ids = [], [], []
        for rec in records:
            seg_id = _candidate_to_segment(rec.get("candidate_id", 0))
            if seg_id is None:
                continue
            try:
                vec = extractor.extract(seg_id)
            except Exception as exc:
                logger.debug("特征提取失败 candidate=%s: %s", rec["candidate_id"], exc)
                continue
            X_list.append(vec)
            y_list.append(1 if rec["action"] == "approved" else 0)
            ids.append(rec["candidate_id"])

        if len(X_list) == 0:
            result.error = "无法提取任何有效特征"
            logger.warning(result.error)
            _save_result(result)
            return result

        X = np.stack(X_list)
        y = np.array(y_list, dtype=np.int32)

        # 预处理
        from Highlight_Model.dataset.preprocessor import FeaturePreprocessor
        preprocessor = FeaturePreprocessor()
        X = preprocessor.fit_transform(X)

        # 3) 划分训练/验证集
        bundle = _split_data(X, y, ids, extractor.feature_names, self.test_ratio)
        train_set, val_set = bundle

        # 4) 检查是否需要增量训练
        prev_path = _MODEL_PATH if _MODEL_PATH.exists() else None
        is_incremental = self.auto_incremental and prev_path is not None

        # 5) 训练
        try:
            if self.model_type == "xgboost":
                model, metrics = _train_xgboost(train_set, val_set,
                                                 prev_path if is_incremental else None)
            else:
                model, metrics = _train_lightgbm(train_set, val_set,
                                                  prev_path if is_incremental else None)
        except Exception as exc:
            result.error = f"训练失败: {exc}"
            logger.exception("模型训练异常")
            _save_result(result)
            return result

        # 6) 保存模型 + 元数据 + 注册版本
        _MODEL_DIR.mkdir(parents=True, exist_ok=True)
        _save_model_xgb(model, _MODEL_PATH)

        # 注册到版本管理系统
        try:
            from Highlight_Model.models.registry import ModelRegistry
            registry = ModelRegistry()
            registry.register(str(_MODEL_PATH), metrics, len(ids), int(sum(y)),
                              list(extractor.feature_names))
            # 如果启用 shadow 模式，新训练版本自动设为 shadow 候选
            try:
                from app.core.config import settings as _s
                if _s.ml_shadow_mode and registry.champion is not None:
                    newest = registry.versions[0]
                    if newest.version != registry.champion.version:
                        registry.set_shadow(newest.version)
            except Exception:
                pass
        except Exception as exc:
            logger.debug("模型注册表更新失败(非关键): %s", exc)

        # 设置漂移基线（首次训练或无基线时）
        try:
            from Highlight_Model.models.drift import PredictionDriftDetector
            drift = PredictionDriftDetector()
            y_pred = model.predict(xgb.DMatrix(X)) if self.model_type == "xgboost" else model.predict(X)
            drift.set_baseline(y_pred, X, list(extractor.feature_names))
        except Exception:
            pass

        # 计算新旧样本增量
        prev_sample_ids = set(self._state.get("trained_sample_ids", []))
        new_sample_ids = set(ids)
        n_new = len(new_sample_ids - prev_sample_ids)

        meta = {
            "feature_names": list(extractor.feature_names),
            "n_features": len(extractor.feature_names),
            "metrics": metrics,
            "n_samples": len(ids),
            "n_positive": int(sum(y)),
            "iteration": result.iteration,
            "model_type": self.model_type,
            "is_incremental": is_incremental,
            "n_new_samples": n_new,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "trained_sample_ids": ids,
        }
        _META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        # 保存预处理参数供推理使用
        _save_preprocessor_params(preprocessor, _MODEL_PATH)

        # 更新状态
        self._state.update({
            "iteration": result.iteration,
            "last_trained_at": meta["trained_at"],
            "n_total_samples": len(ids),
            "n_total_positive": int(sum(y)),
            "trained_sample_ids": ids,
            "last_metrics": metrics,
        })
        _save_state(self._state)

        elapsed = time.monotonic() - t0
        result.success = True
        result.model_path = str(_MODEL_PATH)
        result.n_samples = len(ids)
        result.n_positive = int(sum(y))
        result.n_new = n_new
        result.metrics = metrics
        result.elapsed_s = round(elapsed, 1)

        logger.info(
            "自学习迭代 #%d 完成: %d样本(+%d新) AUC=%.3f F1=%.3f 耗时%.1fs",
            result.iteration, result.n_samples, result.n_new,
            metrics.get("auc", 0), metrics.get("f1", 0), elapsed,
        )

        _save_result(result)
        return result

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #
    @property
    def is_model_available(self) -> bool:
        return _MODEL_PATH.exists()

    @property
    def status(self) -> dict:
        """返回当前自学习状态摘要。"""
        return {
            "model_available": self.is_model_available,
            "iteration": self._state.get("iteration", 0),
            "last_trained_at": self._state.get("last_trained_at"),
            "n_total_samples": self._state.get("n_total_samples", 0),
            "n_total_positive": self._state.get("n_total_positive", 0),
            "last_metrics": self._state.get("last_metrics", {}),
            "model_path": str(_MODEL_PATH) if self.is_model_available else "",
            "model_type": self.model_type,
        }

    @property
    def last_result(self) -> SelfLearnResult | None:
        path = _MODEL_DIR / "last_learn_result.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SelfLearnResult(**data)
        except Exception:
            return None


# ------------------------------------------------------------------ #
# 内部辅助
# ------------------------------------------------------------------ #
def _split_data(X, y, ids, names, test_ratio):
    from Highlight_Model.dataset.builder import DatasetBundle
    rng = np.random.RandomState(42)
    idx = rng.permutation(len(y))
    n_test = max(1, int(len(y) * test_ratio))
    test_idx, train_idx = idx[:n_test], idx[n_test:]
    train = DatasetBundle(X[train_idx], y[train_idx], names, [ids[i] for i in train_idx])
    val = DatasetBundle(X[test_idx], y[test_idx], names, [ids[i] for i in test_idx])
    return train, val


def _train_xgboost(train, val, prev_model_path=None):
    import xgboost as xgb
    dtrain = xgb.DMatrix(train.X, label=train.y)
    dval = xgb.DMatrix(val.X, label=val.y)
    scale = max(1.0, (len(train.y) - train.y.sum()) / max(train.y.sum(), 1))
    params = {
        "objective": "binary:logistic", "eval_metric": "auc",
        "max_depth": 6, "learning_rate": 0.05,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "scale_pos_weight": scale, "seed": 42, "verbosity": 0,
    }
    model = xgb.train(params, dtrain, num_boost_round=200,
                       evals=[(dtrain, "train"), (dval, "val")],
                       early_stopping_rounds=20, verbose_eval=False,
                       xgb_model=prev_model_path)
    y_pred = model.predict(dval)
    metrics = _compute_metrics(val.y, y_pred)
    return model, metrics


def _train_lightgbm(train, val, prev_model_path=None):
    import lightgbm as lgb
    scale = max(1.0, (len(train.y) - train.y.sum()) / max(train.y.sum(), 1))
    params = {
        "objective": "binary", "metric": "auc",
        "num_leaves": 31, "learning_rate": 0.05,
        "feature_fraction": 0.8, "bagging_fraction": 0.8,
        "scale_pos_weight": scale, "seed": 42, "verbose": -1,
    }
    dtrain = lgb.Dataset(train.X, label=train.y)
    dval = lgb.Dataset(val.X, label=val.y, reference=dtrain)
    model = lgb.train(params, dtrain, num_boost_round=200,
                       valid_sets=[dtrain, dval],
                       callbacks=[lgb.early_stopping(20), lgb.log_evaluation(0)],
                       init_model=prev_model_path)
    y_pred = model.predict(val.X)
    metrics = _compute_metrics(val.y, y_pred)
    return model, metrics


def _compute_metrics(y_true, y_pred):
    try:
        from sklearn.metrics import (accuracy_score, f1_score, precision_score,
                                      recall_score, roc_auc_score)
        auc_val = float(roc_auc_score(y_true, y_pred))
        pred_bin = (y_pred > 0.5).astype(int)
        return {
            "auc": round(auc_val, 4),
            "f1": round(float(f1_score(y_true, pred_bin)), 4),
            "precision": round(float(precision_score(y_true, pred_bin)), 4),
            "recall": round(float(recall_score(y_true, pred_bin)), 4),
            "accuracy": round(float(accuracy_score(y_true, pred_bin)), 4),
        }
    except Exception:
        return {"auc": 0.5, "f1": 0.0, "precision": 0.0, "recall": 0.0, "accuracy": 0.0}


def _save_model_xgb(model, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(model, "save_model"):
        model.save_model(str(path))
    else:
        import pickle as _pkl
        path.write_bytes(_pkl.dumps(model))


def _save_preprocessor_params(preprocessor, model_path: Path) -> None:
    """保存 FeaturePreprocessor 参数供推理时加载。"""
    pp_path = model_path.with_suffix(".preprocessor.json")
    pp_path.write_text(json.dumps({
        "impute_values": preprocessor._impute_values.tolist() if preprocessor._impute_values is not None else [],
        "mean": preprocessor._mean.tolist() if preprocessor._mean is not None else [],
        "std": preprocessor._std.tolist() if preprocessor._std is not None else [],
        "impute_strategy": preprocessor.impute_strategy,
    }, ensure_ascii=False), encoding="utf-8")
    logger.info("预处理参数已保存: %s", pp_path)


# ---- 状态持久化 ----
def _load_state() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_result(result: SelfLearnResult) -> None:
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = _MODEL_DIR / "last_learn_result.json"
    path.write_text(json.dumps({
        "success": result.success,
        "model_path": result.model_path,
        "n_samples": result.n_samples,
        "n_positive": result.n_positive,
        "n_new": result.n_new,
        "metrics": result.metrics,
        "iteration": result.iteration,
        "elapsed_s": result.elapsed_s,
        "error": result.error,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
