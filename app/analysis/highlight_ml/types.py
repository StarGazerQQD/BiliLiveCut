"""高光模型数据与共享上下文类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.analysis.highlight_ml.schema import FeatureSchema


@dataclass(frozen=True, slots=True)
class WordTiming:
    """词级转写时间戳。"""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class DanmakuSnapshot:
    """单条弹幕在特征窗口中的只读快照。"""

    ts: datetime
    content: str
    user: str | None
    value: float


@dataclass(frozen=True, slots=True)
class AudioSnapshot:
    """一次音频解码得到的聚合特征，避免各提取器重复读取媒体。"""

    rms_peak: float
    rms_median: float
    rms_std: float
    prominence: float
    silence_ratio: float


@dataclass(frozen=True, slots=True)
class SegmentFeatureContext:
    """一个片段在评分时刻可获得的全部特征输入。"""

    segment_id: int
    session_id: int
    room_id: int
    start_ts: datetime
    end_ts: datetime
    session_started_at: datetime
    duration_s: float
    file_path: str
    transcript_text: str | None
    words: tuple[WordTiming, ...] | None
    asr_avg_logprob: float | None
    asr_review_risk: float | None
    auxiliary: dict[str, object] | None
    window_danmaku: tuple[DanmakuSnapshot, ...]
    baseline_danmaku: tuple[DanmakuSnapshot, ...]
    audio: AudioSnapshot | None


@dataclass(frozen=True, slots=True)
class FeatureRecord:
    """命名特征值；缺失维度保留为 ``None``。"""

    segment_id: int
    schema_version: str
    schema_fingerprint: str
    values: dict[str, float | None]

    def vector(self, schema: FeatureSchema) -> np.ndarray:
        """按给定 Schema 生成 ``NaN + availability`` 向量。"""
        if self.schema_fingerprint != schema.fingerprint:
            raise ValueError("特征记录与目标 Schema 指纹不一致")
        return schema.vectorize(self.values)


@dataclass(frozen=True, slots=True)
class LabeledSample:
    """一个经过明确人工决断的监督样本。"""

    sample_id: str
    segment_id: int
    session_id: int
    room_id: int
    segment_start_ts: datetime
    label: int
    label_source: str
    observed_at: datetime
    features: FeatureRecord


@dataclass(frozen=True, slots=True)
class BlindReviewItem:
    """从未审核片段中抽取的盲审项，不自动赋予负标签。"""

    segment_id: int
    session_id: int
    start_ts: datetime
    end_ts: datetime


@dataclass(frozen=True, slots=True)
class DatasetBundle:
    """监督数据及用于纠正选择偏差的盲审队列。"""

    schema_version: str
    schema_fingerprint: str
    feature_names: tuple[str, ...]
    samples: tuple[LabeledSample, ...]
    blind_review_queue: tuple[BlindReviewItem, ...]

    @property
    def X(self) -> np.ndarray:
        """返回二维特征矩阵。"""
        if not self.samples:
            return np.empty((0, len(self.feature_names)), dtype=np.float64)
        rows: list[list[float]] = []
        for sample in self.samples:
            if sample.features.schema_fingerprint != self.schema_fingerprint:
                raise ValueError("数据集中存在 Schema 指纹不一致的样本")
            row: list[float] = []
            for name in self.feature_names:
                if name.endswith("__available"):
                    value = sample.features.values.get(name.removesuffix("__available"))
                    row.append(1.0 if value is not None and np.isfinite(value) else 0.0)
                else:
                    value = sample.features.values.get(name)
                    row.append(float(value) if value is not None and np.isfinite(value) else float("nan"))
            rows.append(row)
        return np.asarray(rows, dtype=np.float64)

    @property
    def y(self) -> np.ndarray:
        """返回二分类标签向量。"""
        return np.asarray([sample.label for sample in self.samples], dtype=np.int8)
