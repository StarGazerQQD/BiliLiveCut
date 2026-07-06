"""模型推理接口 (v0.1.13.1-HL-Alpha)。

加载训练好的 XGBoost/LightGBM 模型，提供 predict_proba 接口
供母仓库 score_segment() 可插拔调用。纯本地推理，无 API 依赖。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class ModelInference:
    """高光模型推理器。

    加载序列化模型 + 元数据，对外暴露 predict_proba 接口。

    :param model_path: 模型文件路径 (.pkl / .txt)。
    :param threshold: 分类阈值（默认 0.5）。
    """

    def __init__(self, model_path: str | Path = "",
                 threshold: float = 0.5) -> None:
        self.model_path = Path(model_path) if model_path else Path("storage/models/highlight_model.pkl")
        self.threshold = threshold
        self._model: object | None = None
        self._loaded = False
        self._feature_names: list[str] = []
        self._meta: dict = {}

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def feature_names(self) -> list[str]:
        return list(self._feature_names)

    def load(self) -> None:
        """从磁盘加载模型。

        :raises FileNotFoundError: 模型文件不存在。
        """
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"模型文件不存在: {self.model_path}\n"
                "请先运行 train_model() 训练模型，或在 Dashboard 审批足量候选后重新训练。"
            )

        try:
            import xgboost as xgb
            self._model = xgb.Booster()
            self._model.load_model(str(self.model_path))
        except Exception:
            try:
                import pickle
                with open(self.model_path, "rb") as f:
                    self._model = pickle.load(f)
            except Exception:
                try:
                    import lightgbm as lgb
                    self._model = lgb.Booster(model_file=str(self.model_path))
                except Exception as exc:
                    raise RuntimeError(f"无法加载模型: {exc}") from exc

        # 加载元数据（含特征名和预处理参数）
        meta_path = self.model_path.with_suffix(".meta.json")
        if meta_path.exists():
            try:
                self._meta = json.loads(meta_path.read_text(encoding="utf-8"))
                self._feature_names = self._meta.get("feature_names", [])
            except Exception:
                pass

        # 加载预处理器参数（训练时拟合的均值/标准差/填充值）
        pp_path = self.model_path.with_suffix(".preprocessor.json")
        self._impute_values: np.ndarray | None = None
        self._pp_mean: np.ndarray | None = None
        self._pp_std: np.ndarray | None = None
        if pp_path.exists():
            try:
                pp_meta = json.loads(pp_path.read_text(encoding="utf-8"))
                self._impute_values = np.array(pp_meta.get("impute_values", []), dtype=np.float64)
                self._pp_mean = np.array(pp_meta.get("mean", []), dtype=np.float64)
                self._pp_std = np.array(pp_meta.get("std", []), dtype=np.float64)
                logger.info("预处理参数已加载 n_features=%d", len(self._impute_values))
            except Exception as exc:
                logger.warning("预处理参数加载失败: %s", exc)

        self._loaded = True
        logger.info("模型加载成功: %s (n_features=%d)",
                    self.model_path, len(self._feature_names))

    def predict_proba(self, segment_id: int) -> float:
        """对指定片段预测高光概率 (0-1)。

        :param segment_id: raw_segments 主键。
        :returns: 高光概率值 (0-1)。
        :raises RuntimeError: 模型未加载时。
        """
        if not self._loaded:
            self.load()

        from Highlight_Model.feature_extractor.base import FeatureExtractor
        extractor = FeatureExtractor()
        feats = extractor.extract(segment_id).reshape(1, -1).astype(np.float64)

        # 应用与训练时一致的预处理
        feats = self._apply_preprocessing(feats)

        try:
            import xgboost as xgb
            dmat = xgb.DMatrix(feats.astype(np.float32))
            prob = float(self._model.predict(dmat)[0])
        except Exception:
            if hasattr(self._model, "predict_proba"):
                prob = float(self._model.predict_proba(feats.astype(np.float32))[0, 1])
            elif hasattr(self._model, "predict"):
                prob = float(self._model.predict(feats.astype(np.float32))[0])
            else:
                prob = 0.0

        return float(np.clip(prob, 0.0, 1.0))

    def _apply_preprocessing(self, X: np.ndarray) -> np.ndarray:  # noqa: N803
        """对推理特征应用训练时的预处理参数。

        :param X: shape (1, n_features) 原始特征。
        :returns: 预处理后的特征。
        """
        if self._impute_values is None or len(self._impute_values) == 0:
            return X.astype(np.float64)
        # 填充缺失值
        X = np.where(np.isnan(X), self._impute_values[:X.shape[1]], X)
        # Z-score 标准化
        if self._pp_mean is not None and self._pp_std is not None:
            m = self._pp_mean[:X.shape[1]]
            s = self._pp_std[:X.shape[1]]
            s = np.where(s < 1e-8, 1.0, s)
            X = (X - m) / s
        return X.astype(np.float64)

    def predict(self, segment_id: int) -> bool:
        """对指定片段预测是否高光 (阈值二值化)。

        :param segment_id: raw_segments 主键。
        :returns: True=高光。
        """
        return self.predict_proba(segment_id) >= self.threshold

    def batch_predict(self, segment_ids: list[int]) -> np.ndarray:
        """批量预测。

        :param segment_ids: raw_segments 主键列表。
        :returns: shape (n,) 的概率数组。
        """
        return np.array([self.predict_proba(sid) for sid in segment_ids],
                        dtype=np.float32)

    @property
    def training_metrics(self) -> dict:
        """返回模型训练时的评估指标。"""
        return dict(self._meta.get("metrics", {}))
