"""高光评分插件的公共数据契约。"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal, Protocol, TypeAlias, runtime_checkable

ScoringMode: TypeAlias = Literal["off", "shadow", "champion"]
RoomScoringMode: TypeAlias = Literal["inherit", "off", "shadow", "champion"]
HighlightLabel: TypeAlias = Literal[0, 1]


@dataclass(frozen=True, slots=True)
class HighlightWord:
    """宿主提供的词级转写时间戳。"""

    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class HighlightDanmaku:
    """特征窗口中的只读弹幕快照。"""

    ts: datetime
    content: str
    user: str | None
    value: float


@dataclass(frozen=True, slots=True)
class HighlightAudio:
    """宿主已完成解码的聚合音频特征。"""

    rms_peak: float
    rms_median: float
    rms_std: float
    prominence: float
    silence_ratio: float


@dataclass(frozen=True, slots=True)
class HighlightScoringRequest:
    """宿主交给高光评分插件的完整、无 ORM 输入。"""

    segment_id: int
    session_id: int
    room_id: int
    start_ts: datetime
    end_ts: datetime
    session_started_at: datetime
    duration_s: float
    file_path: str
    transcript_text: str | None
    words: tuple[HighlightWord, ...] | None
    asr_avg_logprob: float | None
    asr_review_risk: float | None
    auxiliary: dict[str, object] | None
    window_danmaku: tuple[HighlightDanmaku, ...]
    baseline_danmaku: tuple[HighlightDanmaku, ...]
    audio: HighlightAudio | None
    rule_score: float
    room_mode: RoomScoringMode = "inherit"

    def __post_init__(self) -> None:
        """拒绝无效时间边界和非有限主评分。"""
        if self.end_ts <= self.start_ts or self.duration_s <= 0:
            raise ValueError("片段时间边界和 duration_s 必须有效")
        if not math.isfinite(self.rule_score):
            raise ValueError("rule_score 必须是有限数")


@dataclass(frozen=True, slots=True)
class HighlightScoringResult:
    """高光插件返回的概率、模型身份和显式回退信息。"""

    requested_mode: ScoringMode
    effective_mode: ScoringMode
    champion_version: int | None = None
    champion_probability: float | None = None
    champion_threshold: float | None = None
    shadow_version: int | None = None
    shadow_probability: float | None = None
    schema_version: str | None = None
    schema_fingerprint: str | None = None
    feature_values: dict[str, float | None] | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    error: str | None = None

    def __post_init__(self) -> None:
        """拒绝会污染主评分的非有限或越界概率。"""
        for name in ("champion_probability", "champion_threshold", "shadow_probability"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
                raise ValueError(f"{name} 必须位于 [0, 1]")
        if self.effective_mode == "champion" and self.champion_probability is None:
            raise ValueError("Champion 生效时必须返回 champion_probability")
        if self.feature_values is not None:
            for name, value in self.feature_values.items():
                if value is not None and not math.isfinite(value):
                    raise ValueError(f"feature_values.{name} 必须是有限数或 None")
        try:
            json.dumps(
                {"feature_values": self.feature_values, "metadata": self.metadata},
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("高光评分元数据必须是有限、可 JSON 序列化的数据") from exc

    @property
    def attempted(self) -> bool:
        """返回插件是否尝试执行模型推理。"""
        return self.requested_mode != "off"

    @property
    def uses_champion(self) -> bool:
        """返回本次结果是否应替换规则主评分。"""
        return self.effective_mode == "champion" and self.champion_probability is not None

    def to_dict(self) -> dict[str, object]:
        """返回适合候选元数据和日志保存的 JSON 对象。"""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HighlightFeedback:
    """宿主从一次人工审核决策生成的无 ORM 训练反馈。"""

    plugin_id: str
    sample_id: str
    candidate_id: int
    segment_id: int
    session_id: int
    room_id: int
    segment_start_ts: datetime
    label: HighlightLabel | None
    decision: str
    label_source: str
    reviewed_at: datetime
    schema_version: str
    schema_fingerprint: str
    feature_values: dict[str, float | None]

    def __post_init__(self) -> None:
        """拒绝缺少身份、非法标签和不可持久化特征。"""
        for name in ("plugin_id", "sample_id", "decision", "label_source", "schema_version", "schema_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
        for name in ("candidate_id", "segment_id", "session_id", "room_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if self.label is not None and (isinstance(self.label, bool) or self.label not in (0, 1)):
            raise ValueError("label 必须是 0、1 或 None")
        for name, value in self.feature_values.items():
            if not isinstance(name, str) or not name:
                raise ValueError("feature_values 的键必须是非空字符串")
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"feature_values.{name} 必须是数值或 None")
            if value is not None and not math.isfinite(value):
                raise ValueError(f"feature_values.{name} 必须是有限数或 None")
        try:
            json.dumps(asdict(self), ensure_ascii=False, allow_nan=False, default=datetime.isoformat)
        except (TypeError, ValueError) as exc:
            raise ValueError("高光审核反馈必须可 JSON 序列化") from exc


@runtime_checkable
class HighlightScoringPlugin(Protocol):
    """声明 ``highlight_scorer`` capability 的插件必须实现的接口。"""

    def score_highlight(self, request: HighlightScoringRequest) -> HighlightScoringResult:
        """为一个完成转写的片段返回模型评分。"""
        ...

    def record_highlight_feedback(self, feedback: HighlightFeedback) -> None:
        """持久化一条人工审核反馈；相同 sample_id 必须幂等覆盖。"""
        ...


@dataclass(frozen=True, slots=True)
class HighlightDispatch:
    """宿主调度高光插件后的隔离结果。"""

    plugin_id: str
    prediction: HighlightScoringResult | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class HighlightFeedbackDispatch:
    """宿主投递人工审核反馈后的隔离结果。"""

    plugin_id: str
    delivered: bool = False
    error: str | None = None


__all__ = [
    "HighlightAudio",
    "HighlightDanmaku",
    "HighlightDispatch",
    "HighlightFeedback",
    "HighlightFeedbackDispatch",
    "HighlightLabel",
    "HighlightScoringPlugin",
    "HighlightScoringRequest",
    "HighlightScoringResult",
    "HighlightWord",
    "RoomScoringMode",
    "ScoringMode",
]
