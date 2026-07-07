"""多引擎 ASR 流水线 (V0.1.12.2 重构)。

架构:
    音频
    ├─ Paraformer-zh : 中文文本、时间戳、标点 (主引擎)
    ├─ SenseVoice-Small : 情感、笑声、音乐、事件 (辅助特征, 与主引擎并行)
    └─ Fun-ASR-Nano : 低置信度 / 非中文片段复核
    └─ Whisper large-v3 / turbo : 保留切换开关, 最终兜底

使用方式:
    backend = FunASRBackend()          # Paraformer + SenseVoice + FunASR-Nano
    pipeline = ASRPipeline(backend)    # 含 Whisper 兜底
    result = pipeline.transcribe(audio_path)  # 返回 ASRTranscriptResult

V0.1.12.2 变更:
    - 新增 ASRSegmentResult / ASRTranscriptResult 统一结果结构, 消除各后端置信度歧义。
    - 不再给无置信度字段伪造 0.0, 改为 None。
    - 保留 TranscriptionResult 作为向后兼容层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# ═══════════════════════════════════════════════════════════
# 统一 ASR 结果模型 (V0.1.12.2)
# ═══════════════════════════════════════════════════════════


@dataclass(slots=True)
class Word:
    """一个词及其时间戳(秒, 相对片段起点)。"""

    word: str
    start: float
    end: float


@dataclass(slots=True)
class EmotionEvent:
    """SenseVoice 检测到的辅助事件 (V0.1.12.2: 必须有真实时间范围)。"""

    event_type: str  # "laughter" / "music" / "applause" / "emotion:HAPPY" / ...
    start: float  # 秒, 不得为 0.0 除非整个音频为此事件 (需明确标记)
    end: float
    confidence: float = 1.0


@dataclass(slots=True)
class ASRSegmentResult:
    """单句 ASR 结果 (V0.1.12.2 新增)。

    不同后端置信度定义不同, 通过 ``confidence_type`` 区分:
      - ``paraformer-sentence-confidence``: Paraformer sentence_info[].confidence (0-1)
      - ``avg_logprob``: Whisper segment.avg_logprob (负值, 越大越可信)
      - ``char-confidence``: Fun-ASR-Nano 字级置信度
      - ``none``: 无置信度

    禁止给无置信度的句子伪造 ``raw_confidence = 0.0``。
    """

    start: float
    end: float
    text: str

    raw_confidence: float | None = None
    confidence_type: str | None = None
    normalized_confidence: float | None = None  # 统一 0-1, 越高越可信
    confidence_available: bool = False

    language: str | None = None
    words: list[Word] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass(slots=True)
class ASRTranscriptResult:
    """整段转写结果 (V0.1.12.2 新增)。

    后续高光、复核和运维代码只读取此统一结构。
    """

    text: str  # 全文
    segments: list[ASRSegmentResult] = field(default_factory=list)

    backend: str = ""
    model_id: str = ""
    model_revision: str | None = None

    inference_duration: float = 0.0
    audio_duration: float = 0.0
    real_time_factor: float | None = None

    language: str | None = None
    metadata: dict = field(default_factory=dict)

    # 复核相关 (Phase 2 填充)
    base_text: str = ""  # 主引擎原始文本
    review_text: str = ""  # 复核文本
    final_text: str = ""  # 合并后最终文本
    review_triggered: bool = False
    review_risk_score: float | None = None
    review_reasons: list[str] = field(default_factory=list)
    review_backend: str = ""
    final_text_source: str = "primary"  # "primary" / "review" / "fallback" / "manual_review_needed" / "none"
    reviewed_segments: list[dict] = field(default_factory=list)

    # V0.1.12.4: fallback 追踪
    primary_backend: str = ""
    primary_status: str = ""  # "" / "success" / "failed"
    primary_error_type: str = ""
    primary_error_message: str = ""
    fallback_backend: str = ""
    fallback_trigger_reason: str = ""  # "primary_empty_output" / "primary_exception"

    # 辅助特征
    emotions: list[EmotionEvent] = field(default_factory=list)


def _segment_to_confidence(seg: ASRSegmentResult) -> float | None:
    """获取句子的归一化置信度 (0-1), 无则返回 None。"""
    if seg.confidence_available and seg.normalized_confidence is not None:
        return seg.normalized_confidence
    return None


# ═══════════════════════════════════════════════════════════
# 向后兼容: 保留旧 TranscriptionResult
# ═══════════════════════════════════════════════════════════


@dataclass(slots=True)
class TranscriptionResult:
    """[向后兼容] 旧转写结果, 内部自动从 ASRTranscriptResult 转换。

    V0.1.12.2 新增代码应使用 :class:`ASRTranscriptResult`。
    """

    text: str
    language: str
    words: list[Word] = field(default_factory=list)
    avg_logprob: float = 0.0
    emotions: list[EmotionEvent] = field(default_factory=list)
    reviewed_segments: list[dict] = field(default_factory=list)
    engine: str = "paraformer"

    @classmethod
    def from_unified(cls, unified: ASRTranscriptResult) -> TranscriptionResult:
        """从统一结果转换。"""
        return cls(
            text=unified.final_text or unified.text,
            language=unified.language or "zh",
            words=[w for seg in unified.segments for w in seg.words],
            avg_logprob=(
                unified.segments[0].raw_confidence
                if unified.segments and unified.segments[0].confidence_type == "avg_logprob"
                else 0.0
            ),
            emotions=unified.emotions,
            reviewed_segments=unified.reviewed_segments,
            engine=unified.backend,
        )


def _unified_to_legacy(unified: ASRTranscriptResult) -> TranscriptionResult:
    """快速转换: 统一结果 → 向后兼容结果。"""
    return TranscriptionResult.from_unified(unified)


# ═══════════════════════════════════════════════════════════
# 后端协议
# ═══════════════════════════════════════════════════════════
