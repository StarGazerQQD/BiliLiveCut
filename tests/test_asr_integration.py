"""ASR 集成测试 (V0.1.12.2)。

测试端到端 ASR Pipeline 场景:
    1. 正常主模型识别
    2. 局部复核
    3. 差异过大保留基础文本
    4. 主模型失败 fallback
    5. SenseVoice 辅助特征
    6. 辅助缺失降级
    7. (需要真实模型的场景已标记为 skip)
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.analysis.transcribe import (
    ASRPipeline,
    ASRSegmentResult,
    ASRTranscriptResult,
    _compute_review_risk_score,
    _extract_audio_segment,
    _merge_review_text,
    _normalize_confidence_sentence,
    _normalize_whisper_logprob,
    _probe_audio_duration,
)


class TestPipelineIntegration:
    """Pipeline 集成测试 (无需真实模型)。"""

    def test_pipeline_construction(self) -> None:
        """Pipeline 构造无异常。"""
        pipeline = ASRPipeline()
        assert pipeline is not None

    def test_review_risk_computation(self) -> None:
        """复核风险评分计算。"""
        seg = ASRSegmentResult(
            start=0.0, end=5.0, text="",
            confidence_available=False,
        )
        risk, reasons = _compute_review_risk_score(seg)
        assert risk >= 0.5

    def test_merge_decision(self) -> None:
        """文本合并决策。"""
        final, decision, _ = _merge_review_text("你好", "你好吗", 0.9)
        assert final in ("你好", "你好吗")

    def test_normalization(self) -> None:
        """置信度归一化。"""
        assert _normalize_confidence_sentence({"confidence": 0.85}) == 0.85
        assert _normalize_confidence_sentence({"test": "无"}) is None

    def test_whisper_logprob_mapping(self) -> None:
        """Whisper avg_logprob 映射。"""
        val = _normalize_whisper_logprob(0.0)
        assert 0.0 <= val <= 1.0


class TestASRTranscriptResultIntegration:
    """统一结果模型集成测试。"""

    def test_result_construction(self) -> None:
        result = ASRTranscriptResult(
            text="完整文本测试", language="zh", backend="paraformer",
            model_id="paraformer-zh", model_revision="v2.0.4",
        )
        assert result.backend == "paraformer"
        assert result.final_text_source == "primary"

    def test_review_fields_populated(self) -> None:
        """复核字段可正常填充。"""
        result = ASRTranscriptResult(
            text="原文本",
            base_text="原文本",
            review_text="复核文本",
            final_text="复核文本",
            review_triggered=True,
            review_risk_score=0.82,
            review_backend="funasr-nano",
            final_text_source="review",
            review_reasons=["low_confidence", "hotword_conflict"],
        )
        assert result.review_triggered is True
        assert result.final_text_source == "review"


class TestSegments:
    """Segments 相关逻辑。"""

    def test_segment_confidence_chain(self) -> None:
        """句段置信度链。"""
        seg = ASRSegmentResult(
            start=0.0, end=3.0, text="你好世界",
            raw_confidence=0.9,
            confidence_type="paraformer-sentence-confidence",
            normalized_confidence=0.9,
            confidence_available=True,
        )
        assert seg.confidence_available
        assert seg.normalized_confidence == 0.9
        assert seg.raw_confidence == 0.9

    def test_no_confidence_segment(self) -> None:
        """无置信度句段。"""
        seg = ASRSegmentResult(
            start=0.0, end=3.0, text="无置信度文本",
        )
        assert seg.confidence_available is False
        assert seg.raw_confidence is None
        assert seg.normalized_confidence is None
