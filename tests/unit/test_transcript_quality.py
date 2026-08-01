"""长音频标准化与转写质量门禁回归测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from app.analysis.transcription.audio_normalization import normalized_asr_audio
from app.analysis.transcription.quality import assess_transcript_quality

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def test_quality_accepts_normal_transcript() -> None:
    """正常口语即使包含自然复述也不应被误判。"""
    quality = assess_transcript_quality("我刚才说了一次，现在再说一次，但接下来内容完全不同。")

    assert quality.usable is True
    assert quality.reason is None


def test_quality_rejects_empty_and_degenerate_repetition() -> None:
    """空输出和生成式解码循环都必须阻止进入下游。"""
    empty = assess_transcript_quality("  ，。！")
    repeated = assess_transcript_quality("等一下我们先看看" * 20)

    assert empty.usable is False
    assert empty.reason == "empty_output"
    assert repeated.usable is False
    assert repeated.reason == "degenerate_repetition"
    assert repeated.repetition_ratio >= 0.9


def test_non_wav_input_is_normalized_to_16k_mono_pcm(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """TS 输入应经 FFmpeg 生成临时 16kHz 单声道 PCM WAV，并在用后清理。"""
    source = tmp_path / "segment.ts"
    source.write_bytes(b"transport-stream")
    captured: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        Path(command[-1]).write_bytes(b"RIFF-test")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with normalized_asr_audio(str(source)) as normalized:
        normalized_path = Path(normalized)
        assert normalized_path.is_file()
        assert normalized_path.suffix == ".wav"
        assert ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"] == captured[7:14]

    assert normalized_path.exists() is False


def test_wav_input_bypasses_ffmpeg(monkeypatch: MonkeyPatch) -> None:
    """已标准化或测试用 WAV 不应重复转码。"""

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("WAV 不应调用 FFmpeg")

    monkeypatch.setattr(subprocess, "run", fail_run)
    with normalized_asr_audio("already-normalized.wav") as normalized:
        assert normalized == "already-normalized.wav"
