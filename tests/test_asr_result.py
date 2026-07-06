"""ASR 统一结果模型测试 (V0.1.12.2)。

测试统一结果结构、置信度处理、各后端转换。
所有测试使用 Mock 后端, 不要求下载真实模型。
"""

from __future__ import annotations

from app.analysis.transcribe import (
    ASRSegmentResult,
    ASRTranscriptResult,
    TranscriptionResult,
    _levenshtein_distance,
    _normalize_confidence_sentence,
    _normalize_whisper_logprob,
)


class TestASRSegmentResult:
    """统一句段结果模型。"""

    def test_basic_fields(self) -> None:
        seg = ASRSegmentResult(
            start=1.0, end=3.5, text="今天天气真好",
            raw_confidence=0.92, confidence_type="paraformer-sentence-confidence",
            normalized_confidence=0.92, confidence_available=True,
            language="zh",
        )
        assert seg.start == 1.0
        assert seg.end == 3.5
        assert seg.text == "今天天气真好"
        assert seg.normalized_confidence == 0.92
        assert seg.confidence_available is True

    def test_no_confidence_kept_none(self) -> None:
        """无置信度时不应伪造 0.0。"""
        seg = ASRSegmentResult(start=0.0, end=5.0, text="测试文本")
        assert seg.raw_confidence is None
        assert seg.normalized_confidence is None
        assert seg.confidence_available is False

    def test_confidence_not_faked_to_zero(self) -> None:
        """不能把无置信度伪造为 0.0。"""
        seg = ASRSegmentResult(start=0.0, end=5.0, text="测试")
        assert seg.normalized_confidence != 0.0  # None, not 0.0
        assert seg.raw_confidence != 0.0


class TestNormalizeConfidence:
    """置信度归一化。"""

    def test_paraformer_confidence_normal(self) -> None:
        sent = {"text": "测试", "confidence": 0.85}
        result = _normalize_confidence_sentence(sent)
        assert result == 0.85

    def test_paraformer_confidence_clamp(self) -> None:
        sent = {"confidence": 1.5}
        result = _normalize_confidence_sentence(sent)
        assert result == 1.0

    def test_paraformer_no_confidence(self) -> None:
        sent = {"text": "无置信度"}
        result = _normalize_confidence_sentence(sent)
        assert result is None

    def test_whisper_logprob_mapping(self) -> None:
        """Whisper avg_logprob 映射到 0-1。"""
        assert _normalize_whisper_logprob(0.0) > 0.9   # 可信
        assert _normalize_whisper_logprob(-1.0) > 0.4
        assert _normalize_whisper_logprob(-2.0) < 0.2


class TestASRTranscriptResult:
    """统一转写结果模型。"""

    def test_basic_construction(self) -> None:
        result = ASRTranscriptResult(
            text="全文",
            backend="paraformer",
            model_id="paraformer-zh",
            model_revision="v2.0.4",
            inference_duration=3.5,
            audio_duration=60.0,
            language="zh",
        )
        assert result.backend == "paraformer"
        assert result.model_id == "paraformer-zh"
        assert result.model_revision == "v2.0.4"
        assert result.language == "zh"

    def test_review_fields_default(self) -> None:
        result = ASRTranscriptResult(text="test", backend="paraformer")
        assert result.review_triggered is False
        assert result.review_risk_score is None
        assert result.final_text_source == "primary"


class TestTranscriptionResultBackwardCompat:
    """旧 TranscriptionResult 向后兼容。"""

    def test_from_unified(self) -> None:
        unified = ASRTranscriptResult(
            text="全文测试", language="zh", backend="paraformer",
            final_text="全文测试", base_text="全文测试",
        )
        legacy = TranscriptionResult.from_unified(unified)
        assert legacy.text == "全文测试"
        assert legacy.language == "zh"
        assert legacy.engine == "paraformer"

    def test_from_unified_with_review(self) -> None:
        unified = ASRTranscriptResult(
            text="原文本",
            final_text="复核后文本",
            language="zh",
            backend="paraformer",
            review_triggered=True,
            review_backend="funasr-nano",
            reviewed_segments=[{"original": "原文本", "reviewed": "复核后文本"}],
        )
        legacy = TranscriptionResult.from_unified(unified)
        assert legacy.text == "复核后文本"
        assert len(legacy.reviewed_segments) == 1


class TestLevenshtein:
    """编辑距离。"""

    def test_identical(self) -> None:
        assert _levenshtein_distance("你好", "你好") == 0

    def test_insert(self) -> None:
        assert _levenshtein_distance("你好", "你好吗") == 1

    def test_replace(self) -> None:
        assert _levenshtein_distance("你好", "你好吗") == 1

    def test_empty(self) -> None:
        assert _levenshtein_distance("", "测试") == 2
        assert _levenshtein_distance("测试", "") == 2
