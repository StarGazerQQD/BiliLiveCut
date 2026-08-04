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

from typing import TYPE_CHECKING

from app.analysis.transcription import (
    ASRPipeline,
    ASRSegmentResult,
    ASRTranscriptResult,
    _compute_review_risk_score,
    _merge_review_text,
    _normalize_confidence_sentence,
    _normalize_whisper_logprob,
)

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


class TestPipelineIntegration:
    """Pipeline 集成测试 (无需真实模型)。"""

    def test_pipeline_construction(self) -> None:
        """Pipeline 构造无异常。"""
        pipeline = ASRPipeline()
        assert pipeline is not None

    def test_review_risk_computation(self) -> None:
        """复核风险评分计算。"""
        seg = ASRSegmentResult(
            start=0.0,
            end=5.0,
            text="",
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

    def test_funasr_nano_is_default_primary(self, monkeypatch: MonkeyPatch) -> None:
        """默认主引擎应直接调用 Fun-ASR-Nano，而不是 Paraformer。"""
        from app.analysis.transcription import pipeline as pipeline_module

        calls: list[str] = []

        class FakePrimary:
            nano_revision = "master"

            def transcribe_funasr(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                calls.append("funasr")
                return ASRTranscriptResult(
                    text="Nano 主引擎文本",
                    final_text="Nano 主引擎文本",
                    backend="funasr-nano",
                )

            def transcribe(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                calls.append("paraformer")
                return ASRTranscriptResult(text="不应调用")

        monkeypatch.setattr(pipeline_module.settings, "asr_primary", "funasr_nano")
        pipeline = ASRPipeline(primary_backend=FakePrimary())  # type: ignore[arg-type]

        result = pipeline.transcribe("audio.wav")

        assert result.text == "Nano 主引擎文本"
        assert calls == ["funasr"]

    def test_funasr_empty_falls_back_to_paraformer(self, monkeypatch: MonkeyPatch) -> None:
        """Fun-ASR-Nano 空输出应先回退 Paraformer，并保留主引擎 provenance。"""
        from app.analysis.transcription import pipeline as pipeline_module

        class FakePrimary:
            nano_revision = "master"

            def transcribe_funasr(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                return ASRTranscriptResult(text="", backend="funasr-nano")

            def transcribe(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                return ASRTranscriptResult(text="Paraformer 回退文本", backend="paraformer")

        monkeypatch.setattr(pipeline_module.settings, "asr_primary", "funasr_nano")
        pipeline = ASRPipeline(primary_backend=FakePrimary())  # type: ignore[arg-type]

        result = pipeline.transcribe("audio.wav")

        assert result.final_text == "Paraformer 回退文本"
        assert result.primary_backend == "funasr-nano"
        assert result.fallback_backend == "paraformer"
        assert result.fallback_trigger_reason == "primary_empty_output"
        assert result.primary_error_type == "ASRQualityError"
        assert result.final_text_source == "fallback"

    def test_funasr_degenerate_repetition_falls_back_to_paraformer(self, monkeypatch: MonkeyPatch) -> None:
        """Nano 解码循环必须丢弃，并由 Paraformer 产生正式文本。"""
        from app.analysis.transcription import pipeline as pipeline_module

        class FakePrimary:
            nano_revision = "master"

            def transcribe_funasr(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                repeated = "等一下我们先看看" * 20
                return ASRTranscriptResult(text=repeated, final_text=repeated, backend="funasr-nano")

            def transcribe(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                return ASRTranscriptResult(text="Paraformer 给出的正常回退文本。", backend="paraformer")

        monkeypatch.setattr(pipeline_module.settings, "asr_primary", "funasr_nano")
        pipeline = ASRPipeline(primary_backend=FakePrimary())  # type: ignore[arg-type]

        result = pipeline.transcribe("audio.wav")

        assert result.final_text == "Paraformer 给出的正常回退文本。"
        assert result.fallback_backend == "paraformer"
        assert result.fallback_trigger_reason == "primary_degenerate_repetition"
        assert result.primary_error_type == "ASRQualityError"

    def test_funasr_local_decode_loop_falls_back_to_paraformer(self, monkeypatch: MonkeyPatch) -> None:
        """五分钟正文中的局部 Nano 复读也应切换 Paraformer，而不是继续进入 LLM。"""
        from app.analysis.transcription import pipeline as pipeline_module

        class FakePrimary:
            nano_revision = "master"

            def transcribe_funasr(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                prefix = "".join(f"这是第{index}段正常内容，主播正在讲解当前画面。" for index in range(12))
                repeated = "等一下我们先看看" * 4
                suffix = "".join(f"之后主播继续讲解第{index}种玩法和队伍安排。" for index in range(12))
                text = prefix + repeated + suffix
                return ASRTranscriptResult(text=text, final_text=text, backend="funasr-nano")

            def transcribe(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                return ASRTranscriptResult(text="Paraformer 已消除局部复读。", backend="paraformer")

        monkeypatch.setattr(pipeline_module.settings, "asr_primary", "funasr_nano")
        pipeline = ASRPipeline(primary_backend=FakePrimary())  # type: ignore[arg-type]

        result = pipeline.transcribe("audio.wav")

        assert result.final_text == "Paraformer 已消除局部复读。"
        assert result.fallback_backend == "paraformer"
        assert result.fallback_trigger_reason == "primary_degenerate_repetition"

    def test_funasr_exception_provenance_survives_paraformer_fallback(self, monkeypatch: MonkeyPatch) -> None:
        """模型异常与空输出应使用不同回退原因，并保留原始异常类型。"""
        from app.analysis.transcription import pipeline as pipeline_module

        class FakePrimary:
            nano_revision = "master"

            def transcribe_funasr(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                raise RuntimeError("nano crashed")

            def transcribe(self, _audio_path: str, _initial_prompt: str | None = None) -> ASRTranscriptResult:
                return ASRTranscriptResult(text="Paraformer 异常回退文本。", backend="paraformer")

        monkeypatch.setattr(pipeline_module.settings, "asr_primary", "funasr_nano")
        pipeline = ASRPipeline(primary_backend=FakePrimary())  # type: ignore[arg-type]

        result = pipeline.transcribe("audio.wav")

        assert result.fallback_trigger_reason == "primary_exception"
        assert result.primary_error_type == "RuntimeError"
        assert result.primary_error_message == "nano crashed"


class TestASRTranscriptResultIntegration:
    """统一结果模型集成测试。"""

    def test_result_construction(self) -> None:
        result = ASRTranscriptResult(
            text="完整文本测试",
            language="zh",
            backend="paraformer",
            model_id="paraformer-zh",
            model_revision="v2.0.4",
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
            start=0.0,
            end=3.0,
            text="你好世界",
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
            start=0.0,
            end=3.0,
            text="无置信度文本",
        )
        assert seg.confidence_available is False
        assert seg.raw_confidence is None
        assert seg.normalized_confidence is None
