"""长音频标准化与转写质量门禁回归测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from app.analysis.transcription.audio_normalization import normalized_asr_audio
from app.analysis.transcription.quality import assess_transcript_quality, repair_local_decode_loop

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


def test_quality_rejects_local_decode_loop_inside_long_transcript() -> None:
    """局部解码循环即使只占五分钟正文的一小部分，也必须触发备用 ASR。"""
    normal_prefix = "".join(f"主播先介绍第{index}项游戏安排和准备尝试的玩法。" for index in range(12))
    local_loop = "等一下我们先看看" * 4
    normal_suffix = "".join(f"随后主播说明第{index}组队伍配置并回答观众问题。" for index in range(12))

    quality = assess_transcript_quality(normal_prefix + local_loop + normal_suffix)

    assert quality.usable is False
    assert quality.reason == "degenerate_repetition"
    assert 0 < quality.repetition_ratio < 0.45


def test_quality_keeps_short_natural_emphasis() -> None:
    """短促口语强调不应被局部循环门禁误判。"""
    quality = assess_transcript_quality("真的真的真的，我只是强调这件事情很重要，后面还有完全不同的说明。")

    assert quality.usable is True
    assert quality.reason is None


def test_repair_local_decode_loop_only_collapses_small_exact_loop() -> None:
    """局部解码循环保留两次强调，整段退化仍交给备用引擎。"""
    prefix = "".join(f"主播正在解释第{index}段不同的正常内容。" for index in range(20))
    suffix = "".join(f"随后继续介绍第{index}种不同玩法。" for index in range(20))
    local = repair_local_decode_loop(prefix + "等一下我们先看看" * 4 + suffix)
    dominant = repair_local_decode_loop("等一下我们先看看" * 20)

    assert local.changed is True
    assert local.text.count("等一下我们先看看") == 2
    assert assess_transcript_quality(local.text).usable is True
    assert dominant.changed is False


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
