"""ASR 转写子系统 (V0.1.14).

实现代码保持于 app.analysis.transcribe.py 以确保完全向后兼容。
本子包提供模块化入口，供未来渐进迁移。
"""

from app.analysis.transcribe import (  # noqa: F401
    ASRPipeline,
    ASRSegmentResult,
    ASRTranscriptResult,
    EmotionEvent,
    FasterWhisperBackend,
    FunASRBackend,
    TranscriberBackend,
    TranscriptionResult,
    Word,
    _compute_real_time_factor,
    _compute_review_risk_score,
    _levenshtein_distance,
    _merge_review_text,
    _normalize_confidence_sentence,
    _normalize_whisper_logprob,
    _probe_audio_duration,
    _segment_to_confidence,
    _unified_to_legacy,
    get_default_pipeline,
    transcribe_segment,
)
