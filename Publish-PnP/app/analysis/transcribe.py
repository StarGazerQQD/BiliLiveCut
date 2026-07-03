"""语音转写。

采用"后端抽象 + 具体实现"的方式,便于替换/测试:

* :class:`TranscriberBackend` —— 协议,任何实现 ``transcribe(path)`` 的对象皆可;
* :class:`FasterWhisperBackend` —— 基于 ``faster-whisper`` 的本地转写(免 API 费);
* :func:`transcribe_segment` —— 读取片段、调用后端、把结果写入 ``transcripts`` 表。

``faster-whisper`` 为可选依赖(``pip install -e ".[asr]"``);若环境缺少它,
仅在真正调用时报出清晰的安装提示,不影响其它模块导入。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from loguru import logger

from app.core.config import settings
from app.db.models import RawSegment, SegmentStatus, Transcript
from app.db.session import get_session


@dataclass(slots=True)
class Word:
    """一个词及其时间戳(秒,相对片段起点)。"""

    word: str
    start: float
    end: float


@dataclass(slots=True)
class TranscriptionResult:
    """转写结果。

    :param text: 全文。
    :param language: 识别语言。
    :param words: 词级时间戳列表。
    :param avg_logprob: 平均置信度(越接近 0 越可信)。
    """

    text: str
    language: str
    words: list[Word] = field(default_factory=list)
    avg_logprob: float = 0.0


class TranscriberBackend(Protocol):
    """转写后端协议。"""

    def transcribe(self, audio_path: str) -> TranscriptionResult:
        """转写指定音频/视频文件。

        :param audio_path: 文件路径。
        :returns: :class:`TranscriptionResult`。
        """
        ...


class FasterWhisperBackend:
    """基于 faster-whisper 的本地转写后端。

    模型按 ``(model, device, compute_type)`` 懒加载并进程内缓存,避免重复加载。

    :param model_size: 模型规模,如 ``small`` / ``medium`` / ``large-v3``。
    :param device: ``cpu`` 或 ``cuda``。
    :param compute_type: ``int8`` / ``float16`` 等。
    """

    def __init__(
        self,
        model_size: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self.model_size = model_size or settings.whisper_model
        self.device = device or settings.whisper_device
        self.compute_type = compute_type or settings.whisper_compute_type

    def _load_model(self):  # noqa: ANN202 — 返回 faster_whisper.WhisperModel
        """懒加载并缓存 Whisper 模型。

        :returns: ``faster_whisper.WhisperModel`` 实例。
        :raises RuntimeError: 未安装 faster-whisper 时。
        """
        return _load_whisper_model(self.model_size, self.device, self.compute_type)

    def transcribe(self, audio_path: str) -> TranscriptionResult:
        """转写文件;开启 VAD 过滤与词级时间戳。

        :param audio_path: 文件路径。
        :returns: :class:`TranscriptionResult`。
        """
        model = self._load_model()
        # vad_filter:先用语音活动检测去掉静音段,减少幻觉与无效计算(降本)。
        # word_timestamps:输出词级时间,供边界吸附与语速分析使用。
        segments, info = model.transcribe(
            audio_path,
            vad_filter=True,
            word_timestamps=True,
        )

        words: list[Word] = []
        texts: list[str] = []
        logprobs: list[float] = []
        for seg in segments:
            texts.append(seg.text)
            logprobs.append(seg.avg_logprob)
            for w in seg.words or []:
                words.append(Word(word=w.word, start=float(w.start), end=float(w.end)))

        avg_logprob = sum(logprobs) / len(logprobs) if logprobs else 0.0
        return TranscriptionResult(
            text="".join(texts).strip(),
            language=info.language,
            words=words,
            avg_logprob=avg_logprob,
        )


@lru_cache(maxsize=2)
def _load_whisper_model(model_size: str, device: str, compute_type: str):  # noqa: ANN202
    """加载并缓存 WhisperModel(进程级)。

    :param model_size: 模型规模。
    :param device: 计算设备。
    :param compute_type: 计算精度。
    :returns: ``faster_whisper.WhisperModel``。
    :raises RuntimeError: 未安装 faster-whisper 时。
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "未安装 faster-whisper。请执行: pip install -e \".[asr]\"。"
            "若当前 Python 版本无 ctranslate2 预编译包,请改用 3.11/3.12 虚拟环境。"
        ) from exc

    logger.info(
        "加载 Whisper 模型 model={} device={} compute={}(首次会自动下载)",
        model_size,
        device,
        compute_type,
    )
    return WhisperModel(model_size, device=device, compute_type=compute_type)


@lru_cache(maxsize=1)
def get_default_backend() -> FasterWhisperBackend:
    """返回进程级缓存的默认转写后端。

    :returns: :class:`FasterWhisperBackend` 单例。
    """
    return FasterWhisperBackend()


def transcribe_segment(
    segment_id: int,
    backend: TranscriberBackend | None = None,
) -> Transcript:
    """转写指定片段并把结果写入数据库。

    :param segment_id: ``raw_segments`` 主键。
    :param backend: 可选转写后端;默认使用 faster-whisper。
    :returns: 已写入的 :class:`Transcript`。
    :raises ValueError: 片段不存在时。
    """
    backend = backend or get_default_backend()

    with get_session() as db:
        segment = db.get(RawSegment, segment_id)
        if segment is None:
            raise ValueError(f"片段不存在: id={segment_id}")
        file_path = segment.file_path

    logger.info("开始转写 segment={} -> {}", segment_id, file_path)
    result = backend.transcribe(file_path)

    words_json = json.dumps(
        [{"w": w.word, "start": w.start, "end": w.end} for w in result.words],
        ensure_ascii=False,
    )
    transcript = Transcript(
        segment_id=segment_id,
        language=result.language,
        text=result.text,
        words_json=words_json,
        avg_logprob=result.avg_logprob,
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
        "转写完成 segment={} transcript={} 字数={} 语言={}",
        segment_id,
        tid,
        len(result.text),
        result.language,
    )
    return transcript
