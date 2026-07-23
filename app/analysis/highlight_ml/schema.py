"""高光模型特征 Schema、版本和指纹。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """一个连续特征的稳定定义。"""

    name: str
    group: str
    description: str


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    """定义训练与在线推理共享的特征顺序和缺失值语义。"""

    version: str
    specs: tuple[FeatureSpec, ...]

    @property
    def feature_names(self) -> tuple[str, ...]:
        """返回值列与 availability 列交错排列的稳定名称。"""
        return tuple(name for spec in self.specs for name in (spec.name, f"{spec.name}__available"))

    @property
    def fingerprint(self) -> str:
        """返回可检测训练/推理漂移的 SHA-256 指纹。"""
        payload = {
            "version": self.version,
            "missing": "nan_with_per_feature_availability",
            "specs": [{"name": spec.name, "group": spec.group, "description": spec.description} for spec in self.specs],
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def vectorize(self, values: Mapping[str, float | None]) -> np.ndarray:
        """将命名特征编码为 ``float64`` 的 ``NaN + availability`` 向量。"""
        unknown = set(values).difference(spec.name for spec in self.specs)
        if unknown:
            raise ValueError(f"存在未声明的特征: {sorted(unknown)}")
        vector: list[float] = []
        for spec in self.specs:
            value = values.get(spec.name)
            available = value is not None and np.isfinite(value)
            vector.extend((float(value) if available else float("nan"), 1.0 if available else 0.0))
        return np.asarray(vector, dtype=np.float64)


_SPECS = (
    FeatureSpec("duration_s", "temporal", "片段时长（秒）"),
    FeatureSpec("session_elapsed_s", "temporal", "片段开始距会话开始的秒数"),
    FeatureSpec("transcript_char_count", "linguistic", "转写非空白字符数"),
    FeatureSpec("transcript_char_rate", "linguistic", "每秒转写字符数"),
    FeatureSpec("transcript_unique_char_ratio", "linguistic", "非空白字符去重率"),
    FeatureSpec("transcript_exclamation_rate", "linguistic", "感叹号占字符比例"),
    FeatureSpec("transcript_question_rate", "linguistic", "问号占字符比例"),
    FeatureSpec("transcript_laughter_rate", "linguistic", "笑声字符占字符比例"),
    FeatureSpec("word_count", "linguistic", "词时间戳数量"),
    FeatureSpec("word_rate", "linguistic", "每秒词数"),
    FeatureSpec("word_duration_mean", "linguistic", "平均词时长"),
    FeatureSpec("word_duration_std", "linguistic", "词时长标准差"),
    FeatureSpec("pause_mean_s", "linguistic", "相邻词平均停顿"),
    FeatureSpec("pause_max_s", "linguistic", "相邻词最大停顿"),
    FeatureSpec("long_pause_ratio", "linguistic", "超过 0.8 秒的停顿比例"),
    FeatureSpec("asr_avg_logprob", "asr", "ASR 平均对数概率"),
    FeatureSpec("asr_review_risk", "asr", "ASR 复核风险分"),
    FeatureSpec("danmaku_count", "danmaku", "片段窗口普通弹幕数"),
    FeatureSpec("danmaku_rate", "danmaku", "片段窗口每秒弹幕数"),
    FeatureSpec("danmaku_heat_ratio", "danmaku", "窗口弹幕率相对历史基线的平滑比值"),
    FeatureSpec("danmaku_unique_user_ratio", "danmaku", "有用户名弹幕的用户去重率"),
    FeatureSpec("danmaku_unique_content_ratio", "danmaku", "非空弹幕文本去重率"),
    FeatureSpec("danmaku_exclamation_rate", "danmaku", "含感叹号弹幕比例"),
    FeatureSpec("danmaku_question_rate", "danmaku", "含问号弹幕比例"),
    FeatureSpec("danmaku_value_sum", "danmaku", "窗口互动价值权重总和"),
    FeatureSpec("danmaku_value_max", "danmaku", "窗口单条互动价值权重峰值"),
    FeatureSpec("audio_rms_peak", "acoustic", "归一化 RMS 峰值"),
    FeatureSpec("audio_rms_median", "acoustic", "归一化 RMS 中位数"),
    FeatureSpec("audio_rms_std", "acoustic", "归一化 RMS 标准差"),
    FeatureSpec("audio_prominence", "acoustic", "音量峰值相对中位数突出度"),
    FeatureSpec("audio_silence_ratio", "acoustic", "静音时长占比"),
    FeatureSpec("aux_laughter_count", "auxiliary", "SenseVoice 笑声事件数"),
    FeatureSpec("aux_applause_count", "auxiliary", "SenseVoice 掌声事件数"),
    FeatureSpec("aux_surprise_count", "auxiliary", "SenseVoice 惊讶事件数"),
    FeatureSpec("aux_happy_count", "auxiliary", "SenseVoice 开心事件数"),
)

DEFAULT_FEATURE_SCHEMA = FeatureSchema(version="1.0.0", specs=_SPECS)
