"""多引擎 ASR 流水线 (V0.1.12 重构)。

架构:
    音频
    ├─ Paraformer-zh : 中文文本、时间戳、标点 (主引擎)
    ├─ SenseVoice-Small : 情感、笑声、音乐、事件 (辅助特征, 与主引擎并行)
    └─ Fun-ASR-Nano : 低置信度 / 非中文片段复核
    └─ Whisper large-v3 / turbo : 保留切换开关, 最终兜底

使用方式:
    backend = FunASRBackend()          # Paraformer + SenseVoice + FunASR-Nano
    pipeline = ASRPipeline(backend)    # 含 Whisper 兜底
    result = pipeline.transcribe(audio_path)

全部模型懒加载(首次调用时加载并缓存), 各引擎按 flags 独立启用/禁用。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from app.core.config import settings
from app.db.models import RawSegment, SegmentStatus, Transcript
from app.db.session import get_session

if TYPE_CHECKING:
    pass


# ═══════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════

@dataclass(slots=True)
class Word:
    """一个词及其时间戳(秒, 相对片段起点)。"""

    word: str
    start: float
    end: float


@dataclass(slots=True)
class EmotionEvent:
    """SenseVoice 检测到的辅助事件。"""

    event_type: str       # "laughter" / "music" / "applause" / "cough" / "emotion:angry/..."
    start: float
    end: float
    confidence: float = 1.0


@dataclass(slots=True)
class TranscriptionResult:
    """转写结果。

    :param text: 全文 (Paraformer-zh)。
    :param language: 识别语言。
    :param words: 词级时间戳列表。
    :param avg_logprob: 平均置信度 (越接近 0 越可信)。
    :param emotions: SenseVoice 检测到的情感/笑声/音乐/事件。
    :param reviewed_segments: Fun-ASR-Nano 复核结果列表。
    :param engine: 实际使用的转写引擎名称。
    """

    text: str
    language: str
    words: list[Word] = field(default_factory=list)
    avg_logprob: float = 0.0
    emotions: list[EmotionEvent] = field(default_factory=list)
    reviewed_segments: list[dict] = field(default_factory=list)
    engine: str = "paraformer"


# ═══════════════════════════════════════════════════════════
# 后端协议
# ═══════════════════════════════════════════════════════════

class TranscriberBackend(Protocol):
    """转写后端协议。"""

    def transcribe(self, audio_path: str, initial_prompt: str | None = None) -> TranscriptionResult:
        """转写音频文件。

        :param audio_path: 文件路径。
        :param initial_prompt: 引导热词 (可选)。
        :returns: :class:`TranscriptionResult`。
        """
        ...

    def transcribe_segment(self, audio_path: str, start: float, end: float) -> TranscriptionResult:
        """转写音频片段 (用于复核)。

        :param audio_path: 文件路径。
        :param start: 起始秒。
        :param end: 结束秒。
        :returns: :class:`TranscriptionResult`。
        """
        ...


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

    def __init__(
        self,
        primary: str | None = None,
        sensevoice: bool | None = None,
        funasr_nano: bool | None = None,
    ) -> None:
        self._primary_model_name = primary or "paraformer-zh"
        self._use_sensevoice = sensevoice if sensevoice is not None else settings.asr_sensevoice
        self._use_funasr = funasr_nano if funasr_nano is not None else settings.asr_funasr_review
        self._primary: object | None = None
        self._sensevoice: object | None = None
        self._funasr: object | None = None

    # ---- 懒加载 ----

    def _load_primary(self) -> object:
        """加载 Paraformer-zh 主引擎 (中文 ASR + 标点 + 时间戳)。"""
        if self._primary is not None:
            return self._primary
        try:
            from funasr import AutoModel
        except ImportError:
            raise RuntimeError(
                "需要安装 funasr。请执行: pip install funasr modelscope"
            ) from None
        logger.info("加载 Paraformer-zh 主引擎 model={}", self._primary_model_name)
        self._primary = AutoModel(
            model=self._primary_model_name,
            vad_model="fsmn-vad",
            punc_model="ct-punc",
            spk_model="cam++",
            device=settings.whisper_device,
            hub="ms",
            revision=settings.asr_model_revision,
        )
        return self._primary

    def _load_sensevoice(self) -> object:
        """加载 SenseVoice-Small (情感/笑声/音乐/事件检测)。"""
        if self._sensevoice is not None:
            return self._sensevoice
        try:
            from funasr import AutoModel
        except ImportError:
            raise RuntimeError(
                "需要安装 funasr。请执行: pip install funasr modelscope"
            ) from None
        logger.info("加载 SenseVoice-Small 辅助特征引擎")
        self._sensevoice = AutoModel(
            model="iic/SenseVoiceSmall",
            device=settings.whisper_device,
            hub="ms",
            revision=settings.asr_model_revision,
        )
        return self._sensevoice

    def _load_funasr(self) -> object:
        """加载 Fun-ASR-Nano (低置信度复核)。"""
        if self._funasr is not None:
            return self._funasr
        try:
            from funasr import AutoModel
        except ImportError:
            raise RuntimeError(
                "需要安装 funasr。请执行: pip install funasr modelscope"
            ) from None
        logger.info("加载 Fun-ASR-Nano 复核引擎")
        self._funasr = AutoModel(
            model="iic/Fun-ASR-Nano",
            device=settings.whisper_device,
            hub="ms",
            revision=settings.asr_model_revision,
        )
        return self._funasr

    # ---- 转写 ----

    def transcribe(self, audio_path: str, initial_prompt: str | None = None) -> TranscriptionResult:
        """完整转写: Paraformer 主引擎 + SenseVoice 辅助特征。

        :param audio_path: 音频文件路径。
        :param initial_prompt: 热词引导 (Paraformer 不使用此参数, 保留兼容)。
        :returns: :class:`TranscriptionResult`。
        """
        model = self._load_primary()
        t0 = time.time()
        result = model.generate(input=audio_path)
        elapsed = time.time() - t0
        logger.info("Paraformer 主引擎转写完成, 耗时 {:.1f}s", elapsed)

        if not result or len(result) == 0:
            return TranscriptionResult(text="", language="zh", avg_logprob=-10.0, engine="paraformer")

        res = result[0]
        text = res.get("text", "")
        language = "zh"

        words: list[Word] = []
        timestamps = res.get("timestamp", []) or []
        for ts_item in timestamps:
            if len(ts_item) >= 3:
                words.append(Word(word=str(ts_item[0]), start=float(ts_item[1]) / 1000.0, end=float(ts_item[2]) / 1000.0))

        sentences = res.get("sentence_info", []) or []
        logprobs = [s.get("confidence", 0.0) for s in sentences if isinstance(s, dict)]
        avg_logprob = sum(logprobs) / len(logprobs) if logprobs else 0.0

        # 并行检测辅助特征
        emotions: list[EmotionEvent] = []
        if self._use_sensevoice:
            try:
                emotions = self._detect_auxiliary(audio_path)
            except Exception as exc:
                logger.warning("SenseVoice 辅助特征检测失败: {}", exc)

        return TranscriptionResult(
            text=text.strip(),
            language=language,
            words=words,
            avg_logprob=avg_logprob,
            emotions=emotions,
            engine="paraformer",
        )

    def _detect_auxiliary(self, audio_path: str) -> list[EmotionEvent]:
        """SenseVoice-Small: 检测情感、笑声、音乐、事件。"""
        sv = self._load_sensevoice()
        t0 = time.time()
        result = sv.generate(input=audio_path)
        elapsed = time.time() - t0
        logger.info("SenseVoice 辅助特征检测完成, 耗时 {:.1f}s", elapsed)

        events: list[EmotionEvent] = []
        if not result or len(result) == 0:
            return events

        res = result[0]
        # SenseVoice 输出格式: [{text, emo_label, event_label, ...}]
        timestamps = res.get("timestamp", []) or []

        # 提取情感标签
        emo_label = res.get("emo_label", "")
        if emo_label:
            # emo_label 格式: "<HAPPY>0.9|<SAD>0.1"
            for part in emo_label.split("|"):
                if not part:
                    continue
                tag_val = part.split(">", 1)
                if len(tag_val) == 2:
                    emo_name = tag_val[0].lstrip("<")
                    try:
                        conf = float(tag_val[1])
                    except ValueError:
                        conf = 1.0
                    events.append(EmotionEvent(
                        event_type=f"emotion:{emo_name}",
                        start=0.0,
                        end=0.0,
                        confidence=conf,
                    ))

        # 提取事件标签 (笑声/音乐/鼓掌等)
        event_label = res.get("event_label", "")
        if event_label:
            for part in event_label.split("|"):
                if not part:
                    continue
                tag_val = part.split(">", 1)
                if len(tag_val) == 2:
                    evt_name = tag_val[0].lstrip("<")
                    try:
                        conf = float(tag_val[1])
                    except ValueError:
                        conf = 1.0
                    events.append(EmotionEvent(
                        event_type=evt_name.lower(),
                        start=0.0,
                        end=0.0,
                        confidence=conf,
                    ))

        # 带时间戳的事件 (如果 SenseVoice 返回了时间戳)
        for ts_item in timestamps:
            if len(ts_item) >= 3:
                # 时间戳项可能包含事件标签
                pass  # SenseVoice 时间戳结构因版本而异, 保留扩展点

        return events

    def transcribe_segment(self, audio_path: str, start: float, end: float) -> TranscriptionResult:
        """Fun-ASR-Nano: 对低置信度片段复核。

        :param audio_path: 音频文件路径。
        :param start: 起始位置 (秒)。
        :param end: 结束位置 (秒)。
        :returns: :class:`TranscriptionResult`。
        """
        if not self._use_funasr:
            return TranscriptionResult(text="", language="zh", engine="funasr-nano")
        model = self._load_funasr()
        try:
            result = model.generate(input=audio_path, hotword="", beginning=start, ending=end)
        except TypeError:
            # Fun-ASR-Nano 可能不支持 beginning/ending 参数
            result = model.generate(input=audio_path)

        if not result or len(result) == 0:
            return TranscriptionResult(text="", language="zh", engine="funasr-nano")

        res = result[0]
        text = res.get("text", "")
        logprob = res.get("confidence", 0.0)

        return TranscriptionResult(
            text=text.strip(),
            language="zh",
            avg_logprob=logprob,
            engine="funasr-nano",
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
        self.model_size = model_size or settings.whisper_model
        self.device = device or settings.whisper_device
        self.compute_type = compute_type or settings.whisper_compute_type

    def _load_model(self):  # noqa: ANN202
        return _load_whisper_model(self.model_size, self.device, self.compute_type)

    def transcribe(self, audio_path: str, initial_prompt: str | None = None) -> TranscriptionResult:
        """Whisper 转写 (兜底)。

        :param audio_path: 文件路径。
        :param initial_prompt: hotwords 引导。
        :returns: :class:`TranscriptionResult`。
        """
        model = self._load_model()
        kwargs = {"vad_filter": True, "word_timestamps": True}
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        t0 = time.time()
        segments, info = model.transcribe(audio_path, **kwargs)
        elapsed = time.time() - t0

        words: list[Word] = []
        texts: list[str] = []
        logprobs: list[float] = []
        for seg in segments:
            texts.append(seg.text)
            logprobs.append(seg.avg_logprob)
            for w in seg.words or []:
                words.append(Word(word=w.word, start=float(w.start), end=float(w.end)))

        avg_logprob = sum(logprobs) / len(logprobs) if logprobs else 0.0
        logger.info("Whisper 兜底转写完成, 耗时 {:.1f}s, 语言={}", elapsed, info.language)
        return TranscriptionResult(
            text="".join(texts).strip(),
            language=info.language,
            words=words,
            avg_logprob=avg_logprob,
            engine="whisper",
        )

    def transcribe_segment(self, audio_path: str, start: float, end: float) -> TranscriptionResult:
        """Whisper 不支持片段转写, 直接全文转写。

        :param audio_path: 文件路径。
        :returns: :class:`TranscriptionResult`。
        """
        return self.transcribe(audio_path)


@lru_cache(maxsize=2)
def _load_whisper_model(model_size: str, device: str, compute_type: str):  # noqa: ANN202
    """加载并缓存 WhisperModel (进程级, 最多缓存 2 个)。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "未安装 faster-whisper。请执行: pip install -e \".[asr]\"。"
            "若当前 Python 版本无 ctranslate2 预编译包, 请改用 3.11/3.12 虚拟环境。"
        ) from exc
    logger.info(
        "加载 Whisper 兜底模型 model={} device={} compute={}",
        model_size, device, compute_type,
    )
    return WhisperModel(model_size, device=device, compute_type=compute_type)


# ═══════════════════════════════════════════════════════════
# ASR 流水线 (Paraformer → SenseVoice → FunASR → Whisper)
# ═══════════════════════════════════════════════════════════

class ASRPipeline:
    """多引擎 ASR 流水线。

    流程:
        1. Paraformer-zh 主引擎转写 (中文文本 + 时间戳 + 标点)
        2. SenseVoice-Small 辅助特征 (情感/笑声/音乐/事件, 已在主引擎内并行调用)
        3. Fun-ASR-Nano 低置信度复核 (逐句检查, 低分片段复核)
        4. Whisper 兜底 (主引擎失败或无输出时自动切换)
    """

    def __init__(
        self,
        primary_backend: FunASRBackend | None = None,
        whisper_backend: FasterWhisperBackend | None = None,
    ) -> None:
        self._primary = primary_backend
        self._whisper = whisper_backend
        self._use_fallback = settings.asr_fallback_whisper
        self._confidence_threshold = settings.asr_confidence_threshold

    def _get_primary(self) -> FunASRBackend:
        if self._primary is None:
            self._primary = FunASRBackend()
        return self._primary

    def _get_whisper(self) -> FasterWhisperBackend:
        if self._whisper is None:
            self._whisper = FasterWhisperBackend()
        return self._whisper

    def transcribe(self, audio_path: str, initial_prompt: str | None = None) -> TranscriptionResult:
        """执行完整多引擎 ASR 流水线。

        :param audio_path: 音频文件路径。
        :param initial_prompt: 热词引导。
        :returns: :class:`TranscriptionResult`。
        """
        use_paraformer = settings.asr_primary == "paraformer"

        if use_paraformer:
            try:
                result = self._get_primary().transcribe(audio_path, initial_prompt)
            except Exception as exc:
                logger.error("Paraformer 主引擎转写失败: {}", exc)
                result = TranscriptionResult(text="", language="zh", avg_logprob=-10.0, engine="paraformer")

            # 低置信度复核: Fun-ASR-Nano
            if settings.asr_funasr_review and result.text:
                result = self._review_low_confidence(result, audio_path)

            # 主引擎有结果则返回
            if result.text and len(result.text.strip()) > 0:
                return result

            # 主引擎空结果 → 兜底 Whisper
            if self._use_fallback:
                logger.info("Paraformer 无有效输出, 切换 Whisper 兜底")
                return self._get_whisper().transcribe(audio_path, initial_prompt)
            return result

        # 直接使用 Whisper
        if self._use_fallback:
            return self._get_whisper().transcribe(audio_path, initial_prompt)

        logger.warning("ASR 主引擎未配置且 Whisper 兜底已禁用, 返回空结果")
        return TranscriptionResult(text="", language="zh", avg_logprob=-10.0, engine="none")

    def _review_low_confidence(self, result: TranscriptionResult, audio_path: str) -> TranscriptionResult:
        """Fun-ASR-Nano: 逐句检查低置信度片段并复核。

        仅对 avg_logprob < threshold 的连续文本片段触发复核。
        """
        primary = self._get_primary()
        reviewed: list[dict] = []

        # 按句子拆分 (基于标点或时间戳间隔)
        sentences = _split_sentences(result.text, result.words)
        for sent in sentences:
            if sent.get("avg_logprob", 0.0) < self._confidence_threshold:
                try:
                    review = primary.transcribe_segment(
                        audio_path,
                        sent.get("start", 0.0),
                        sent.get("end", 0.0),
                    )
                    reviewed.append({
                        "original": sent.get("text", ""),
                        "original_score": sent.get("avg_logprob", 0.0),
                        "reviewed": review.text,
                        "reviewed_score": review.avg_logprob,
                        "start": sent.get("start"),
                        "end": sent.get("end"),
                    })
                except Exception as exc:
                    logger.warning("Fun-ASR-Nano 片段复核失败: {}", exc)

        if reviewed:
            result.reviewed_segments = reviewed
            logger.info("Fun-ASR-Nano: 复核 {} 个低置信度片段", len(reviewed))

        return result


def _split_sentences(text: str, words: list[Word] | None = None) -> list[dict]:
    """将文本按句子拆分, 关联时间戳和置信度。

    :param text: 全文。
    :param words: 词级时间戳列表 (可选)。
    :returns: 句子列表 [{text, start, end, avg_logprob}]。
    """
    sentences: list[dict] = []
    if not text:
        return sentences

    # 简单按标点分割
    import re
    parts = re.split(r'[。！？；\n]', text)
    parts = [p.strip() for p in parts if p.strip()]

    word_idx = 0
    for part in parts:
        sent = {"text": part, "avg_logprob": 0.0}
        if words and word_idx < len(words):
            # 估算该句子的起止时间
            char_count = len(part)
            if char_count > 0:
                start_word = min(word_idx, len(words) - 1)
                end_word = min(word_idx + max(char_count // 2, 1), len(words) - 1)
                sent["start"] = words[start_word].start
                sent["end"] = words[end_word].end
                word_idx = end_word + 1
        sentences.append(sent)

    return sentences


# ═══════════════════════════════════════════════════════════
# 全局 pipeline + transcribe_segment
# ═══════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def get_default_pipeline() -> ASRPipeline:
    """返回进程级缓存的默认 ASR 流水线。

    :returns: :class:`ASRPipeline` 单例。
    """
    return ASRPipeline()


def transcribe_segment(
    segment_id: int,
    backend: TranscriberBackend | None = None,
) -> Transcript:
    """转写指定片段并把结果写入数据库。

    自动注入房间级 hotwords, 应用 aliases 纠错, 存储辅助特征。

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

    words_json = json.dumps(
        [{"w": w.word, "start": w.start, "end": w.end} for w in result.words],
        ensure_ascii=False,
    )

    # V0.1.12: 序列化辅助特征
    auxiliary_json: str | None = None
    if result.emotions or result.reviewed_segments:
        auxiliary_json = json.dumps({
            "emotions": [
                {"type": e.event_type, "start": e.start, "end": e.end, "confidence": e.confidence}
                for e in result.emotions
            ],
            "reviewed_segments": result.reviewed_segments,
            "engine": result.engine,
        }, ensure_ascii=False)

    transcript = Transcript(
        segment_id=segment_id,
        language=result.language,
        text=text,
        words_json=words_json,
        avg_logprob=result.avg_logprob,
        auxiliary_json=auxiliary_json,
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
        "转写完成 segment={} transcript={} 字数={} 语言={} 引擎={}",
        segment_id, tid, len(result.text), result.language, result.engine,
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
