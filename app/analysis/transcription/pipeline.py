"""多引擎 ASR 流水线重构)。

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

import json
from functools import lru_cache

from loguru import logger

from app.analysis import asr_metrics

# ── 导入 backends 中的共享工具函数 ──────────────────────────
# Zero-duplicate policy: all utility functions live in backends.py
from app.analysis.transcription.backends import (
    FasterWhisperBackend,
    FunASRBackend,  # noqa: F401
    _cleanup_review_temp,
    _compute_review_risk_score,
    _extract_audio_segment,
    _merge_review_text,
)
from app.analysis.transcription.models import (  # single source — no duplicates
    ASRTranscriptResult,
    TranscriberBackend,
)
from app.core.config import settings
from app.db.models import RawSegment, SegmentStatus, Transcript
from app.db.session import get_session

# ═══════════════════════════════════════════════════════════
# ASR 流水线 (Paraformer → SenseVoice → FunASR → Whisper)
# ═══════════════════════════════════════════════════════════


class ASRPipeline:
    """多引擎 ASR 流水线 (V0.1.14.11 重构)。

    流程:
        1. Paraformer-zh 主引擎转写 (中文文本 + 时间戳 + 标点)
        2. SenseVoice-Small 辅助特征 (情感/笑声/音乐/事件, 已在主引擎内并行调用)
        3. Fun-ASR-Nano review_risk_score 计算 + 局部复核
        4. base/review/final 文本合并
        5. Whisper 兜底 (主引擎失败或无输出时自动切换)
    """

    def __init__(
        self,
        primary_backend: FunASRBackend | None = None,
        whisper_backend: FasterWhisperBackend | None = None,
    ) -> None:
        self._primary = primary_backend
        self._whisper = whisper_backend
        self._use_fallback = settings.asr_fallback_whisper
        self._review_risk_threshold = getattr(
            settings,
            "asr_review_risk_threshold",
            0.65,
        )

    def _get_primary(self) -> FunASRBackend:
        if self._primary is None:
            self._primary = FunASRBackend()
        return self._primary

    def _get_whisper(self) -> FasterWhisperBackend:
        if self._whisper is None:
            self._whisper = FasterWhisperBackend()
        return self._whisper

    def transcribe(
        self,
        audio_path: str,
        initial_prompt: str | None = None,
    ) -> ASRTranscriptResult:
        """执行完整多引擎 ASR 流水线 (V0.1.12.2: 返回统一结果)。

        :param audio_path: 音频文件路径。
        :param initial_prompt: 热词引导。
        :returns: :class:`ASRTranscriptResult`。
        """
        use_paraformer = settings.asr_primary == "paraformer"

        if use_paraformer:
            try:
                result = self._get_primary().transcribe(audio_path, initial_prompt)
            except Exception as exc:
                logger.error("Paraformer 主引擎转写失败: {}", exc)
                result = ASRTranscriptResult(
                    text="",
                    language="zh",
                    backend="paraformer",
                    model_id="paraformer-zh",
                    model_revision=settings.asr_model_revision,
                    primary_status="failed",
                    primary_error_type=type(exc).__name__,
                    primary_error_message=str(exc)[:500],
                )

            # V0.1.12.2: review_risk_score 复核决策
            if settings.asr_funasr_review and result.text:
                result = self._review_loop(result, audio_path, initial_prompt)

            # 主引擎有结果则返回
            if result.final_text and len(result.final_text.strip()) > 0:
                return result
            if result.text and len(result.text.strip()) > 0:
                return result

            # 主引擎空结果 → 兜底 Whisper
            if self._use_fallback:
                logger.info("Paraformer 无有效输出, 切换 Whisper 兜底")
                fallback_result = self._get_whisper().transcribe(audio_path, initial_prompt)
                # V0.1.12.4: 保留主模型失败信息
                fallback_result.final_text_source = "fallback"
                fallback_result.primary_backend = "paraformer"
                fallback_result.primary_status = "failed"
                fallback_result.fallback_backend = "whisper"
                fallback_result.fallback_trigger_reason = "primary_empty_output"
                fallback_result.review_text = fallback_result.text
                fallback_result.final_text = fallback_result.text
                return fallback_result
            # V0.1.12.4: 即使无兜底, 也标记主模型失败
            result.final_text_source = "none"
            result.primary_status = "failed"
            return result

        # 直接使用 Whisper
        if self._use_fallback:
            return self._get_whisper().transcribe(audio_path, initial_prompt)

        logger.warning("ASR 主引擎未配置且 Whisper 兜底已禁用, 返回空结果")
        return ASRTranscriptResult(text="", language="zh", backend="none")

    def _review_loop(
        self,
        result: ASRTranscriptResult,
        audio_path: str,
        initial_prompt: str | None = None,
    ) -> ASRTranscriptResult:
        """V0.1.12.2: 基于 review_risk_score 的复核闭环。

        1. 对每个 segment 计算 review_risk_score
        2. 高风险 segment 截取局部音频
        3. Fun-ASR-Nano 复核局部音频
        4. 合并 base/review → final_text
        """
        primary = self._get_primary()
        hotwords: list[str] = initial_prompt.split(", ") if initial_prompt else []

        # 初始化
        result.base_text = result.text
        reviewed_segments: list[dict] = []
        final_segments: list[str] = []

        for seg in result.segments:
            risk_score, risk_reasons = _compute_review_risk_score(seg, hotwords)
            seg.metadata["review_risk_score"] = risk_score
            seg.metadata["review_reasons"] = risk_reasons

            if risk_score < self._review_risk_threshold:
                final_segments.append(seg.text)
                # 低风险句子不记录 reviewed_segments
                continue

            result.review_triggered = True
            logger.info(
                "触发复核: risk={:.2f} reasons={} text={:.30s}",
                risk_score,
                risk_reasons,
                seg.text,
            )

            # 截取局部音频
            temp_audio = _extract_audio_segment(
                audio_path,
                seg.start,
                seg.end,
                context_s=1.5,
            )
            if temp_audio is None:
                # 截取失败 → 保持原文本, 标记
                final_segments.append(seg.text)
                reviewed_segments.append(
                    {
                        "original": seg.text,
                        "original_risk": risk_score,
                        "reviewed": None,
                        "reviewed_score": None,
                        "start": seg.start,
                        "end": seg.end,
                        "decision": "keep_base",
                        "reason": "extract_failed",
                        "review_backend": "funasr-nano",
                    }
                )
                continue

            try:
                review_result = primary.transcribe_segment(
                    temp_audio,
                    seg.start,
                    seg.end,
                )
                review_text = review_result.text
            except Exception as exc:
                logger.warning("Fun-ASR-Nano 片段复核失败: {}", exc)
                final_segments.append(seg.text)
                _cleanup_review_temp(temp_audio)
                continue

            _cleanup_review_temp(temp_audio)

            if not review_text:
                final_segments.append(seg.text)
                reviewed_segments.append(
                    {
                        "original": seg.text,
                        "original_risk": risk_score,
                        "reviewed": None,
                        "reviewed_score": None,
                        "start": seg.start,
                        "end": seg.end,
                        "decision": "keep_base",
                        "reason": "review_empty",
                        "review_backend": "funasr-nano",
                    }
                )
                continue

            # 合并
            final_txt, decision, merge_reasons = _merge_review_text(
                seg.text,
                review_text,
                risk_score,
                hotwords,
            )
            final_segments.append(final_txt)

            reviewed_segments.append(
                {
                    "original": seg.text,
                    "original_risk": risk_score,
                    "original_reasons": risk_reasons,
                    "reviewed": review_text,
                    "reviewed_score": (
                        review_result.segments[0].normalized_confidence if review_result.segments else None
                    ),
                    "start": seg.start,
                    "end": seg.end,
                    "decision": decision,
                    "reason": merge_reasons,
                    "review_backend": "funasr-nano",
                }
            )

        # 组装最终文本
        final_text = "。".join(final_segments) if final_segments else result.text
        result.final_text = final_text + "。" if final_text and not final_text.endswith("。") else final_text
        result.review_text = result.final_text
        result.reviewed_segments = reviewed_segments
        result.review_backend = "funasr-nano"
        result.review_risk_score = (
            max(seg.metadata.get("review_risk_score", 0.0) for seg in result.segments) if result.segments else None
        )
        result.review_reasons = list(set(r for seg in result.segments for r in seg.metadata.get("review_reasons", [])))

        # V0.1.12.4: 优先级: manual_review_needed > review > fallback > primary
        if any(r.get("decision") == "manual_review_needed" for r in reviewed_segments):
            result.final_text_source = "manual_review_needed"
        elif any(r.get("decision") == "use_review" for r in reviewed_segments):
            result.final_text_source = "review"
        elif not result.final_text_source:
            result.final_text_source = "primary"

        if reviewed_segments:
            logger.info(
                "Fun-ASR-Nano: 复核 {} 个片段, risk_max={:.2f}, final_source={}",
                len(reviewed_segments),
                result.review_risk_score or 0.0,
                result.final_text_source,
            )
            # V0.1.12.3: 记录复核统计
            adopted = result.final_text_source == "review"
            kept_base = result.final_text_source == "primary"
            manual = result.final_text_source == "manual_review_needed"
            asr_metrics.record_review(adopted=adopted, kept_base=kept_base, manual_needed=manual)
            if adopted:
                asr_metrics.record_review_success()
            elif manual:
                asr_metrics.record_review_failure()

        return result


# ═══════════════════════════════════════════════════════════
# 全局 pipeline + transcribe_segment
# ═══════════════════════════════════════════════════════════


@lru_cache(maxsize=1)
def get_default_pipeline() -> ASRPipeline:
    """返回进程级缓存的默认 ASR 流水线。"""
    return ASRPipeline()


def transcribe_segment(
    segment_id: int,
    backend: TranscriberBackend | None = None,
) -> Transcript:
    """转写指定片段并把结果写入数据库 (V0.1.12.2: 记录完整追踪信息)。

    :param segment_id: raw_segments 主键。
    :param backend: 可选转写后端; 默认使用 ASRPipeline。
    :returns: 已写入的 :class:`Transcript`。
    :raises ValueError: 片段不存在时。
    """
    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        file_path = segment.file_path
        initial_prompt = _build_whisper_prompt(db, segment)

    logger.info("开始转写 segment={} -> {}", segment_id, file_path)

    if backend is not None:
        result = backend.transcribe(file_path, initial_prompt=initial_prompt)
    else:
        pipeline = get_default_pipeline()
        result = pipeline.transcribe(file_path, initial_prompt=initial_prompt)

    # 应用房间级 aliases 纠错
    text = _apply_room_aliases(result.text, segment_id)
    final_text = _apply_room_aliases(result.final_text or result.text, segment_id)
    result.final_text = final_text

    words_json = json.dumps(
        [{"w": w.word, "start": w.start, "end": w.end} for seg in result.segments for w in seg.words],
        ensure_ascii=False,
    )

    # V0.1.12.2: 序列化辅助特征 + reviewed_segments
    auxiliary_json: str | None = None
    if result.emotions or result.reviewed_segments:
        auxiliary_json = json.dumps(
            {
                "emotions": [
                    {"type": e.event_type, "start": e.start, "end": e.end, "confidence": e.confidence}
                    for e in result.emotions
                ],
                "reviewed_segments": result.reviewed_segments,
                "engine": result.backend,
            },
            ensure_ascii=False,
        )

    # V0.1.12.2: 记录完整 ASR 追踪
    review_reasons_json = (
        json.dumps(
            result.review_reasons,
            ensure_ascii=False,
        )
        if result.review_reasons
        else None
    )

    avg_logprob_val: float | None = None
    if result.segments:
        first_seg = result.segments[0]
        if first_seg.confidence_type == "avg_logprob" and first_seg.raw_confidence is not None:
            avg_logprob_val = float(first_seg.raw_confidence)
        elif first_seg.normalized_confidence is not None:
            avg_logprob_val = first_seg.normalized_confidence

    transcript = Transcript(
        segment_id=segment_id,
        language=result.language,
        text=final_text or text,
        words_json=words_json,
        avg_logprob=avg_logprob_val,
        auxiliary_json=auxiliary_json,
        # V0.1.12.2 新增字段
        base_text=result.base_text or result.text,
        final_text=final_text or result.text,
        primary_backend=result.backend,
        primary_model_id=result.model_id,
        primary_model_revision=result.model_revision,
        review_backend=result.review_backend or None,
        fallback_backend=result.fallback_backend or None,
        review_triggered=result.review_triggered,
        review_risk_score=result.review_risk_score,
        review_reasons=review_reasons_json,
        final_text_source=result.final_text_source or "primary",
        inference_duration=result.inference_duration,
    )
    with get_session() as db:
        db.add(transcript)
        db.flush()
        db.refresh(transcript)
        seg = db.get(RawSegment, segment_id)
        if seg is not None:
            seg.status = SegmentStatus.TRANSCRIBED
            db.add(seg)
        tid = transcript.id

    logger.info(
        "转写完成 segment={} transcript={} 字数={} 语言={} 引擎={} review={}",
        segment_id,
        tid,
        len(final_text or text),
        result.language,
        result.backend,
        result.review_triggered,
    )
    return transcript


def _build_whisper_prompt(db, segment) -> str | None:
    """从房间配置构建 hotwords prompt。"""
    from app.analysis.room_config import load_room_config
    from app.db.models import LiveRoom, RecordingSession

    session = db.get(RecordingSession, segment.session_id) if segment.session_id else None
    if session is None:
        return None
    room = db.get(LiveRoom, session.room_id) if session.room_id else None
    if room is None:
        return None

    cfg = load_room_config(room)
    hotwords: list[str] = cfg.get("hotwords", [])
    if not hotwords:
        return None
    return ", ".join(hotwords)


def _apply_room_aliases(text: str, segment_id: int) -> str:
    """对转写文本应用房间级 aliases 纠错。"""
    from app.analysis.room_config import apply_aliases, load_room_config
    from app.db.models import LiveRoom, RawSegment, RecordingSession
    from app.db.session import get_session as _gs

    with _gs() as db:
        seg = db.get(RawSegment, segment_id)
        if seg is None:
            return text
        session = db.get(RecordingSession, seg.session_id) if seg.session_id else None
        if session is None:
            return text
        room = db.get(LiveRoom, session.room_id) if session.room_id else None
        if room is None:
            return text
        cfg = load_room_config(room)

    aliases: dict[str, str] = cfg.get("aliases", {})
    if not aliases:
        return text
    return apply_aliases(text, aliases)
