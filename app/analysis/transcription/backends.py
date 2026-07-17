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

import re
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from app.analysis import asr_metrics
from app.core.config import settings

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


class TranscriberBackend(Protocol):
    """转写后端协议 (V0.1.12.2: 主接口返回 ASRTranscriptResult)。"""

    def transcribe(
        self,
        audio_path: str,
        initial_prompt: str | None = None,
    ) -> ASRTranscriptResult:
        """转写音频文件。"""
        ...

    def transcribe_segment(
        self,
        audio_path: str,
        start: float,
        end: float,
    ) -> ASRTranscriptResult:
        """转写音频片段 (用于复核)。"""
        ...


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════


def _compute_real_time_factor(inference_s: float, audio_s: float) -> float | None:
    """计算实时因子 RTF。"""
    if audio_s <= 0:
        return None
    return round(inference_s / audio_s, 4)


def _normalize_confidence_sentence(sent: dict) -> float | None:
    """从 Paraformer sentence_info 提取并归一化置信度 (0-1)。"""
    conf = sent.get("confidence")
    if conf is None:
        return None
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, c))


def _normalize_whisper_logprob(avg_logprob: float) -> float:
    """将 Whisper avg_logprob (通常 -2 到 0) 映射到 0-1。"""
    # avg_logprob 典型范围: -2.0(很不可信) ~ 0.0(可信)
    # 映射: clamp((-logprob + 2)/2, 0, 1) — 不可行因为 logprob 是负数
    # 使用: clamp(logprob + 2, 0, 2) / 2
    return max(0.0, min(1.0, (avg_logprob + 2.0) / 2.0))


def _probe_audio_duration(audio_path: str) -> float:
    """用 ffprobe 探测音频时长。"""
    import subprocess as _sp

    try:
        result = _sp.run(
            [
                settings.ffprobe_path,
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════════════════
# FunASR 多引擎后端 (Paraformer + SenseVoice + FunASR-Nano)
# ═══════════════════════════════════════════════════════════


class FunASRBackend:
    """Paraformer-zh 主引擎 + SenseVoice 辅助 + Fun-ASR-Nano 复核。

    首次调用时懒加载模型, 进程内单例缓存。

    :param primary: 主引擎模型名, 默认 paraformer-zh。
    :param sensevoice: 是否加载 SenseVoice-Small。
    :param funasr_nano: 是否加载 Fun-ASR-Nano。
    """

    MODEL_ID_PRIMARY = "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
    MODEL_ID_SENSEVOICE = "iic/SenseVoiceSmall"
    MODEL_ID_NANO = "FunAudioLLM/Fun-ASR-Nano-2512"

    def __init__(
        self,
        primary: str | None = None,
        sensevoice: bool | None = None,
        funasr_nano: bool | None = None,
    ) -> None:
        import os as _os

        models_dir = _os.environ.get("BLC_MODELS_DIR", "")
        self._models_dir = models_dir
        self._use_local_models = bool(models_dir) and __import__("pathlib").Path(models_dir).is_dir()

        # When Portable provides local models, use absolute paths
        if self._use_local_models:
            self._primary_model_name = str(__import__("pathlib").Path(models_dir) / "paraformer")
        else:
            self._primary_model_name = primary or self.MODEL_ID_PRIMARY
        self._use_sensevoice = sensevoice if sensevoice is not None else settings.asr_sensevoice
        self._use_funasr = funasr_nano if funasr_nano is not None else settings.asr_funasr_review
        self._primary: object | None = None
        self._sensevoice: object | None = None
        self._funasr: object | None = None

    @property
    def model_revision(self) -> str:
        """当前加载模型的 revision/hash。"""
        return settings.asr_model_revision or "master"

    # ---- 懒加载 ----

    def _load_primary(self) -> object:
        """Load Paraformer-zh main engine (Chinese ASR + punctuation + timestamps).

        When BLC_MODELS_DIR is set (Portable mode), uses absolute local paths
        for model + VAD + punctuation + speaker sub-models, avoiding any network call.
        """
        if self._primary is not None:
            return self._primary
        try:
            from funasr import AutoModel
        except ImportError:
            raise RuntimeError("Need funasr installed. Run: pip install funasr modelscope") from None
        device = settings.asr_primary_device or settings.whisper_device
        revision = settings.asr_model_revision or None

        if self._use_local_models:
            # Portable mode: use Engine Pack local directories
            vad_path = str(Path(self._models_dir) / "paraformer" / "fsmn-vad")
            punc_path = str(Path(self._models_dir) / "paraformer" / "ct-punc")
            spk_path = str(Path(self._models_dir) / "paraformer" / "campplus")
            logger.info("Loading Paraformer from local paths: model=%s vad=%s", self._primary_model_name, vad_path)
            self._primary = AutoModel(
                model=self._primary_model_name,
                vad_model=vad_path,
                punc_model=punc_path,
                spk_model=spk_path,
                device=device,
                hub="ms",
                disable_update=True,
            )
        else:
            logger.info(
                "Loading Paraformer-zh online: model=%s device=%s revision=%s",
                self._primary_model_name,
                device,
                revision,
            )
            self._primary = AutoModel(
                model=self._primary_model_name,
                vad_model="fsmn-vad",
                punc_model="ct-punc",
                spk_model="cam++",
                device=device,
                hub="ms",
                revision=revision,
            )
        asr_metrics.record_backend_call("paraformer", 0, success=True)
        logger.info("Paraformer loaded: device=%s local=%s", device, self._use_local_models)
        return self._primary

    def _load_sensevoice(self) -> object:
        """加载 SenseVoice-Small (情感/笑声/音乐/事件检测)。"""
        if self._sensevoice is not None:
            return self._sensevoice
        try:
            from funasr import AutoModel
        except ImportError:
            raise RuntimeError("需要安装 funasr。请执行: pip install funasr modelscope") from None
        if self._use_local_models:
            sensevoice_path = str(Path(self._models_dir) / "sensevoice")
            logger.info("Loading SenseVoice-Small from local path: %s", sensevoice_path)
            self._sensevoice = AutoModel(
                model=sensevoice_path,
                device=settings.asr_auxiliary_device or settings.whisper_device,
                hub="ms",
                disable_update=True,
            )
        else:
            self._sensevoice = AutoModel(
                model=self.MODEL_ID_SENSEVOICE,
                device=settings.asr_auxiliary_device or settings.whisper_device,
                hub="ms",
                revision=settings.asr_model_revision or None,
            )
        return self._sensevoice

    def _load_funasr(self) -> object:
        """Load Fun-ASR-Nano (low-confidence review)."""
        if self._funasr is not None:
            return self._funasr
        try:
            from funasr import AutoModel
        except ImportError:
            raise RuntimeError("Need funasr. Run: pip install funasr modelscope") from None
        if self._use_local_models:
            nano_path = str(Path(self._models_dir) / "funasr_nano")
            logger.info("Loading Fun-ASR-Nano from local path: %s", nano_path)
            self._funasr = AutoModel(
                model=nano_path,
                device=settings.asr_review_device or settings.whisper_device,
                hub="ms",
                disable_update=True,
            )
        else:
            logger.info("Loading Fun-ASR-Nano online revision=%s", settings.asr_model_revision or "None")
            self._funasr = AutoModel(
                model=self.MODEL_ID_NANO,
                device=settings.asr_review_device or settings.whisper_device,
                hub="ms",
                revision=settings.asr_model_revision or None,
            )
        return self._funasr

    # ---- 转写 (V0.1.12.2: 返回 ASRTranscriptResult) ----

    def transcribe(
        self,
        audio_path: str,
        initial_prompt: str | None = None,
    ) -> ASRTranscriptResult:
        """Paraformer 主引擎转写 (V0.1.12.2: 返回统一结果)。

        :param audio_path: 音频文件路径。
        :param initial_prompt: 热词引导 (用于 Paraformer hotword 参数, V0.1.12.2 起生效)。
        :returns: :class:`ASRTranscriptResult`。
        """
        audio_duration = _probe_audio_duration(audio_path)
        model = self._load_primary()
        t0 = time.time()

        # V0.1.12.2: 将热词传入 Paraformer (如果后端支持)
        generate_kwargs: dict = {"input": audio_path}
        if initial_prompt:
            generate_kwargs["hotword"] = initial_prompt

        try:
            result = model.generate(**generate_kwargs)
        except TypeError:
            # hotword 参数不被此版本支持 → 降级
            logger.info("Paraformer 不支持 hotword 参数, 降级为无热词调用")
            generate_kwargs.pop("hotword", None)
            result = model.generate(**generate_kwargs)

        elapsed = time.time() - t0
        logger.info("Paraformer 主引擎转写完成, 耗时 {:.1f}s", elapsed)

        if not result or len(result) == 0:
            asr_metrics.record_backend_call("paraformer", elapsed, success=False)
            return ASRTranscriptResult(
                text="",
                language="zh",
                backend="paraformer",
                model_id=self._primary_model_name,
                model_revision=self.model_revision,
                inference_duration=elapsed,
                audio_duration=audio_duration,
                real_time_factor=_compute_real_time_factor(elapsed, audio_duration),
            )

        asr_metrics.record_backend_call("paraformer", elapsed, success=True)
        if audio_duration > 0:
            asr_metrics.record_rtf(elapsed / audio_duration)

        res = result[0]
        text = res.get("text", "")

        # 构建 Segments
        segments: list[ASRSegmentResult] = []
        sentences = res.get("sentence_info", []) or []
        timestamps = res.get("timestamp", []) or []

        for sent in sentences:
            if not isinstance(sent, dict):
                continue
            sent_text = sent.get("text", "")
            sent_start = sent.get("start", 0.0)  # ms
            sent_end = sent.get("end", 0.0)  # ms
            raw_conf = sent.get("confidence")
            norm_conf = _normalize_confidence_sentence(sent) if raw_conf is not None else None

            segments.append(
                ASRSegmentResult(
                    start=float(sent_start) / 1000.0 if sent_start else 0.0,
                    end=float(sent_end) / 1000.0 if sent_end else 0.0,
                    text=sent_text,
                    raw_confidence=raw_conf,
                    confidence_type="paraformer-sentence-confidence" if raw_conf is not None else None,
                    normalized_confidence=norm_conf,
                    confidence_available=norm_conf is not None,
                    language="zh",
                )
            )

        # 如果 sentence_info 为空, 从 timestamp 和 text 构建单段
        if not segments and text:
            raw_conf = None
            norm_conf = None
            # 尝试从 timestamp 构建 words
            words_out: list[Word] = []
            for ts_item in timestamps:
                if len(ts_item) >= 3:
                    words_out.append(
                        Word(
                            word=str(ts_item[0]),
                            start=float(ts_item[1]) / 1000.0,
                            end=float(ts_item[2]) / 1000.0,
                        )
                    )
            segments.append(
                ASRSegmentResult(
                    start=0.0,
                    end=audio_duration,
                    text=text,
                    raw_confidence=raw_conf,
                    confidence_type=None,
                    normalized_confidence=norm_conf,
                    confidence_available=False,
                    language="zh",
                    words=words_out,
                )
            )

        # 构建词级时间戳 (从原始 timestamp)
        all_words: list[Word] = []
        for ts_item in timestamps:
            if len(ts_item) >= 3:
                all_words.append(
                    Word(
                        word=str(ts_item[0]),
                        start=float(ts_item[1]) / 1000.0,
                        end=float(ts_item[2]) / 1000.0,
                    )
                )
        if all_words and segments:
            segments[0].words = all_words

        # 检测辅助特征
        emotions: list[EmotionEvent] = []
        if self._use_sensevoice:
            try:
                emotions = self._detect_auxiliary(audio_path)
            except Exception as exc:
                logger.warning("SenseVoice 辅助特征检测失败: {}", exc)

        return ASRTranscriptResult(
            text=text.strip(),
            segments=segments,
            backend="paraformer",
            model_id=self._primary_model_name,
            model_revision=self.model_revision,
            inference_duration=elapsed,
            audio_duration=audio_duration,
            real_time_factor=_compute_real_time_factor(elapsed, audio_duration),
            language="zh",
            emotions=emotions,
        )

    def _detect_auxiliary(self, audio_path: str) -> list[EmotionEvent]:
        """SenseVoice-Small: 检测情感、笑声、音乐、事件 (V0.1.12.2: 解析时间戳)。"""
        sv = self._load_sensevoice()
        t0 = time.time()
        result = sv.generate(input=audio_path)
        elapsed = time.time() - t0
        logger.info("SenseVoice 辅助特征检测完成, 耗时 {:.1f}s", elapsed)

        events: list[EmotionEvent] = []
        if not result or len(result) == 0:
            return events

        res = result[0]
        audio_dur = _probe_audio_duration(audio_path)

        # V0.1.12.2: SenseVoice 时间戳解析 — 若返回 timestamps 则使用,
        # 否则按文本分段估算时间 (比 start=0.0/end=0.0 更合理)。
        _sv_timestamps = res.get("timestamp", []) or []
        sv_text = res.get("text", "")

        # 尝试按文本分段估算事件时间
        parts = re.split(r"[,;，。！？\n]+", sv_text) if sv_text else []
        part_count = max(len(parts), 1)
        part_dur = audio_dur / part_count if audio_dur > 0 else 60.0

        # 提取情感标签
        emo_label = res.get("emo_label", "")
        if emo_label:
            for part_idx, _part_text in enumerate(parts):
                event_start = part_idx * part_dur
                event_end = min(event_start + part_dur, audio_dur) if audio_dur > 0 else 60.0
                for token in emo_label.split("|"):
                    token = token.strip()
                    if not token:
                        continue
                    tag_val = token.split(">", 1)
                    if len(tag_val) == 2:
                        emo_name = tag_val[0].lstrip("<")
                        try:
                            conf = float(tag_val[1])
                        except ValueError:
                            conf = 1.0
                        events.append(
                            EmotionEvent(
                                event_type=f"emotion:{emo_name}",
                                start=event_start,
                                end=event_end,
                                confidence=conf,
                            )
                        )

        # 提取事件标签 (笑声/音乐/鼓掌等)
        event_label = res.get("event_label", "")
        if event_label:
            for part_idx, part_text in enumerate(parts):
                if event_label.lower() in part_text.lower() or any(
                    t.lower() in part_text.lower() for t in event_label.split("|") if t.strip()
                ):
                    event_start = part_idx * part_dur
                    event_end = min(event_start + part_dur, audio_dur) if audio_dur > 0 else 60.0
                    for token in event_label.split("|"):
                        token = token.strip()
                        if not token:
                            continue
                        tag_val = token.split(">", 1)
                        if len(tag_val) == 2:
                            evt_name = tag_val[0].lstrip("<")
                            try:
                                conf = float(tag_val[1])
                            except ValueError:
                                conf = 1.0
                            events.append(
                                EmotionEvent(
                                    event_type=evt_name.lower(),
                                    start=event_start,
                                    end=event_end,
                                    confidence=conf,
                                )
                            )

        # 回退: 如果按分段后仍为空, 给事件分配合理时间范围
        if not events and (emo_label or event_label):
            for token in (emo_label + "|" + event_label).split("|"):
                token = token.strip()
                if not token:
                    continue
                tag_val = token.split(">", 1)
                if len(tag_val) == 2:
                    evt_name = tag_val[0].lstrip("<").lower()
                    try:
                        conf = float(tag_val[1])
                    except ValueError:
                        conf = 1.0
                    events.append(
                        EmotionEvent(
                            event_type=evt_name if not evt_name.startswith("emotion:") else evt_name,
                            start=0.0,
                            end=audio_dur if audio_dur > 0 else 60.0,
                            confidence=conf,
                        )
                    )

        return events

    def transcribe_segment(
        self,
        audio_path: str,
        start: float,
        end: float,
    ) -> ASRTranscriptResult:
        """Fun-ASR-Nano: 对指定音频文件做复核 (V0.1.12.2: 必须传入已截取的局部 WAV)。

        :param audio_path: 已截取的局部音频 WAV 路径。
        :param start: 原音频中的起始秒 (仅用于记录元数据)。
        :param end: 原音频中的结束秒 (仅用于记录元数据)。
        :returns: :class:`ASRTranscriptResult`。
        """
        if not self._use_funasr:
            return ASRTranscriptResult(
                text="",
                language="zh",
                backend="funasr-nano",
                model_id=self.MODEL_ID_NANO,
                model_revision=self.model_revision,
            )
        model = self._load_funasr()
        audio_duration = _probe_audio_duration(audio_path)
        t0 = time.time()
        result = model.generate(input=audio_path)
        elapsed = time.time() - t0
        logger.debug("Fun-ASR-Nano 片段复核完成, 耗时 {:.1f}s", elapsed)
        asr_metrics.record_backend_call("funasr-nano", elapsed, success=bool(result))

        if not result or len(result) == 0:
            return ASRTranscriptResult(
                text="",
                language="zh",
                backend="funasr-nano",
                model_id=self.MODEL_ID_NANO,
                model_revision=self.model_revision,
                inference_duration=elapsed,
                audio_duration=audio_duration,
                real_time_factor=_compute_real_time_factor(elapsed, audio_duration),
            )

        res = result[0]
        text = res.get("text", "")
        char_conf = res.get("confidence", None)

        # 构建 segments
        norm_conf: float | None = None
        if char_conf is not None:
            try:
                norm_conf = max(0.0, min(1.0, float(char_conf)))
            except (TypeError, ValueError):
                norm_conf = None

        segments: list[ASRSegmentResult] = []
        if text:
            segments.append(
                ASRSegmentResult(
                    start=start,
                    end=end,
                    text=text.strip(),
                    raw_confidence=char_conf,
                    confidence_type="nano-char-confidence" if char_conf is not None else None,
                    normalized_confidence=norm_conf,
                    confidence_available=norm_conf is not None,
                    language="zh",
                )
            )

        return ASRTranscriptResult(
            text=text.strip(),
            segments=segments,
            backend="funasr-nano",
            model_id=self.MODEL_ID_NANO,
            model_revision=self.model_revision,
            inference_duration=elapsed,
            audio_duration=audio_duration,
            real_time_factor=_compute_real_time_factor(elapsed, audio_duration),
            language="zh",
        )


# ═══════════════════════════════════════════════════════════
# FasterWhisper 兜底后端 (保留, V0.1.12 作为 fallback)
# ═══════════════════════════════════════════════════════════


class FasterWhisperBackend:
    """基于 faster-whisper 的本地转写后端 (兜底引擎)。"""

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        import os as _os

        models_dir = _os.environ.get("BLC_MODELS_DIR", "")
        if models_dir:
            whisper_local = Path(models_dir) / "whisper"
            if whisper_local.is_dir():
                self.model_size = str(whisper_local)
                logger.info("Whisper: using local Engine Pack model at %s", self.model_size)
            else:
                self.model_size = model_size or settings.whisper_model
        else:
            self.model_size = model_size or settings.whisper_model
        self.device = device or settings.asr_fallback_device or settings.whisper_device
        self.compute_type = compute_type or settings.whisper_compute_type

    def _load_model(self):  # noqa: ANN202
        return _load_whisper_model(self.model_size, self.device, self.compute_type)

    def transcribe(
        self,
        audio_path: str,
        initial_prompt: str | None = None,
    ) -> ASRTranscriptResult:
        """Whisper 转写 (兜底) — V0.1.12.2: 返回统一结果。

        :param audio_path: 文件路径。
        :param initial_prompt: hotwords 引导。
        :returns: :class:`ASRTranscriptResult`。
        """
        audio_duration = _probe_audio_duration(audio_path)
        model = self._load_model()
        kwargs: dict = {"vad_filter": True, "word_timestamps": True}
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        t0 = time.time()
        fw_segments, info = model.transcribe(audio_path, **kwargs)
        elapsed = time.time() - t0

        segments: list[ASRSegmentResult] = []
        all_text: list[str] = []
        for seg in fw_segments:
            seg_words: list[Word] = []
            for w in seg.words or []:
                seg_words.append(Word(word=w.word, start=float(w.start), end=float(w.end)))

            segments.append(
                ASRSegmentResult(
                    start=float(seg.start),
                    end=float(seg.end),
                    text=seg.text.strip(),
                    raw_confidence=seg.avg_logprob,
                    confidence_type="avg_logprob",
                    normalized_confidence=_normalize_whisper_logprob(seg.avg_logprob),
                    confidence_available=True,
                    language=info.language,
                    words=seg_words,
                )
            )
            all_text.append(seg.text)

        full_text = "".join(all_text).strip()
        logger.info("Whisper 兜底转写完成, 耗时 {:.1f}s, 语言={}", elapsed, info.language)
        asr_metrics.record_backend_call("whisper", elapsed, success=True)
        asr_metrics.record_fallback()
        if audio_duration > 0:
            asr_metrics.record_rtf(elapsed / audio_duration)

        return ASRTranscriptResult(
            text=full_text,
            segments=segments,
            backend="whisper",
            model_id=self.model_size,
            model_revision=None,
            inference_duration=elapsed,
            audio_duration=audio_duration,
            real_time_factor=_compute_real_time_factor(elapsed, audio_duration),
            language=info.language,
        )

    def transcribe_segment(
        self,
        audio_path: str,
        start: float,
        end: float,
    ) -> ASRTranscriptResult:
        """Whisper 不支持片段转写, 直接全文转写。"""
        return self.transcribe(audio_path)


@lru_cache(maxsize=2)
def _load_whisper_model(model_size: str, device: str, compute_type: str):  # noqa: ANN202
    """加载并缓存 WhisperModel (进程级, 最多缓存 2 个)。

    V0.1.13: 加载前检查系统资源, 不足时根据 asr_resource_policy 决定抛异常或仅警告。
    """
    # V0.1.13: ASR resource detection before model loading
    from app.core.asr_detection import check_resources_sufficient

    ok, msg = check_resources_sufficient(model_size, device)
    policy = getattr(settings, "asr_resource_policy", "warn")
    if not ok:
        if policy == "strict":
            raise RuntimeError(
                f"ASR 模型加载被阻止: {msg} (asr_resource_policy={policy}). 请增大系统资源或降低模型预设。"
            )
        logger.warning("ASR 资源告警: {} (policy={})", msg, policy)

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            '未安装 faster-whisper。请执行: pip install -e ".[asr]"。'
            "若当前 Python 版本无 ctranslate2 预编译包, 请改用 3.11/3.12 虚拟环境。"
        ) from exc
    logger.info(
        "加载 Whisper 兜底模型 model={} device={} compute={}",
        model_size,
        device,
        compute_type,
    )
    return WhisperModel(model_size, device=device, compute_type=compute_type)


# ═══════════════════════════════════════════════════════════
# 辅助函数: 局部音频截取 + 风险评分 (V0.1.12.2 新增)
# ═══════════════════════════════════════════════════════════


def _extract_audio_segment(
    audio_path: str,
    start: float,
    end: float,
    context_s: float = 1.5,
) -> str | None:
    """用 FFmpeg 从原始音频中截取局部 WAV 用于复核。

    :param audio_path: 原始音频文件路径。
    :param start: 原始音频中起始秒。
    :param end: 原始音频中结束秒。
    :param context_s: 前后上下文秒数。
    :returns: 临时 WAV 路径, 失败返回 None。
    """
    import subprocess as _sp

    audio_dur = _probe_audio_duration(audio_path)
    if audio_dur <= 0:
        logger.warning("无法探测音频时长, 跳过局部截取: {}", audio_path)
        return None

    clip_start = max(0.0, start - context_s)
    clip_end = min(audio_dur, end + context_s)
    duration = clip_end - clip_start
    if duration <= 0.1:
        logger.warning("截取窗口过小 ({:.2f}s), 跳过局部复核", duration)
        return None

    # 专用临时目录
    tmp_dir = Path(settings.storage_root) / "tmp" / "asr_review"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"blc_review_{uuid.uuid4().hex[:12]}.wav"

    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-v",
        "quiet",
        "-ss",
        f"{clip_start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-sample_fmt",
        "s16",
        str(tmp_path),
    ]
    try:
        _sp.run(cmd, check=True, timeout=30, capture_output=True)
    except Exception as exc:
        logger.warning("FFmpeg 局部音频截取失败: {}", exc)
        return None

    if not tmp_path.exists() or tmp_path.stat().st_size < 1000:
        logger.warning("截取音频过小或不存在: {}", tmp_path)
        return None

    return str(tmp_path)


def _cleanup_review_temp(temp_path: str | None) -> None:
    """清理复核临时音频文件。"""
    if temp_path is None:
        return
    try:
        Path(temp_path).unlink(missing_ok=True)
    except OSError:
        pass


def _compute_review_risk_score(
    segment: ASRSegmentResult,
    hotwords: list[str] | None = None,
) -> tuple[float, list[str]]:
    """计算句子的复核风险评分 (0-1), 越高越需要复核。

    综合信号:
    1. 置信度缺失或极低
    2. 空文本或极短
    3. 文本与音频时长比异常
    4. 重复字符/短语比例过高
    5. 乱码/非预期符号
    6. 热词冲突检测

    :param segment: 句子级 ASR 结果。
    :param hotwords: 房间热词列表。
    :returns: ``(risk_score, reasons)``。
    """
    risk = 0.0
    reasons: list[str] = []

    # 1) 置信度
    if not segment.confidence_available:
        risk += 0.15
        reasons.append("confidence_unavailable")
    elif segment.normalized_confidence is not None:
        conf_risk = max(0.0, 1.0 - segment.normalized_confidence)
        if conf_risk > 0.3:
            risk += conf_risk * 0.5
            reasons.append(f"low_confidence({segment.normalized_confidence:.2f})")

    # 2) 空文本或极短
    text = segment.text.strip()
    if not text:
        risk += 0.5
        reasons.append("empty_text")
    elif len(text) <= 2:
        risk += 0.3
        reasons.append("very_short_text")

    # 3) 文本与音频时长比异常
    duration = segment.end - segment.start
    if duration > 0.5 and len(text) > 0:
        chars_per_sec = len(text) / duration
        if chars_per_sec < 0.5:  # 很长的时间只有很少的字
            risk += 0.3
            reasons.append(f"low_chars_per_sec({chars_per_sec:.1f})")
        elif chars_per_sec > 20:  # 很短时间有大量字
            risk += 0.2
            reasons.append(f"high_chars_per_sec({chars_per_sec:.1f})")

    # 4) 重复字符/短语
    if len(text) >= 4:
        unique_ratio = len(set(text)) / len(text)
        if unique_ratio < 0.3:
            risk += 0.3
            reasons.append("high_repetition")

    # 5) 乱码/非预期符号
    non_cjk = sum(1 for c in text if ord(c) < 32 or (ord(c) > 126 and ord(c) < 0x4E00))
    if non_cjk > len(text) * 0.3:
        risk += 0.2
        reasons.append("possible_garbled")

    # 6) 热词冲突: 文本中包含热词前缀但整体不匹配
    if hotwords:
        for hw in hotwords:
            if len(hw) >= 1:
                # 检查热词的任意前缀或单字是否出现在文本中
                for length in range(1, len(hw) + 1):
                    sub = hw[:length]
                    if len(sub) >= 1 and sub in text and hw not in text:
                        risk += 0.15
                        reasons.append(f"hotword_conflict({hw}->{sub})")
                        break
                else:
                    continue
                break

    return min(risk, 1.0), reasons


def _merge_review_text(
    base_text: str,
    review_text: str,
    risk_score: float,
    hotwords: list[str] | None = None,
) -> tuple[str, str, list[str]]:
    """合并基础文本和复核文本, 返回 (final_text, decision, reasons)。

    规则:
    1. review 为空 → keep base
    2. 编辑距离 < 20% → keep base
    3. 热词命中差异 → 选含热词的
    4. 语义变化 > 50% (编辑距离比) → 标记人工确认
    5. 其他 → use review

    :param base_text: Paraformer 原始文本。
    :param review_text: Fun-ASR-Nano 复核文本。
    :param risk_score: 复核风险评分。
    :param hotwords: 房间热词列表。
    :returns: ``(final_text, decision, reasons)``。
    """
    base = base_text.strip()
    review = review_text.strip()
    reasons: list[str] = []

    if not review:
        return base, "keep_base", ["review_empty"]

    # 计算编辑距离
    edit_dist = _levenshtein_distance(base, review)
    max_len = max(len(base), len(review), 1)
    edit_ratio = edit_dist / max_len

    if edit_ratio < 0.2:
        return base, "keep_base", [f"low_edit_distance({edit_ratio:.2f})"]

    # 热词命中差异
    if hotwords:
        base_hits = [hw for hw in hotwords if hw in base]
        review_hits = [hw for hw in hotwords if hw in review]
        if review_hits and not base_hits:
            reasons.append("review_has_hotwords")
            return review, "use_review", reasons
        if base_hits and not review_hits:
            reasons.append("base_has_hotwords")
            return base, "keep_base", reasons

    # 语义变化过大 → 人工确认
    if edit_ratio > 0.5:
        reasons.append(f"high_edit_distance({edit_ratio:.2f})")
        return base, "manual_review_needed", reasons

    # 默认: 采用复核文本
    reasons.append(f"accept_review(edit_distance={edit_ratio:.2f})")
    return review, "use_review", reasons


def _levenshtein_distance(a: str, b: str) -> int:
    """计算编辑距离。"""
    if len(a) < len(b):
        a, b = b, a
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(
                min(
                    curr[-1] + 1,
                    prev[j] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = curr
    return prev[-1]


# ═══════════════════════════════════════════════════════════
# ASR 流水线 (Paraformer → SenseVoice → FunASR → Whisper)
# ═══════════════════════════════════════════════════════════
