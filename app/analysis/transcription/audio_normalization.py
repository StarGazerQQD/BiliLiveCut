"""为 ASR 标准化媒体输入。"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from loguru import logger

from app.core.config import settings

_ASR_SAMPLE_RATE = 16_000
_NORMALIZE_TIMEOUT_S = 300


@contextmanager
def normalized_asr_audio(source_path: str) -> Iterator[str]:
    """把非 WAV 媒体解码为临时的 16kHz 单声道 PCM WAV。

    录制器产出的是带视频流的 TS；直接把五分钟 TS 交给生成式 ASR 容易让
    解码器处理超长单句。WAV 输入通常来自本模块或局部复核截取，可直接复用。

    :param source_path: 原始媒体文件路径。
    :yields: 可供 ASR 使用的 WAV 路径或原始 WAV 路径。
    :raises RuntimeError: 文件不存在、FFmpeg 超时或转换失败时。
    """
    source = Path(source_path)
    if source.suffix.casefold() in {".wav", ".wave"}:
        yield str(source)
        return
    if not source.is_file():
        raise RuntimeError(f"ASR 输入文件不存在: {source}")

    with TemporaryDirectory(prefix="blc-asr-") as temp_dir:
        output = Path(temp_dir) / "audio-16k-mono.wav"
        command = [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(_ASR_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_NORMALIZE_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ASR 音频标准化超时（>{_NORMALIZE_TIMEOUT_S}s）: {source}") from exc
        except OSError as exc:
            raise RuntimeError(f"无法启动 FFmpeg 标准化 ASR 音频: {exc}") from exc

        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "FFmpeg 未生成音频").strip()
            raise RuntimeError(f"ASR 音频标准化失败: {detail[:500]}")

        logger.debug("ASR 输入已标准化: {} -> {}", source, output)
        yield str(output)
